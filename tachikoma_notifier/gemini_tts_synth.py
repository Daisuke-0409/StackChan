"""Step 7: synthesize speech via the Gemini API's native TTS models.

Same contract as voicebox_synth.py / voicevox_synth.py / windows_wave_synth.py:
text -> raw 16-bit mono PCM bytes at DEFAULT_SAMPLE_RATE, raising the *same*
SpeechSynthesisError type on any failure, so it drops into
StackChanSpeechSink's `synthesizer` parameter unchanged.

Verified live against a real API key on 2026-07-23:
  - Available TTS-capable models for this key (per ListModels,
    filtering for `generateContent` support): gemini-2.5-flash-preview-tts,
    gemini-2.5-pro-preview-tts, gemini-3.1-flash-tts-preview. Defaults to
    gemini-2.5-flash-preview-tts (the one named in the task that asked for
    this file; the other two are viable TACHIKOMA_GEMINI_TTS_MODEL swaps).
  - Request: POST {base_url}/models/{model}:generateContent with
    generationConfig.responseModalities=["AUDIO"] and a prebuilt voice
    name in speechConfig. A bare short phrase (e.g. just "こんにちは") can
    make the model answer conversationally in text instead of speaking it,
    so the input is wrapped in an explicit "read this verbatim" instruction
    before being sent -- confirmed this fixes it.
  - Response: JSON with candidates[0].content.parts[0].inlineData.data
    (base64) and .mimeType. Confirmed mimeType "audio/L16;codec=pcm;rate=24000"
    -- raw 16-bit PCM at exactly DEFAULT_SAMPLE_RATE, no WAV header and no
    resampling needed, just a straight base64 decode.

Voice: samples of Kore plus 7 alternates (Puck, Charon, Aoede, Leda,
Zephyr, Fenrir, Orus) were generated and compared by ear against real
Japanese text; "Zephyr" was chosen as the default from that comparison.
Override with TACHIKOMA_GEMINI_TTS_VOICE for a different character/style.

The API key is read only from TACHIKOMA_GEMINI_API_KEY (shared with
gemini_responder.py) and sent only as the `x-goog-api-key` header.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

from tachikoma_notifier.windows_wave_synth import DEFAULT_SAMPLE_RATE, SpeechSynthesisError

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-2.5-flash-preview-tts"
DEFAULT_VOICE_NAME = "Zephyr"
DEFAULT_TIMEOUT_SECONDS = 30.0
_READ_ALOUD_PREFIX = "次のテキストをそのまま読み上げてください: "

Opener = Callable[[urllib.request.Request, float], Any]


def _default_opener(request: urllib.request.Request, timeout_seconds: float) -> Any:
    return urllib.request.urlopen(request, timeout=timeout_seconds)


class GeminiTtsSynthesizer:
    """Callable text -> PCM synthesizer backed by a Gemini native-TTS model."""

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        model: Optional[str] = None,
        voice_name: Optional[str] = None,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        opener: Opener = _default_opener,
    ) -> None:
        resolved_key = api_key if api_key is not None else os.getenv("TACHIKOMA_GEMINI_API_KEY", "")
        if not resolved_key:
            raise ValueError("TACHIKOMA_GEMINI_API_KEY is required")
        self._api_key = resolved_key
        self._base_url = base_url.rstrip("/")
        self._model = model if model is not None else os.getenv("TACHIKOMA_GEMINI_TTS_MODEL", DEFAULT_MODEL)
        self._voice_name = (
            voice_name if voice_name is not None else os.getenv("TACHIKOMA_GEMINI_TTS_VOICE", DEFAULT_VOICE_NAME)
        )
        self._sample_rate = sample_rate
        self._timeout_seconds = timeout_seconds
        self._opener = opener

    def synthesize(self, text: str) -> bytes:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a non-empty string")

        body = json.dumps(
            {
                "contents": [{"role": "user", "parts": [{"text": _READ_ALOUD_PREFIX + text}]}],
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": self._voice_name}}},
                },
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}/models/{self._model}:generateContent",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "x-goog-api-key": self._api_key},
        )
        try:
            with self._opener(request, self._timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SpeechSynthesisError("gemini tts request failed") from exc

        try:
            inline_data = payload["candidates"][0]["content"]["parts"][0]["inlineData"]
            mime_type = inline_data["mimeType"]
            audio_b64 = inline_data["data"]
        except (KeyError, IndexError, TypeError) as exc:
            raise SpeechSynthesisError("gemini tts response did not contain audio data") from exc

        if "L16" not in mime_type or f"rate={self._sample_rate}" not in mime_type:
            raise SpeechSynthesisError("gemini tts returned an unexpected audio format")

        try:
            pcm = base64.b64decode(audio_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise SpeechSynthesisError("gemini tts returned invalid base64 audio") from exc

        if not pcm:
            raise SpeechSynthesisError("gemini tts audio is empty")
        return pcm


__all__ = ["DEFAULT_MODEL", "DEFAULT_VOICE_NAME", "GeminiTtsSynthesizer"]
