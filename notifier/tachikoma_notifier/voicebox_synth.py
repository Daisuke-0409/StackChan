"""Step 7: synthesize speech via the local Voicebox service.

Mirrors windows_wave_synth.py's contract exactly (text -> raw 16-bit mono
PCM bytes at a fixed sample rate, raising the *same* SpeechSynthesisError
type on any failure) so it can be dropped into StackChanSpeechSink's
`synthesizer` parameter as a straight swap for the Windows SAPI path,
without touching stackchan_speech_sink.py at all.

Verified live against a running Voicebox instance (its own /docs /
openapi.json) on 2026-07-23. The real flow is three calls, not one:
  1. POST {base_url}/generate {"text", "profile_id", "language", "engine"}
     -> JSON GenerationResponse with an "id" and "status" ("loading_model"
     -> "generating" -> "completed"/"failed"). Generation is asynchronous;
     a real first call (with a cold model) took ~35s.
  2. Poll GET {base_url}/history/{id} until status is "completed" (raise on
     "failed" or on exceeding poll_timeout_seconds).
  3. GET {base_url}/audio/{id} -> the synthesized audio as WAV bytes
     directly (Content-Type: audio/wav), confirmed mono/16-bit/24000Hz --
     matching DEFAULT_SAMPLE_RATE, so no resampling is needed.

Valid `engine` values per the live schema are `qwen`, `qwen_custom_voice`,
`luxtts`, `chatterbox`, `chatterbox_turbo`, `tada`, `kokoro` (default
`qwen`) -- NOT `qwen3-tts`, which this file originally (incorrectly)
assumed before the service was reachable.

The engine's own auth (if any) is read only from TACHIKOMA_VOICEBOX_API_KEY
(optional -- omit entirely for a local engine with no auth) and sent as a
Bearer Authorization header; it is never logged and never included in
exception messages.
"""
from __future__ import annotations

import io
import json
import os
import time
import urllib.error
import urllib.request
import wave
from typing import Any, Callable, Optional

from tachikoma_notifier.windows_wave_synth import DEFAULT_SAMPLE_RATE, SpeechSynthesisError

DEFAULT_REQUEST_TIMEOUT_SECONDS = 20.0
DEFAULT_POLL_INTERVAL_SECONDS = 2.0
DEFAULT_POLL_TIMEOUT_SECONDS = 120.0
DEFAULT_LANGUAGE = "ja"
DEFAULT_ENGINE = "qwen"

Opener = Callable[[urllib.request.Request, float], Any]
SleepFn = Callable[[float], None]


def _default_opener(request: urllib.request.Request, timeout_seconds: float) -> Any:
    return urllib.request.urlopen(request, timeout=timeout_seconds)


class VoiceboxSynthesizer:
    """Callable text -> PCM synthesizer backed by the local Voicebox service."""

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        profile_id: Optional[str] = None,
        language: str = DEFAULT_LANGUAGE,
        engine: str = DEFAULT_ENGINE,
        api_key: Optional[str] = None,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        poll_timeout_seconds: float = DEFAULT_POLL_TIMEOUT_SECONDS,
        opener: Opener = _default_opener,
        sleep_fn: SleepFn = time.sleep,
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
        self._request_timeout_seconds = request_timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._poll_timeout_seconds = poll_timeout_seconds
        self._opener = opener
        self._sleep_fn = sleep_fn

    def _headers(self, extra: Optional[dict] = None) -> dict:
        headers = dict(extra or {})
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _get_json(self, url: str) -> Any:
        request = urllib.request.Request(url, method="GET", headers=self._headers())
        with self._opener(request, self._request_timeout_seconds) as response:
            raw = response.read()
        return json.loads(raw.decode("utf-8"))

    def synthesize(self, text: str) -> bytes:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a non-empty string")

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
            headers=self._headers({"Content-Type": "application/json"}),
        )
        try:
            with self._opener(request, self._request_timeout_seconds) as response:
                generation = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SpeechSynthesisError("voicebox generate request failed") from exc

        generation_id = generation.get("id")
        if not generation_id:
            raise SpeechSynthesisError("voicebox generate response did not contain an id")

        status = generation.get("status")
        waited_seconds = 0.0
        try:
            while status not in ("completed", "failed"):
                if waited_seconds >= self._poll_timeout_seconds:
                    raise SpeechSynthesisError("voicebox generation did not complete in time")
                self._sleep_fn(self._poll_interval_seconds)
                waited_seconds += self._poll_interval_seconds
                generation = self._get_json(f"{self._base_url}/history/{generation_id}")
                status = generation.get("status")
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SpeechSynthesisError("voicebox generation status check failed") from exc

        if status != "completed":
            raise SpeechSynthesisError("voicebox generation failed")

        audio_request = urllib.request.Request(f"{self._base_url}/audio/{generation_id}", headers=self._headers())
        try:
            with self._opener(audio_request, self._request_timeout_seconds) as response:
                audio_bytes = response.read()
        except (urllib.error.URLError, OSError) as exc:
            raise SpeechSynthesisError("voicebox audio fetch failed") from exc

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
