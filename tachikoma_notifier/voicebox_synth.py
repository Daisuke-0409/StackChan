"""Step 7: synthesize speech via a self-hosted VOICEVOX-compatible Voicebox engine.

Mirrors windows_wave_synth.py's contract exactly (text -> raw 16-bit mono
PCM bytes at a fixed sample rate, raising the *same* SpeechSynthesisError
type on any failure) so it can be dropped into StackChanSpeechSink's
`synthesizer` parameter as a straight swap for the Windows SAPI path,
without touching stackchan_speech_sink.py at all.

ASSUMPTION (flagged for review): "Voicebox" here is treated as a
VOICEVOX-Engine-API-compatible server (the common self-hosted Japanese TTS
engine family that also underlies most "voice clone" character-voice
setups): a two-step REST flow --
  1. POST {base_url}/audio_query?text=...&speaker=...   -> JSON audio query
  2. POST {base_url}/synthesis?speaker=...               -> WAV bytes
No such server was reachable from this machine while writing this module,
so the exact request/response shape is unverified against the real
endpoint. If the real Voicebox service's API differs, only this file
should need to change.

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
import urllib.parse
import urllib.request
import wave
from typing import Any, Callable, Optional

from tachikoma_notifier.windows_wave_synth import DEFAULT_SAMPLE_RATE, SpeechSynthesisError

DEFAULT_TIMEOUT_SECONDS = 20.0

Opener = Callable[[urllib.request.Request, float], Any]


def _default_opener(request: urllib.request.Request, timeout_seconds: float) -> Any:
    return urllib.request.urlopen(request, timeout=timeout_seconds)


class VoiceboxSynthesizer:
    """Callable text -> PCM synthesizer backed by a Voicebox/VOICEVOX-style REST engine."""

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        speaker_id: Optional[str] = None,
        api_key: Optional[str] = None,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        opener: Opener = _default_opener,
    ) -> None:
        resolved_base_url = base_url if base_url is not None else os.getenv("TACHIKOMA_VOICEBOX_BASE_URL", "")
        resolved_speaker_id = (
            speaker_id if speaker_id is not None else os.getenv("TACHIKOMA_VOICEBOX_SPEAKER_ID", "")
        )
        if not resolved_base_url or not resolved_speaker_id:
            raise ValueError("TACHIKOMA_VOICEBOX_BASE_URL and TACHIKOMA_VOICEBOX_SPEAKER_ID are required")
        self._base_url = resolved_base_url.rstrip("/")
        self._speaker_id = resolved_speaker_id
        self._api_key = api_key if api_key is not None else os.getenv("TACHIKOMA_VOICEBOX_API_KEY", "")
        self._sample_rate = sample_rate
        self._timeout_seconds = timeout_seconds
        self._opener = opener

    def _headers(self, extra: Optional[dict] = None) -> dict:
        headers = dict(extra or {})
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def synthesize(self, text: str) -> bytes:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a non-empty string")

        query_params = urllib.parse.urlencode({"text": text, "speaker": self._speaker_id})
        query_request = urllib.request.Request(
            f"{self._base_url}/audio_query?{query_params}",
            method="POST",
            headers=self._headers(),
        )
        try:
            with self._opener(query_request, self._timeout_seconds) as response:
                audio_query_raw = response.read()
            audio_query = json.loads(audio_query_raw.decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SpeechSynthesisError("voicebox audio_query request failed") from exc

        synth_params = urllib.parse.urlencode({"speaker": self._speaker_id})
        synth_request = urllib.request.Request(
            f"{self._base_url}/synthesis?{synth_params}",
            data=json.dumps(audio_query).encode("utf-8"),
            method="POST",
            headers=self._headers({"Content-Type": "application/json"}),
        )
        try:
            with self._opener(synth_request, self._timeout_seconds) as response:
                wav_bytes = response.read()
        except (urllib.error.URLError, OSError) as exc:
            raise SpeechSynthesisError("voicebox synthesis request failed") from exc

        return _extract_pcm(wav_bytes, expected_sample_rate=self._sample_rate)


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
