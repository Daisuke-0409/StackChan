"""Step 7: synthesize speech via the local Voicebox (qwen3-tts) service.

Mirrors windows_wave_synth.py's contract exactly (text -> raw 16-bit mono
PCM bytes at a fixed sample rate, raising the *same* SpeechSynthesisError
type on any failure) so it can be dropped into StackChanSpeechSink's
`synthesizer` parameter as a straight swap for the Windows SAPI path,
without touching stackchan_speech_sink.py at all.

Endpoint/request shape per user-provided spec (single POST, no separate
audio_query step, unlike the earlier VOICEVOX-style guess this file used to
implement):
  POST {base_url}/generate
  {"text": ..., "profile_id": ..., "language": "ja", "engine": "qwen3-tts"}

UNVERIFIED (flagged for review): `localhost:17493` was unreachable
(connection refused -- nothing listening, not just a missing /docs page)
from the machine this was written on, so this shape could not be checked
against the service's own /docs. Two things in particular are guesses:
  - The *response* shape: assumed to be the synthesized audio returned
    directly as WAV bytes in the response body (matching the "single POST,
    no separate fetch step" design implied by the spec). If the real
    service instead returns JSON (e.g. a base64 field or a follow-up URL),
    only `_extract_pcm`/`synthesize` need to change.
  - The `engine` value ("qwen3-tts") is passed through as given; no other
    engine values were provided to compare against.
Re-verify both against {base_url}/docs once the service is reachable.

The engine's own auth (if any) is read only from TACHIKOMA_VOICEBOX_API_KEY
(optional -- omit entirely for a local engine with no auth) and sent as a
Bearer Authorization header; it is never logged and never included in
exception messages.
"""
from __future__ import annotations

import io
import json
import os
import urllib.error
import urllib.request
import wave
from typing import Any, Callable, Optional

from tachikoma_notifier.windows_wave_synth import DEFAULT_SAMPLE_RATE, SpeechSynthesisError

DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_LANGUAGE = "ja"
DEFAULT_ENGINE = "qwen3-tts"

Opener = Callable[[urllib.request.Request, float], Any]


def _default_opener(request: urllib.request.Request, timeout_seconds: float) -> Any:
    return urllib.request.urlopen(request, timeout=timeout_seconds)


class VoiceboxSynthesizer:
    """Callable text -> PCM synthesizer backed by the local Voicebox /generate endpoint."""

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        profile_id: Optional[str] = None,
        language: str = DEFAULT_LANGUAGE,
        engine: str = DEFAULT_ENGINE,
        api_key: Optional[str] = None,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        opener: Opener = _default_opener,
    ) -> None:
        resolved_base_url = base_url if base_url is not None else os.getenv("TACHIKOMA_VOICEBOX_BASE_URL", "")
        resolved_profile_id = (
            profile_id if profile_id is not None else os.getenv("TACHIKOMA_VOICEBOX_PROFILE_ID", "")
        )
        if not resolved_base_url or not resolved_profile_id:
            raise ValueError("TACHIKOMA_VOICEBOX_BASE_URL and TACHIKOMA_VOICEBOX_PROFILE_ID are required")
        self._base_url = resolved_base_url.rstrip("/")
        self._profile_id = resolved_profile_id
        self._language = language
        self._engine = engine
        self._api_key = api_key if api_key is not None else os.getenv("TACHIKOMA_VOICEBOX_API_KEY", "")
        self._sample_rate = sample_rate
        self._timeout_seconds = timeout_seconds
        self._opener = opener

    def synthesize(self, text: str) -> bytes:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a non-empty string")

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        body = json.dumps(
            {
                "text": text,
                "profile_id": self._profile_id,
                "language": self._language,
                "engine": self._engine,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}/generate",
            data=body,
            method="POST",
            headers=headers,
        )
        try:
            with self._opener(request, self._timeout_seconds) as response:
                audio_bytes = response.read()
        except (urllib.error.URLError, OSError) as exc:
            raise SpeechSynthesisError("voicebox generate request failed") from exc

        return _extract_pcm(audio_bytes, expected_sample_rate=self._sample_rate)


def _extract_pcm(wav_bytes: bytes, *, expected_sample_rate: int) -> bytes:
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
            if wav_file.getnchannels() != 1 or wav_file.getsampwidth() != 2:
                raise SpeechSynthesisError("synthesized audio is not 16-bit mono PCM")
            if wav_file.getframerate() != expected_sample_rate:
                raise SpeechSynthesisError("synthesized audio sample rate does not match request")
            pcm = wav_file.readframes(wav_file.getnframes())
    except wave.Error as exc:
        raise SpeechSynthesisError("synthesized response is not a valid WAV file") from exc

    if not pcm:
        raise SpeechSynthesisError("synthesized audio is empty")
    return pcm


__all__ = ["VoiceboxSynthesizer"]
