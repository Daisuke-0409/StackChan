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
import datetime
import json
import math
import os
import ssl
import struct
import tempfile
import threading
import time
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
MIN_TRANSCRIBE_AUDIO_SECONDS = 0.5  # below this, skip STT entirely and treat as "didn't hear
                                    # anything" -- confirmed live: Gemini's STT confidently
                                    # hallucinates a fluent, plausible-sounding but entirely
                                    # fabricated sentence for near-empty audio instead of
                                    # admitting it heard nothing, so a low-confidence real
                                    # result isn't the failure mode being guarded against here

# --- TEMPORARY debug instrumentation (2026-07-24, "STT sounds wrong" /
# latency investigation) ---------------------------------------------------
# Off by default: recognized speech content and raw mic audio are personal
# conversation data, so this must never log or save anything unless a human
# explicitly opts in for one debugging session. Remove this block (and its
# call sites in process_transcribe/_log) once the investigation is done.
def _debug_logging_enabled(env: dict[str, str]) -> bool:
    return env.get("TACHIKOMA_DEBUG_LOGGING") == "1"


DEBUG_AUDIO_DIR = os.environ.get(
    "TACHIKOMA_DEBUG_AUDIO_DIR", os.path.join(tempfile.gettempdir(), "tachikoma_debug_audio")
)
# --- end temporary debug instrumentation block (see other markers below) --


def _log(message: str) -> None:
    """Prints one gateway log line with a wall-clock timestamp, so it can be
    correlated against the device's own epoch-ms log timestamps. Replaces
    bare print() for all gateway logging (access log, streaming/enqueue
    diagnostics); never includes conversation content by default.
    """
    print(f"[{datetime.datetime.now().isoformat(timespec='milliseconds')}] {message}")

# Each device_id maps to an ordered list, drained front-first by
# dequeue_speech(). enqueue_speech() defaults to *replacing* that list with
# a single new item -- the original Phase 5 contract POST /v1/speak and its
# callers (StackChanSpeechSink) still rely on ("only the latest pending
# announcement is kept"), verified against real hardware and covered by
# test_second_enqueue_replaces_first_pending_one. append=True is additive
# only, used by the streaming chat->TTS path (see
# _gemini_stream_chat_and_speak) to queue several sentences in speaking
# order without one clobbering the last.
_speech_queue: dict[str, list[bytes]] = {}
_speech_queue_lock = threading.Lock()


def enqueue_speech(device_id: str, audio: bytes, *, append: bool = False) -> tuple[int, dict[str, Any]]:
    """Store a pending announcement for device_id.

    append=False (default): replaces any existing queue for this device
    with just this one item -- the original single-slot behavior.
    append=True: adds to the end of the existing queue instead.
    """
    if not isinstance(device_id, str) or not device_id:
        return _result(400, "invalid_input")
    if not isinstance(audio, (bytes, bytearray)) or not audio or len(audio) % 2 != 0:
        return _result(400, "invalid_input")
    if len(audio) > MAX_SPEECH_AUDIO_BYTES:
        return _result(413, "invalid_input")
    with _speech_queue_lock:
        if append and device_id in _speech_queue:
            _speech_queue[device_id].append(bytes(audio))
        else:
            _speech_queue[device_id] = [bytes(audio)]
    return 200, {"ok": True}


def dequeue_speech(device_id: str) -> Optional[bytes]:
    """Pop and return the oldest pending announcement for device_id, if any."""
    with _speech_queue_lock:
        pending = _speech_queue.get(device_id)
        if not pending:
            _speech_queue.pop(device_id, None)
            return None
        audio = pending.pop(0)
        if not pending:
            del _speech_queue[device_id]
        return audio


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
GEMINI_DEFAULT_STT_MODEL = "gemini-flash-latest"  # gemini-2.5-flash 404'd ("no longer available to
                                                   # new users") when verified live 2026-07-24; -latest
                                                   # tracks whatever Google currently recommends
GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
_GEMINI_STT_PROMPT = (
    "次の音声を一字一句そのまま日本語で書き起こしてください。"
    "書き起こしたテキストのみを返し、説明や前置きは付けないでください。"
)
GEMINI_SYSTEM_PROMPT = "日本語で、簡潔に答えてください。"
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


GEMINI_SENTENCE_DELIMITERS = "。！？!?"
GEMINI_MIN_SENTENCE_CHARS = 3


def _gemini_streaming_enabled(env: dict[str, str]) -> bool:
    return env.get("GEMINI_STREAMING", "1") != "0"


def _extract_ready_sentences(pending: str) -> tuple[list[str], str]:
    """Splits pending into zero or more sentences ready to speak, plus the
    remaining unflushed tail (kept for the next call, or for a final
    end-of-stream flush by the caller).

    A candidate ending at a delimiter is held back -- merged into whatever
    follows -- while it is at most GEMINI_MIN_SENTENCE_CHARS characters
    (stripped), so a fragment like "はい。" gets combined with the next
    sentence instead of triggering its own (wasted) TTS call. Pure and
    network-free so the splitting logic is unit-testable on its own.
    """
    sentences: list[str] = []
    start = 0
    for i, ch in enumerate(pending):
        if ch not in GEMINI_SENTENCE_DELIMITERS:
            continue
        candidate = pending[start:i + 1]
        if len(candidate.strip()) > GEMINI_MIN_SENTENCE_CHARS:
            sentences.append(candidate)
            start = i + 1
        # else: too short on its own -- left in place so it merges into
        # whatever candidate is found at the next delimiter.
    return sentences, pending[start:]


def _iter_gemini_sse_text_deltas(response: Any):
    """Yields each incremental text delta from a streamGenerateContent SSE
    response, in arrival order. Lines that aren't a well-formed `data: {...}`
    event, or don't carry a text part (e.g. a bare finishReason chunk), are
    silently skipped -- a stream is expected to contain a mix of these.
    """
    for raw_line in response:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        chunk_str = line[len("data:"):].strip()
        if not chunk_str or chunk_str == "[DONE]":
            continue
        try:
            chunk = json.loads(chunk_str)
            delta = chunk["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            continue
        if isinstance(delta, str) and delta:
            yield delta


def _gemini_stream_chat_and_speak(text: str, payload: dict[str, Any],
                                  env: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """Streaming counterpart to _gemini_chat_response() + the TTS/enqueue
    block in process_chat(): as Gemini's reply streams in, each completed
    sentence is synthesized and enqueued immediately (in speaking order,
    via enqueue_speech(..., append=True)) instead of waiting for the full
    reply before any audio exists at all. Returns the same (status, body)
    shape as the non-streaming path, so process_chat() doesn't need to know
    which one ran.

    A sentence's TTS failure is logged and skipped, not fatal -- later
    sentences still get their turn. Only a total failure (no sentence ever
    enqueued) falls back to the fixed confirmation tone, matching the
    non-streaming path's existing behavior.
    """
    key = env.get("AI_PROVIDER_API_KEY", "")
    if not key:
        return _result(503, "server_error")
    device_id = payload["device_id"]
    model = env.get("AI_PROVIDER_MODEL", GEMINI_DEFAULT_CHAT_MODEL)
    url = f"{GEMINI_API_BASE_URL}/models/{model}:streamGenerateContent?alt=sse"
    request_body = json.dumps({
        "system_instruction": {"parts": [{"text": GEMINI_SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": text}]}],
    }).encode("utf-8")
    request = urllib.request.Request(url, data=request_body, method="POST", headers={
        "Content-Type": "application/json", "x-goog-api-key": key,
    })

    t_start = time.monotonic()
    full_text_parts: list[str] = []
    enqueued_count = 0
    sentence_index = 0

    def flush_sentence(sentence: str) -> None:
        nonlocal enqueued_count, sentence_index
        sentence = sentence.strip()
        if not sentence:
            return
        sentence_index += 1
        idx = sentence_index
        t_ready = time.monotonic()
        pcm = _gemini_tts_pcm(sentence, env)
        t_tts = time.monotonic()
        if pcm is None:
            _log(f"gateway streaming sentence={idx} tts_failed text_len={len(sentence)} "
                 f"sentence_ready_ms={(t_ready - t_start) * 1000:.0f} "
                 f"tts_ms={(t_tts - t_ready) * 1000:.0f}")
            return
        enqueue_status, enqueue_body = enqueue_speech(device_id, pcm, append=enqueued_count > 0)
        t_enqueue = time.monotonic()
        if enqueue_status != 200:
            _log(f"gateway streaming sentence={idx} enqueue_failed status={enqueue_status} "
                 f"error={enqueue_body.get('error')} pcm_bytes={len(pcm)}")
            return
        enqueued_count += 1
        _log(f"gateway streaming sentence={idx} text_len={len(sentence)} pcm_bytes={len(pcm)} "
             f"sentence_ready_ms={(t_ready - t_start) * 1000:.0f} "
             f"tts_ms={(t_tts - t_ready) * 1000:.0f} "
             f"enqueue_ms={(t_enqueue - t_tts) * 1000:.0f} "
             f"total_ms={(t_enqueue - t_start) * 1000:.0f}")

    pending = ""
    first_chunk_logged = False
    try:
        with urllib.request.urlopen(request, timeout=float(env.get("AI_PROVIDER_TIMEOUT_SECONDS", "30")),
                                    context=ssl.create_default_context()) as response:
            for delta in _iter_gemini_sse_text_deltas(response):
                if not first_chunk_logged:
                    first_chunk_logged = True
                    _log(f"gateway streaming first_chunk_ms={(time.monotonic() - t_start) * 1000:.0f}")
                full_text_parts.append(delta)
                pending += delta
                ready, pending = _extract_ready_sentences(pending)
                for sentence in ready:
                    flush_sentence(sentence)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return _result(502, "authentication_failed")
        if exc.code == 429:
            return _result(503, "rate_limited")
        return _result(502, "server_error")
    except (urllib.error.URLError, TimeoutError, ValueError):
        return _result(504, "timeout")

    if pending.strip():
        flush_sentence(pending)

    full_text = "".join(full_text_parts).strip()
    if not full_text:
        return _result(502, "invalid_response")
    full_text = full_text[:MAX_OUTPUT_BYTES]

    if enqueued_count == 0:
        # Every sentence's TTS (or the stream itself) failed -- same
        # fallback the non-streaming path uses so /v1/chat still produces
        # *something* audible rather than silence with no explanation.
        enqueue_speech(device_id, _generate_beep_pcm())

    return 200, {"text": full_text, "request_id": payload["request_id"],
                 "session_id": payload["session_id"], "is_final": True}


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

    provider = env.get("AI_PROVIDER", "mock").lower()
    if provider == "gemini" and _gemini_streaming_enabled(env):
        # Owns TTS/enqueue itself (per completed sentence, as they arrive)
        # instead of the single after-the-fact block below -- see
        # _gemini_stream_chat_and_speak's docstring.
        return _gemini_stream_chat_and_speak(text, payload, env)

    status, body = _provider_response(text, payload, env)
    if status == 200:
        pcm = None
        if provider == "gemini":
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
            _log(f"gateway enqueue_speech failed status={enqueue_status} "
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


def _gemini_stt_text(pcm: bytes, sample_rate: int, env: dict[str, str]) -> Optional[str]:
    """Transcribe via Gemini's audio-understanding input; returns None (never
    raises) on any failure. Reuses AI_PROVIDER_API_KEY -- the same Gemini
    project/key already configured for chat and TTS, not a separate
    STT-specific credential. Wraps the device's raw PCM in a WAV container
    (_pcm_to_wav, already used for the generic OpenAI-compatible path below)
    and sends it as inlineData alongside a "transcribe verbatim" instruction,
    the same read-aloud-style workaround _gemini_tts_pcm uses in reverse.
    """
    key = env.get("AI_PROVIDER_API_KEY", "")
    if not key:
        return None
    model = env.get("GEMINI_STT_MODEL", GEMINI_DEFAULT_STT_MODEL)
    url = f"{GEMINI_API_BASE_URL}/models/{model}:generateContent"
    wav_b64 = base64.b64encode(_pcm_to_wav(pcm, sample_rate)).decode("ascii")
    request_body = json.dumps({
        "contents": [{"role": "user", "parts": [
            {"text": _GEMINI_STT_PROMPT},
            {"inlineData": {"mimeType": "audio/wav", "data": wav_b64}},
        ]}],
    }).encode("utf-8")
    request = urllib.request.Request(url, data=request_body, method="POST", headers={
        "Content-Type": "application/json", "x-goog-api-key": key,
    })
    try:
        with urllib.request.urlopen(request, timeout=float(env.get("AI_PROVIDER_TIMEOUT_SECONDS", "30")),
                                    context=ssl.create_default_context()) as response:
            decoded = json.loads(response.read(MAX_INPUT_BYTES * 4 + 1024).decode("utf-8"))
        text = decoded["candidates"][0]["content"]["parts"][0]["text"]
        text = text.strip() if isinstance(text, str) else ""
        return text or None
    except Exception:
        return None


def _stt_response(pcm: bytes, sample_rate: int, env: dict[str, str]) -> tuple[int, dict[str, Any]]:
    provider = env.get("STT_PROVIDER", "mock").lower()
    if provider == "mock":
        text = env.get("MOCK_TRANSCRIPTION", "こんにちは")
        return 200, {"text": text[:MAX_INPUT_BYTES]}
    if provider == "gemini":
        text = _gemini_stt_text(pcm, sample_rate, env)
        if text is None:
            return _result(502, "invalid_response")
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

    debug = _debug_logging_enabled(env)  # TEMPORARY, see DEBUG_AUDIO_DIR block near the top of this file
    if debug:
        try:
            os.makedirs(DEBUG_AUDIO_DIR, exist_ok=True)
            filename = f"transcribe_{datetime.datetime.now().strftime('%Y%m%dT%H%M%S%f')}.wav"
            path = os.path.join(DEBUG_AUDIO_DIR, filename)
            with open(path, "wb") as f:
                f.write(_pcm_to_wav(bytes(audio), sample_rate))
            _log(f"gateway debug saved uploaded audio to {path} (sample_rate={sample_rate} "
                 f"bytes={len(audio)} duration_s={len(audio) / 2 / sample_rate:.2f})")
        except OSError as exc:
            _log(f"gateway debug failed to save uploaded audio: {type(exc).__name__}")

    duration_s = len(audio) / 2 / sample_rate
    if duration_s < MIN_TRANSCRIBE_AUDIO_SECONDS:
        # Too little audio to plausibly contain speech: skip the STT call
        # entirely rather than risk a confidently-fabricated transcription.
        # {"text": ""} is not a special case for the device -- it already
        # rejects an empty "text" field as InvalidResponse
        # (VoiceInputController::UploadAndTranscribe), the same failure path
        # a real STT error takes, so no firmware change is needed for this.
        if debug:
            _log(f"gateway debug transcribe skipped: duration_s={duration_s:.2f} "
                 f"< {MIN_TRANSCRIBE_AUDIO_SECONDS}s minimum")
        return 200, {"text": ""}

    t_stt_start = time.monotonic()
    status, body = _stt_response(bytes(audio), sample_rate, env)
    if debug:
        stt_ms = (time.monotonic() - t_stt_start) * 1000
        detail = repr(body.get("text")) if status == 200 else body.get("error")
        _log(f"gateway debug transcribe status={status} stt_ms={stt_ms:.0f} text={detail}")
    return status, body


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
            debug = _debug_logging_enabled(os.environ)  # TEMPORARY, see DEBUG_AUDIO_DIR block
            t_upload_start = time.monotonic() if debug else None
            try:
                length = min(int(self.headers.get("Content-Length", "0")), MAX_TRANSCRIBE_AUDIO_BYTES + 1024)
                if length <= 0:
                    raise ValueError("missing body")
                audio = self.rfile.read(length)
                sample_rate = int(self.headers.get("X-Sample-Rate", "16000"))
            except ValueError:
                self._send(400, {"error": "invalid_input"})
                return
            if debug:
                _log(f"gateway debug upload_ms={(time.monotonic() - t_upload_start) * 1000:.0f} "
                     f"bytes={len(audio)} sample_rate={sample_rate}")
            status, body = process_transcribe(audio, dict(self.headers), os.environ, sample_rate=sample_rate)
            self._send(status, body)
        else:
            self._send(404, {"error": "not_found"})

    def log_message(self, fmt: str, *args: Any) -> None:
        # Never print Authorization headers, provider keys, or full prompts.
        _log(f"gateway {self.command} {self.path} {args[1] if len(args) > 1 else ''}")


def main() -> None:
    host = os.environ.get("GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("GATEWAY_PORT", "8080"))
    _log(f"Tachikoma Gateway listening on {host}:{port} (provider={os.environ.get('AI_PROVIDER', 'mock')})")
    if _debug_logging_enabled(os.environ):
        _log(f"TACHIKOMA_DEBUG_LOGGING=1: recognized speech text will be logged and uploaded audio "
             f"saved to {DEBUG_AUDIO_DIR} -- investigation-only, disable when done")
    ThreadingHTTPServer((host, port), GatewayHandler).serve_forever()


if __name__ == "__main__":
    main()
