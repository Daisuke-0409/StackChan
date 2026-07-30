"""Step 7: synthesize speech via a local VOICEVOX ENGINE instance.

Same contract as voicebox_synth.py and windows_wave_synth.py: text -> raw
16-bit mono PCM bytes at DEFAULT_SAMPLE_RATE, raising the *same*
SpeechSynthesisError type on any failure, so it drops into
StackChanSpeechSink's `synthesizer` parameter unchanged.

Verified live against a real VOICEVOX ENGINE instance (v0.25.2,
localhost:50021) on 2026-07-23: the standard two-step REST flow --
  1. POST {base_url}/audio_query?text=...&speaker=...  -> JSON audio query
     (outputSamplingRate defaults to 24000, outputStereo to false --
     already matching DEFAULT_SAMPLE_RATE/mono with no conversion needed)
  2. POST {base_url}/synthesis?speaker=...              -> WAV bytes
     directly (Content-Type: audio/wav), confirmed mono/16-bit/24000Hz.
Both calls completed in well under a second (CPU-only, no GPU needed) --
unlike voicebox_synth.py's Qwen3-TTS model, which took 35-130s per call on
this machine's CPU.

Default speaker is Zundamon / Normal (VOICEVOX style id 3, confirmed via
GET /speakers against this instance). Override with
TACHIKOMA_VOICEVOX_SPEAKER_ID for a different character/style (e.g. "2"
for Shikoku Metan / Normal, tried briefly before reverting to this).
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

DEFAULT_BASE_URL = "http://localhost:50021"
DEFAULT_SPEAKER_ID = "3"  # Zundamon / Normal
DEFAULT_TIMEOUT_SECONDS = 30.0

Opener = Callable[[urllib.request.Request, float], Any]


def _default_opener(request: urllib.request.Request, timeout_seconds: float) -> Any:
    return urllib.request.urlopen(request, timeout=timeout_seconds)


class VoicevoxSynthesizer:
    """Callable text -> PCM synthesizer backed by a local VOICEVOX ENGINE."""

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        speaker_id: Optional[str] = None,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        opener: Opener = _default_opener,
    ) -> None:
        resolved_base_url = base_url if base_url is not None else os.getenv("TACHIKOMA_VOICEVOX_BASE_URL", "")
        self._base_url = (resolved_base_url or DEFAULT_BASE_URL).rstrip("/")
        self._speaker_id = (
            speaker_id if speaker_id is not None else os.getenv("TACHIKOMA_VOICEVOX_SPEAKER_ID", DEFAULT_SPEAKER_ID)
        )
        self._sample_rate = sample_rate
        self._timeout_seconds = timeout_seconds
        self._opener = opener

    def synthesize(self, text: str) -> bytes:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a non-empty string")

        query_params = urllib.parse.urlencode({"text": text, "speaker": self._speaker_id})
        query_request = urllib.request.Request(f"{self._base_url}/audio_query?{query_params}", method="POST")
        try:
            with self._opener(query_request, self._timeout_seconds) as response:
                audio_query = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SpeechSynthesisError("voicevox audio_query request failed") from exc

        synth_params = urllib.parse.urlencode({"speaker": self._speaker_id})
        synth_request = urllib.request.Request(
            f"{self._base_url}/synthesis?{synth_params}",
            data=json.dumps(audio_query).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with self._opener(synth_request, self._timeout_seconds) as response:
                wav_bytes = response.read()
        except (urllib.error.URLError, OSError) as exc:
            raise SpeechSynthesisError("voicevox synthesis request failed") from exc

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


__all__ = ["DEFAULT_SPEAKER_ID", "VoicevoxSynthesizer"]
