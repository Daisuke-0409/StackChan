"""Step 2.7: cloud speech-to-text dry run.

Sends a short recorded audio clip to a configured cloud STT HTTP endpoint
and returns the recognized text. This is a dry run: the result is only
logged / returned to the caller. Nothing here is sent to Claude Code, and
nothing here applies an approval decision -- see voice_approval_gate.py for
the strictly-gated approve_once path (Step 2.8).

The API key is read only from the TACHIKOMA_STT_API_KEY environment
variable (or passed explicitly for tests). It is never logged, never
included in exception messages, and never written to disk. Errors are
raised as TranscriptionError with a fixed, safe message; the underlying
response body is never surfaced.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "whisper-1"
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_AUDIO_BYTES = 25 * 1024 * 1024

Opener = Callable[[urllib.request.Request, float], Any]


class TranscriptionError(Exception):
    """Safe, non-sensitive error. Never includes raw response bodies or keys."""


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    provider: str
    model: str


def _default_opener(request: urllib.request.Request, timeout_seconds: float) -> Any:
    return urllib.request.urlopen(request, timeout=timeout_seconds)


class CloudTranscriber:
    """Minimal OpenAI-compatible cloud STT client using only the stdlib."""

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        opener: Opener = _default_opener,
    ) -> None:
        resolved_key = api_key if api_key is not None else os.getenv("TACHIKOMA_STT_API_KEY", "")
        if not resolved_key:
            raise ValueError("TACHIKOMA_STT_API_KEY is required")
        self._api_key = resolved_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._opener = opener

    def transcribe(self, audio_bytes: bytes, *, filename: str = "audio.wav") -> TranscriptionResult:
        if not isinstance(audio_bytes, (bytes, bytearray)) or not audio_bytes:
            raise ValueError("audio_bytes must be non-empty bytes")
        if len(audio_bytes) > MAX_AUDIO_BYTES:
            raise ValueError("audio_bytes exceeds the maximum upload size")
        boundary = uuid.uuid4().hex
        body = _build_multipart_body(boundary, bytes(audio_bytes), filename, self._model)
        request = urllib.request.Request(
            f"{self._base_url}/audio/transcriptions",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        try:
            with self._opener(request, self._timeout_seconds) as response:
                raw_body = response.read()
            payload = json.loads(raw_body.decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise TranscriptionError("cloud STT request failed") from exc
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise TranscriptionError("cloud STT response did not contain text")
        return TranscriptionResult(text=text.strip(), provider=self._base_url, model=self._model)


def _build_multipart_body(boundary: str, audio_bytes: bytes, filename: str, model: str) -> bytes:
    parts = [
        f"--{boundary}\r\n".encode("utf-8"),
        b'Content-Disposition: form-data; name="model"\r\n\r\n',
        model.encode("utf-8") + b"\r\n",
        f"--{boundary}\r\n".encode("utf-8"),
        (
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8"),
        audio_bytes,
        b"\r\n",
        f"--{boundary}--\r\n".encode("utf-8"),
    ]
    return b"".join(parts)


class DryRunTranscriptionLog:
    """Logs recognized text only; never forwards it anywhere else."""

    def __init__(self, output_fn: Callable[[str], None] = print) -> None:
        self._output = output_fn

    def record(self, result: TranscriptionResult) -> None:
        self._output(f"[stt-dry-run] provider={result.provider} model={result.model} text={result.text}")


def run_dry_run(
    transcriber: CloudTranscriber,
    audio_path: str,
    log: DryRunTranscriptionLog,
) -> TranscriptionResult:
    """Read one recorded audio file, transcribe it, and log the text only."""
    with open(audio_path, "rb") as handle:
        audio_bytes = handle.read()
    result = transcriber.transcribe(audio_bytes, filename=os.path.basename(audio_path))
    log.record(result)
    return result


__all__ = [
    "CloudTranscriber",
    "DryRunTranscriptionLog",
    "TranscriptionError",
    "TranscriptionResult",
    "run_dry_run",
]
