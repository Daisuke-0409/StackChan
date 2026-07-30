"""StackChanSpeechSink: push synthesized speech audio to the physical robot.

Reuses the existing SpeechSink abstraction (see notifier.py's SpeechSink /
WindowsSpeechSink / LogSpeechSink) so voice_announce.build_speech_announcer()
can switch between the PC's own speakers (WindowsSpeechSink, for
development) and the physical robot's speaker (this class, for production)
without changing anything else in the approval/relay pipeline.

Audio delivery goes through the Tachikoma Gateway's speech queue
(POST /v1/speak on the StackChan firmware repo's gateway server), a
separate endpoint from the existing chat request/response flow that carries
no conversation history or provider credentials -- just one device's
pending PCM bytes, delivered once.

The device token is read only from TACHIKOMA_STACKCHAN_DEVICE_TOKEN (or
passed explicitly for tests); it is never logged and never appears in the
request body, only in the Authorization header.
"""
from __future__ import annotations

import os
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

from tachikoma_notifier.notifier import SpeechSink
from tachikoma_notifier.windows_wave_synth import SpeechSynthesisError, synthesize_wav_pcm

DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_SAMPLE_RATE = 24000

Opener = Callable[[urllib.request.Request, float], Any]
Synthesizer = Callable[[str], bytes]


def _default_opener(request: urllib.request.Request, timeout_seconds: float) -> Any:
    return urllib.request.urlopen(request, timeout=timeout_seconds)


class StackChanSpeechSink(SpeechSink):
    """SpeechSink that plays a phrase on the physical StackChan speaker.

    Matches the SpeechSink.speak(phrase: str) -> bool contract, so it can be
    passed directly wherever WindowsSpeechSink / LogSpeechSink is used
    today, including voice_announce.build_speech_announcer().
    """

    def __init__(
        self,
        *,
        endpoint: Optional[str] = None,
        device_token: Optional[str] = None,
        device_id: Optional[str] = None,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        synthesizer: Optional[Synthesizer] = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        opener: Opener = _default_opener,
        fallback: Optional[SpeechSink] = None,
    ) -> None:
        resolved_endpoint = endpoint if endpoint is not None else os.getenv("TACHIKOMA_STACKCHAN_SPEAK_URL", "")
        resolved_token = (
            device_token if device_token is not None else os.getenv("TACHIKOMA_STACKCHAN_DEVICE_TOKEN", "")
        )
        resolved_device_id = (
            device_id if device_id is not None else os.getenv("TACHIKOMA_STACKCHAN_DEVICE_ID", "")
        )
        if not resolved_endpoint or not resolved_token or not resolved_device_id:
            raise ValueError(
                "TACHIKOMA_STACKCHAN_SPEAK_URL, TACHIKOMA_STACKCHAN_DEVICE_TOKEN, "
                "and TACHIKOMA_STACKCHAN_DEVICE_ID are required"
            )
        self._endpoint = resolved_endpoint
        self._device_token = resolved_token
        self._device_id = resolved_device_id
        self._sample_rate = sample_rate
        self._synthesizer = synthesizer or (lambda text: synthesize_wav_pcm(text, sample_rate=sample_rate))
        self._timeout_seconds = timeout_seconds
        self._opener = opener
        self._fallback = fallback

    def speak(self, phrase: str) -> bool:
        try:
            pcm = self._synthesizer(phrase)
            self._push(pcm)
            return True
        except (SpeechSynthesisError, urllib.error.URLError, OSError, ValueError):
            if self._fallback is not None:
                return bool(self._fallback.speak(phrase))
            return False

    def _push(self, pcm: bytes) -> None:
        request = urllib.request.Request(
            self._endpoint,
            data=pcm,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._device_token}",
                "X-Device-Id": self._device_id,
                "Content-Type": f"audio/L16;rate={self._sample_rate};channels=1",
            },
        )
        with self._opener(request, self._timeout_seconds) as response:
            status = getattr(response, "status", 200)
        if status is not None and status >= 400:
            raise urllib.error.URLError(f"speak endpoint returned status {status}")


__all__ = ["StackChanSpeechSink"]
