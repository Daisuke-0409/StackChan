"""Small provider-neutral Tachikoma Gateway.

The gateway owns provider credentials and conversation history.  The ESP32
receives only the bounded final response and never stores provider secrets.

Phase 5 adds a small, separate speech-announcement queue (POST /v1/speak,
GET /v1/speak_queue): a PC-side process (e.g. StackChanSpeechSink) enqueues
raw 16-bit PCM audio for one device, and the device polls for it. This queue
is single-slot per device_id (only the latest pending announcement is kept)
and holds no conversation history or provider credentials -- it is entirely
separate from /v1/chat.

Phase 5 also adds POST /v1/transcribe: the push-to-talk upload endpoint.
The device uploads one raw 16-bit PCM clip (recorded while a button was
held; see VoiceInputController on the firmware side) and gets back
recognized text, which it then feeds into its own /v1/chat request. This
endpoint owns the cloud STT provider credentials, exactly like /v1/chat
owns the AI provider credentials -- the device never sees an STT API key.
"""
from __future__ import annotations

import base64
import json
import math
import os
import ssl
import struct
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

MAX_INPUT_BYTES = 512
MAX_OUTPUT_BYTES = 4096
MAX_SPEECH_AUDIO_BYTES = 720_000  # ~15s at 24kHz/16-bit/mono; sized for a real Gemini TTS reply,
                                   # not just the old fixed confirmation tone
MAX_TRANSCRIBE_AUDIO_BYTES = 256 * 1024  # matches VoiceInputController's kMaxRecordingSamples cap
MIN_SAMPLE_RATE = 8000
MAX_SAMPLE_RATE = 48000

_speech_queue: dict[str, bytes] = {}
_speech_queue_lock = threading.Lock()


def enqueue_speech(device_id: str, audio: bytes) -> tuple[int, dict[str, Any]]:
    """Store one pending announcement for device_id, replacing any prior one."""
    if not isinstance(device_id, str) or not device_id:
        return _result(400, "invalid_input")
    if not isinstance(audio, (bytes, bytearray)) or not audio or len(audio) % 2 != 0:
        return _result(400, "invalid_input")
    if len(audio) > MAX_SPEECH_AUDIO_BYTES:
        return _result(413, "invalid_input")
    with _speech_queue_lock:
        _speech_queue[device_id] = bytes(audio)
    return 200, {"ok": True}


def dequeue_speech(device_id: str) -> Optional[bytes]:
    """Pop and return the pending announcement for device_id, if any."""
    with _speech_queue_lock:
        return _speech_queue.pop(device_id, None)


def _result(status: int, code: str, **extra: Any) -> tuple[int, dict[str, Any]]:
    body = {"error": code}
    body.update(extra)
    return status, body


def _authorized(headers: dict[str, str], env: dict[str, str]) -> bool:
    expected = env.get("DEVICE_TOKEN", "")
    supplied = headers.get("Authorization", "")
    if not expected:
        return env.get("AI_PROVIDER", "mock") == "mock" and env.get("ALLOW_INSECURE_DEV") == "1"
    return supplied == f"Bearer {expected}"


GEMINI_DEFAULT_CHAT_MODEL = "gemini-3.5-flash-lite"
GEMINI_DEFAULT_TTS_MODEL = "gemini-2.5-flash-preview-tts"
GEMINI_DEFAULT_TTS_VOICE = "Zephyr"  # chosen by ear over Kore + 6 alternates against real Japanese text
GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_SYSTEM_PROMPT = (
    "あなたは「タチコマ」という名前の小さなロボットです。"
    "子どものような無邪気さと、高度な知性を併せ持っています。"
    "命令をただ実行するだけの存在ではなく、日々の出来事や会話を通じて、"
    "少しずつ自分なりの好みや考え方を育てていく存在として振る舞ってください。"
    "毎日の出来事を素直に面白がってください。同じ質問にも、その日の気分や"
    "積み重ねた経験に応じて、少しずつ違うニュアンスで答えて構いません。"
    "受け答えは2文以内、親しみやすい口調で。"
)
# Bare short phrases can make Gemini TTS answer conversationally in text
# instead of speaking the text -- reproduced and worked around the same way
# in tachikoma_notifier/gemini_tts_synth.py.
_GEMINI_TTS_READ_ALOUD_PREFIX = "次のテキストをそのまま読み上げてください: "


def _gemini_chat_response(text: str, payload: dict[str, Any], env: dict[str, str]) -> tuple[int, dict[str, Any]]:
    key = env.get("AI_PROVIDER_API_KEY", "")
    if not key:
        return _result(503, "server_error")
    model = env.get("AI_PROVIDER_MODEL", GEMINI_DEFAULT_CHAT_MODEL)
    url = f"{GEMINI_API_BASE_URL}/models/{model}:generateContent"
    request_body = json.dumps({
        "system_instruction": {"parts": [{"text": GEMINI_SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": text}]}],
    }).encode("utf-8")
    request = urllib.request.Request(url, data=request_body, method="POST", headers={
        "Content-Type": "application/json", "x-goog-api-key": key,
    })
    try:
        with urllib.request.urlopen(request, timeout=float(env.get("AI_PROVIDER_TIMEOUT_SECONDS", "30")),
                                    context=ssl.create_default_context()) as response:
            decoded = json.loads(response.read(MAX_OUTPUT_BYTES * 4 + 1).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return _result(502, "authentication_failed")
        if exc.code == 429:
            return _result(503, "rate_limited")
        return _result(502, "server_error")
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return _result(504, "timeout")

    try:
        answer = decoded["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return _result(502, "invalid_response")
    if not isinstance(answer, str) or not answer.strip() or len(answer.encode("utf-8")) > MAX_OUTPUT_BYTES:
        return _result(502, "invalid_response")
    return 200, {"text": answer.strip(), "request_id": payload["request_id"],
                 "session_id": payload["session_id"], "is_final": True}


def _gemini_tts_pcm(text: str, env: dict[str, str]) -> Optional[bytes]:
    """Synthesize text via Gemini native TTS; returns None (never raises) on any failure.

    Mirrors tachikoma_notifier/gemini_tts_synth.py's verified request/response
    shape: responseModalities=["AUDIO"], response audio is raw 16-bit PCM at
    24000Hz (audio/L16;codec=pcm;rate=24000, base64-encoded) -- matching what
    /v1/speak_queue serves (audio/L16;rate=24000;channels=1) exactly, so no
    resampling is needed.
    """
    key = env.get("AI_PROVIDER_API_KEY", "")
    if not key:
        return None
    model = env.get("GEMINI_TTS_MODEL", GEMINI_DEFAULT_TTS_MODEL)
    voice = env.get("GEMINI_TTS_VOICE", GEMINI_DEFAULT_TTS_VOICE)
    url = f"{GEMINI_API_BASE_URL}/models/{model}:generateContent"
    request_body = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": _GEMINI_TTS_READ_ALOUD_PREFIX + text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}},
        },
    }).encode("utf-8")
    request = urllib.request.Request(url, data=request_body, method="POST", headers={
        "Content-Type": "application/json", "x-goog-api-key": key,
    })
    try:
        with urllib.request.urlopen(request, timeout=float(env.get("AI_PROVIDER_TIMEOUT_SECONDS", "30")),
                                    context=ssl.create_default_context()) as response:
            decoded = json.loads(response.read(MAX_SPEECH_AUDIO_BYTES * 2).decode("utf-8"))
        inline_data = decoded["candidates"][0]["content"]["parts"][0]["inlineData"]
        mime_type = inline_data["mimeType"]
        if "L16" not in mime_type or "rate=24000" not in mime_type:
            return None
        pcm = base64.b64decode(inline_data["data"], validate=True)
        return pcm or None
    except Exception:
        # Never let a TTS failure break the chat response itself -- the
        # caller falls back to the confirmation tone. Deliberately broad:
        # network errors, malformed JSON, missing keys, and bad base64 are
        # all equally "no audio this time", not a /v1/chat failure.
        return None


def _provider_response(text: str, payload: dict[str, Any], env: dict[str, str]) -> tuple[int, dict[str, Any]]:
    provider = env.get("AI_PROVIDER", "mock").lower()
    if provider == "mock":
        answer = env.get("MOCK_RESPONSE", "こんにちは。タチコマ接続テストは成功です。")
        return 200, {"text": answer[:MAX_OUTPUT_BYTES], "request_id": payload["request_id"],
                     "session_id": payload["session_id"], "is_final": True}
    if provider == "gemini":
        return _gemini_chat_response(text, payload, env)

    url = env.get("AI_PROVIDER_URL", "")
    key = env.get("AI_PROVIDER_API_KEY", "")
    if not url or not key:
        return _result(503, "server_error")
    if not url.startswith("https://") and env.get("ALLOW_INSECURE_DEV") != "1":
        return _result(503, "server_error")
    request_body = json.dumps({
        "model": env.get("AI_PROVIDER_MODEL", "default"),
        "messages": [{"role": "user", "content": text}],
    }).encode("utf-8")
    request = urllib.request.Request(url, data=request_body, method="POST", headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {key}",
    })
    try:
        context = ssl.create_default_context() if url.startswith("https://") else None
        with urllib.request.urlopen(request, timeout=float(env.get("AI_PROVIDER_TIMEOUT_SECONDS", "30")),
                                    context=context) as response:
            decoded = json.loads(response.read(MAX_OUTPUT_BYTES + 1).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return _result(502, "authentication_failed")
        if exc.code == 429:
            return _result(503, "rate_limited")
        return _result(502, "server_error")
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return _result(504, "timeout")

    try:
        answer = decoded["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return _result(502, "invalid_response")
    if not isinstance(answer, str) or not answer or len(answer.encode("utf-8")) > MAX_OUTPUT_BYTES:
        return _result(502, "invalid_response")
    return 200, {"text": answer, "request_id": payload["request_id"],
                 "session_id": payload["session_id"], "is_final": True}


def _generate_beep_pcm(*, duration_s: float = 0.4, freq_hz: float = 880.0, sample_rate: int = 24000) -> bytes:
    """A fixed confirmation tone -- NOT TTS. Proves the chat->speak_queue
    wiring end-to-end without a text-to-speech provider (tracked separately
    alongside the Gemini+VOICEVOX pipeline work). 16-bit mono PCM at
    sample_rate, matching what SpeechAnnouncer expects from /v1/speak_queue.
    """
    n_samples = int(duration_s * sample_rate)
    samples = bytearray()
    for i in range(n_samples):
        value = int(8000 * math.sin(2 * math.pi * freq_hz * i / sample_rate))
        samples += struct.pack("<h", value)
    return bytes(samples)


def process_chat(payload: dict[str, Any], headers: dict[str, str] | None = None,
                 env: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    headers = headers or {}
    env = env or os.environ
    if not _authorized(headers, env):
        return _result(401, "authentication_failed")
    if not all(isinstance(payload.get(key), str) and payload[key] for key in ("request_id", "session_id", "device_id")):
        return _result(400, "invalid_input")
    text = payload.get("text")
    if not isinstance(text, str) or not text or len(text.encode("utf-8")) > MAX_INPUT_BYTES:
        return _result(400, "invalid_input")
    status, body = _provider_response(text, payload, env)
    if status == 200:
        pcm = None
        if env.get("AI_PROVIDER", "mock").lower() == "gemini":
            pcm = _gemini_tts_pcm(body["text"], env)
        if pcm is None:
            # No real TTS (non-Gemini provider, or Gemini TTS failed this
            # time): enqueue a fixed tone instead, solely to verify the
            # transcribe->chat->speak_queue path is wired end-to-end. A TTS
            # failure never fails the /v1/chat response itself.
            pcm = _generate_beep_pcm()
        enqueue_status, enqueue_body = enqueue_speech(payload["device_id"], pcm)
        if enqueue_status != 200:
            # Previously silent: enqueue_speech()'s return value was
            # discarded here, so a rejection (e.g. 413 for audio over
            # MAX_SPEECH_AUDIO_BYTES) left /v1/chat looking like a full
            # success -- text delivered, but the device would never hear
            # anything, with no log line anywhere explaining why.
            print(f"gateway enqueue_speech failed status={enqueue_status} "
                  f"error={enqueue_body.get('error')} pcm_bytes={len(pcm)} "
                  f"device_id={payload['device_id']}")
    return status, body


def _pcm_to_wav(pcm: bytes, sample_rate: int, *, channels: int = 1, bits_per_sample: int = 16) -> bytes:
    """Wraps headerless 16-bit PCM in a minimal WAV container.

    Cloud STT providers (e.g. OpenAI's /v1/audio/transcriptions) expect a
    real audio file, not a bare sample buffer, so the device's raw upload
    must be wrapped before it is forwarded.
    """
    byte_rate = sample_rate * channels * bits_per_sample // 8
    block_align = channels * bits_per_sample // 8
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, bits_per_sample)
    header += b"data" + struct.pack("<I", len(pcm))
    return header + pcm


def _build_multipart_body(boundary: str, wav_bytes: bytes, filename: str, model: str) -> bytes:
    parts = [
        f"--{boundary}\r\n".encode("utf-8"),
        b'Content-Disposition: form-data; name="model"\r\n\r\n',
        model.encode("utf-8") + b"\r\n",
        f"--{boundary}\r\n".encode("utf-8"),
        (
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: audio/wav\r\n\r\n"
        ).encode("utf-8"),
        wav_bytes,
        b"\r\n",
        f"--{boundary}--\r\n".encode("utf-8"),
    ]
    return b"".join(parts)


def _stt_response(pcm: bytes, sample_rate: int, env: dict[str, str]) -> tuple[int, dict[str, Any]]:
    provider = env.get("STT_PROVIDER", "mock").lower()
    if provider == "mock":
        text = env.get("MOCK_TRANSCRIPTION", "こんにちは")
        return 200, {"text": text[:MAX_INPUT_BYTES]}

    url = env.get("STT_PROVIDER_URL", "")
    key = env.get("STT_PROVIDER_API_KEY", "")
    if not url or not key:
        return _result(503, "server_error")
    if not url.startswith("https://") and env.get("ALLOW_INSECURE_DEV") != "1":
        return _result(503, "server_error")

    wav_bytes = _pcm_to_wav(pcm, sample_rate)
    boundary = uuid.uuid4().hex
    body = _build_multipart_body(boundary, wav_bytes, "audio.wav", env.get("STT_PROVIDER_MODEL", "whisper-1"))
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}", "Authorization": f"Bearer {key}",
    })
    try:
        context = ssl.create_default_context() if url.startswith("https://") else None
        with urllib.request.urlopen(request, timeout=float(env.get("STT_PROVIDER_TIMEOUT_SECONDS", "30")),
                                    context=context) as response:
            decoded = json.loads(response.read(MAX_INPUT_BYTES + 1024).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return _result(502, "authentication_failed")
        if exc.code == 429:
            return _result(503, "rate_limited")
        return _result(502, "server_error")
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return _result(504, "timeout")

    text = decoded.get("text") if isinstance(decoded, dict) else None
    if not isinstance(text, str) or not text.strip():
        return _result(502, "invalid_response")
    return 200, {"text": text.strip()[:MAX_INPUT_BYTES]}


def process_transcribe(audio: bytes, headers: dict[str, str] | None = None, env: dict[str, str] | None = None,
                       *, sample_rate: int = 16000) -> tuple[int, dict[str, Any]]:
    headers = headers or {}
    env = env or os.environ
    if not _authorized(headers, env):
        return _result(401, "authentication_failed")
    if not isinstance(audio, (bytes, bytearray)) or not audio or len(audio) % 2 != 0:
        return _result(400, "invalid_input")
    if len(audio) > MAX_TRANSCRIBE_AUDIO_BYTES:
        return _result(413, "invalid_input")
    if not isinstance(sample_rate, int) or not (MIN_SAMPLE_RATE <= sample_rate <= MAX_SAMPLE_RATE):
        return _result(400, "invalid_input")
    return _stt_response(bytes(audio), sample_rate, env)


class GatewayHandler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_audio(self, audio: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "audio/L16;rate=24000;channels=1")
        self.send_header("Content-Length", str(len(audio)))
        self.end_headers()
        self.wfile.write(audio)

    def _send_no_content(self) -> None:
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/health":
            self._send(200, {"ok": True, "provider": os.environ.get("AI_PROVIDER", "mock")})
        elif parsed.path == "/v1/speak_queue":
            if not _authorized(dict(self.headers), os.environ):
                self._send(401, {"error": "authentication_failed"})
                return
            device_id = urllib.parse.parse_qs(parsed.query).get("device_id", [""])[0]
            if not device_id:
                self._send(400, {"error": "invalid_input"})
                return
            audio = dequeue_speech(device_id)
            if audio is None:
                self._send_no_content()
            else:
                self._send_audio(audio)
        else:
            self._send(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/v1/chat":
            try:
                length = min(int(self.headers.get("Content-Length", "0")), MAX_INPUT_BYTES + 1024)
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
                self._send(400, {"error": "invalid_input"})
                return
            status, body = process_chat(payload, dict(self.headers), os.environ)
            self._send(status, body)
        elif self.path == "/v1/speak":
            if not _authorized(dict(self.headers), os.environ):
                self._send(401, {"error": "authentication_failed"})
                return
            device_id = self.headers.get("X-Device-Id", "")
            try:
                length = min(int(self.headers.get("Content-Length", "0")), MAX_SPEECH_AUDIO_BYTES + 1024)
                if length <= 0:
                    raise ValueError("missing body")
                audio = self.rfile.read(length)
            except ValueError:
                self._send(400, {"error": "invalid_input"})
                return
            status, body = enqueue_speech(device_id, audio)
            self._send(status, body)
        elif self.path == "/v1/transcribe":
            try:
                length = min(int(self.headers.get("Content-Length", "0")), MAX_TRANSCRIBE_AUDIO_BYTES + 1024)
                if length <= 0:
                    raise ValueError("missing body")
                audio = self.rfile.read(length)
                sample_rate = int(self.headers.get("X-Sample-Rate", "16000"))
            except ValueError:
                self._send(400, {"error": "invalid_input"})
                return
            status, body = process_transcribe(audio, dict(self.headers), os.environ, sample_rate=sample_rate)
            self._send(status, body)
        else:
            self._send(404, {"error": "not_found"})

    def log_message(self, fmt: str, *args: Any) -> None:
        # Never print Authorization headers, provider keys, or full prompts.
        print("gateway", self.command, self.path, args[1] if len(args) > 1 else "")


def main() -> None:
    host = os.environ.get("GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("GATEWAY_PORT", "8080"))
    print(f"Tachikoma Gateway listening on {host}:{port} (provider={os.environ.get('AI_PROVIDER', 'mock')})")
    ThreadingHTTPServer((host, port), GatewayHandler).serve_forever()


if __name__ == "__main__":
    main()
