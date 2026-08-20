"""A speech recogniser of our own, so the robot stops asking an LLM to listen.

Transcription is done today by handing audio to Gemini and asking it to
write down what it heard. That is a general model doing a side task, and
it shows: measured on 2026-08-20, every failure was a proper noun replaced
with a plausible ordinary word -- 加江田 became 楓, 佐土原 became サトワラ,
ダイボ石材の霊標管理表 became 第5セクターの冷却配管図表. It was not
mishearing them so much as reaching for words it knew. A recogniser does
not do that, because it is not trying to write sentences.

This is that recogniser, wearing the shape the gateway already speaks:
an OpenAI-style POST /v1/audio/transcriptions with a WAV, answering
{"text": ...}. So switching is three lines of .env and no code:

    STT_PROVIDER=local
    STT_PROVIDER_URL=http://127.0.0.1:9000/v1/audio/transcriptions
    STT_PROVIDER_API_KEY=<anything; see below>

Three things follow from running it here rather than calling out:

- The proper nouns can be given to the decoder as a bias rather than as an
  instruction. Whisper's initial_prompt nudges what it expects to hear;
  the Gemini prompt had to *tell* it to use spellings, and on audio it
  could not make out it did as told -- which is how 大輔 ended up at the
  end of sentences nobody said it in.
- The round trip to Google disappears from the wait.
- The audio never leaves the house. The project already keeps customer
  data from the model; this extends that to the voice itself.

Running it (home PC, once):

    pip install faster-whisper
    python -u gateway/local_stt.py

The model downloads on first run and is held in memory afterwards. On a
Ryzen mini PC with no NVIDIA card this is CPU inference, which is why the
default is a distilled Japanese model rather than large-v3.
"""
from __future__ import annotations

import http.server
import json
import os
import sys
import time

# Japanese-specialised distillation of large-v3, several times faster for
# close to the same Japanese accuracy. The whole complaint being answered
# here is Japanese proper nouns, so a Japanese model is the right default
# on a machine doing this on its CPU.
MODEL = os.environ.get("LOCAL_STT_MODEL", "kotoba-tech/kotoba-whisper-v2.0-faster")

# int8 on CPU. float16 needs a GPU this machine does not have.
COMPUTE_TYPE = os.environ.get("LOCAL_STT_COMPUTE_TYPE", "int8")
DEVICE = os.environ.get("LOCAL_STT_DEVICE", "cpu")

HOST = os.environ.get("LOCAL_STT_HOST", "127.0.0.1")
PORT = int(os.environ.get("LOCAL_STT_PORT", "9000"))

# The words this household and this job actually use. Whisper takes this as
# context for what it is about to hear -- a bias on the decoder, not an
# instruction to produce them. Empty is fine and simply removes the bias.
INITIAL_PROMPT = os.environ.get("LOCAL_STT_VOCABULARY", "")

# Refuses anything but the transcript. Whisper will happily hallucinate a
# closing sentence over silence, and these are the two guards that stop it.
VAD_FILTER = os.environ.get("LOCAL_STT_VAD", "1") == "1"

MAX_BODY_BYTES = int(os.environ.get("LOCAL_STT_MAX_BODY_BYTES", str(16 * 1024 * 1024)))

_model = None


def load_model():
    """Held for the life of the process: loading it costs seconds."""
    global _model
    if _model is None:
        from faster_whisper import WhisperModel  # deferred: heavy, and optional
        print(f"local_stt loading {MODEL} ({DEVICE}/{COMPUTE_TYPE})...", flush=True)
        started = time.time()
        _model = WhisperModel(MODEL, device=DEVICE, compute_type=COMPUTE_TYPE)
        print(f"local_stt ready in {(time.time() - started):.1f}s", flush=True)
    return _model


def transcribe_wav(wav_bytes: bytes) -> str:
    """The transcript, or "" when there was nothing to transcribe."""
    import io as _io

    segments, _info = load_model().transcribe(
        _io.BytesIO(wav_bytes),
        language="ja",             # never guess: this household speaks Japanese
        vad_filter=VAD_FILTER,
        initial_prompt=INITIAL_PROMPT or None,
        condition_on_previous_text=False,   # each utterance stands alone; carrying
                                            # context between them is how one bad
                                            # transcript poisons the next
    )
    return "".join(segment.text for segment in segments).strip()


def parse_multipart(body: bytes, boundary: str) -> bytes:
    """The WAV out of an OpenAI-style upload.

    Hand-rolled because this file has one caller with one shape, and
    pulling in a parser to read two fields would be more surface than the
    thing it parses.
    """
    marker = b"--" + boundary.encode("utf-8")
    for part in body.split(marker):
        head, _, payload = part.partition(b"\r\n\r\n")
        if b'name="file"' not in head:
            continue
        # The part ends with the CRLF that precedes the next boundary.
        return payload[:-2] if payload.endswith(b"\r\n") else payload
    raise ValueError("no file part")


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, {"ok": True, "model": MODEL, "loaded": _model is not None})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/v1/audio/transcriptions"):
            return self._send(404, {"error": "not found"})

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            return self._send(400, {"error": "bad content length"})
        content_type = self.headers.get("Content-Type", "")
        if "boundary=" not in content_type:
            return self._send(400, {"error": "expected multipart/form-data"})
        boundary = content_type.split("boundary=", 1)[1].strip().strip('"')

        try:
            wav = parse_multipart(self.rfile.read(length), boundary)
        except ValueError:
            return self._send(400, {"error": "no file part"})

        started = time.time()
        try:
            text = transcribe_wav(wav)
        except Exception as exc:  # noqa: BLE001 -- reported, never raised at the caller
            self.log_message("transcribe failed (%s)", type(exc).__name__)
            return self._send(500, {"error": "transcription failed"})

        # Length and duration, never the words. The transcript is the
        # person's speech, and a log on this machine is not where it goes.
        self.log_message("transcribed %d bytes in %dms -> %d chars",
                         len(wav), int((time.time() - started) * 1000), len(text))
        self._send(200, {"text": text})

    def log_request(self, code="-", size="-"):
        pass  # do_POST logs what is safe to log

    def log_message(self, fmt, *args):
        sys.stderr.write("[local_stt] " + (fmt % args) + "\n")


class _SingleInstanceServer(http.server.ThreadingHTTPServer):
    """Refuses to start when the port is already taken.

    Python turns SO_REUSEADDR on by default, and on Windows a second
    process can then bind a port the first is already listening on, with
    requests split between them at random -- so a restarted server appears
    not to have picked up its new settings. Bitten three times in this
    repo already; not a fourth.
    """

    allow_reuse_address = False


def main():
    load_model()   # fail loudly at startup, not on the first thing anybody says
    print(f"local_stt listening on {HOST}:{PORT}", flush=True)
    print("point the gateway at "
          f"http://{HOST}:{PORT}/v1/audio/transcriptions", flush=True)
    try:
        server = _SingleInstanceServer((HOST, PORT), Handler)
    except OSError as exc:
        raise SystemExit(f"local_stt could not take {HOST}:{PORT} ({exc}). "
                         "Another one is probably already running.") from exc
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
