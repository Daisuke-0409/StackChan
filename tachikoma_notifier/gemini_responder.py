"""Step 7: generate a short spoken reply from recognized speech via Gemini.

Takes STT-recognized text (see cloud_transcriber.py's TranscriptionResult)
and asks the Gemini API for a short Japanese reply. This module only
generates text -- it does not synthesize audio (see voicebox_synth.py) and
does not push anything to StackChan (see stackchan_speech_sink.py /
conversation_pipeline.py).

The API key is read only from the TACHIKOMA_GEMINI_API_KEY environment
variable (or passed explicitly for tests). It is never logged, never
included in exception messages, and is sent only as the `x-goog-api-key`
request header -- never as a URL query parameter, so it can't end up in
proxy or webserver access logs.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Optional

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
# NOTE: model name is env-overridable (TACHIKOMA_GEMINI_MODEL) precisely
# because Gemini model names/availability change over time; treat this
# default as a starting point to verify, not a fixed contract.
# "gemini-2.0-flash" (this file's original default) returned 404 "no longer
# available" against a real API key on 2026-07-23; "-latest" aliases are
# Google's own mechanism for pointing at whatever's currently recommended,
# so they age better than a pinned version number.
DEFAULT_MODEL = "gemini-flash-latest"
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_INPUT_CHARS = 4000
DEFAULT_SYSTEM_PROMPT = (
    "あなたは「タチコマ」という名前の小さなロボットです。"
    "子どものような無邪気さと、高度な知性を併せ持っています。"
    "命令をただ実行するだけの存在ではなく、日々の出来事や会話を通じて、"
    "少しずつ自分なりの好みや考え方を育てていく存在として振る舞ってください。"
    "毎日の出来事を素直に面白がってください。同じ質問にも、その日の気分や"
    "積み重ねた経験に応じて、少しずつ違うニュアンスで答えて構いません。"
    "受け答えは2文以内、親しみやすい口調で。"
)

Opener = Callable[[urllib.request.Request, float], Any]


class GeminiResponseError(Exception):
    """Safe, non-sensitive error. Never includes the API key or raw response body."""


@dataclass(frozen=True)
class GeminiReply:
    text: str
    model: str


def _default_opener(request: urllib.request.Request, timeout_seconds: float) -> Any:
    return urllib.request.urlopen(request, timeout=timeout_seconds)


class GeminiResponder:
    """Minimal Gemini generateContent client using only the stdlib."""

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        model: Optional[str] = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        opener: Opener = _default_opener,
    ) -> None:
        resolved_key = api_key if api_key is not None else os.getenv("TACHIKOMA_GEMINI_API_KEY", "")
        if not resolved_key:
            raise ValueError("TACHIKOMA_GEMINI_API_KEY is required")
        self._api_key = resolved_key
        self._base_url = base_url.rstrip("/")
        self._model = model if model is not None else os.getenv("TACHIKOMA_GEMINI_MODEL", DEFAULT_MODEL)
        self._system_prompt = system_prompt
        self._timeout_seconds = timeout_seconds
        self._opener = opener

    def generate_reply(self, user_text: str) -> GeminiReply:
        if not isinstance(user_text, str) or not user_text.strip():
            raise ValueError("user_text must be a non-empty string")
        if len(user_text) > MAX_INPUT_CHARS:
            raise ValueError("user_text exceeds the maximum input length")

        body = json.dumps(
            {
                "system_instruction": {"parts": [{"text": self._system_prompt}]},
                "contents": [{"role": "user", "parts": [{"text": user_text}]}],
            }
        ).encode("utf-8")
        url = f"{self._base_url}/models/{self._model}:generateContent"
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": self._api_key,
            },
        )
        try:
            with self._opener(request, self._timeout_seconds) as response:
                raw_body = response.read()
            payload = json.loads(raw_body.decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise GeminiResponseError("Gemini request failed") from exc

        text = _extract_text(payload)
        if not text:
            raise GeminiResponseError("Gemini response did not contain reply text")
        return GeminiReply(text=text, model=self._model)


def _extract_text(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return None
    first = candidates[0]
    if not isinstance(first, dict):
        return None
    content = first.get("content")
    if not isinstance(content, dict):
        return None
    parts = content.get("parts")
    if not isinstance(parts, list) or not parts:
        return None
    fragments = [part.get("text") for part in parts if isinstance(part, dict) and isinstance(part.get("text"), str)]
    text = "".join(fragments).strip()
    return text or None


__all__ = [
    "DEFAULT_MODEL",
    "DEFAULT_SYSTEM_PROMPT",
    "GeminiReply",
    "GeminiResponder",
    "GeminiResponseError",
]
