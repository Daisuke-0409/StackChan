"""Step 7: chain STT text -> Gemini reply -> Voicebox speech -> StackChan.

This module only connects three already-independent pieces; it adds no new
network calls of its own:
  1. gemini_responder.GeminiResponder  (STT text -> short reply text)
  2. voicebox_synth.VoiceboxSynthesizer (reply text -> PCM, injected as a
     `synthesizer` into the *existing*, already-verified-on-real-hardware
     StackChanSpeechSink -- see stackchan_speech_sink.py)
  3. Any SpeechSink (typically StackChanSpeechSink) that actually pushes
     the resulting audio to the device's speak_queue.

Nothing here decides how recorded audio becomes STT text -- that boundary
is intentionally left at cloud_transcriber.TranscriptionResult.text (or any
other str), matching how voice_announce.py stays independent of the
approval store it's fed from.
"""
from __future__ import annotations

from typing import Callable, Optional

from tachikoma_notifier.gemini_responder import GeminiResponder, GeminiResponseError
from tachikoma_notifier.notifier import SpeechSink, WindowsSpeechSink
from tachikoma_notifier.voicebox_synth import VoiceboxSynthesizer

Logger = Callable[[str], None]


def build_voicebox_stackchan_sink(*, fallback: Optional[SpeechSink] = None) -> SpeechSink:
    """StackChanSpeechSink wired to Voicebox instead of the Windows SAPI path.

    Uses the exact StackChanSpeechSink class already verified end-to-end on
    real hardware (endpoint/token/device_id still resolved from
    TACHIKOMA_STACKCHAN_* env vars); only the `synthesizer` is swapped from
    windows_wave_synth.synthesize_wav_pcm to VoiceboxSynthesizer.synthesize.
    """
    # Imported lazily, matching notifier._build_sink's existing pattern:
    # stackchan_speech_sink.py imports SpeechSink from notifier.py, so a
    # top-level import here would be circular.
    from tachikoma_notifier.stackchan_speech_sink import StackChanSpeechSink

    voicebox = VoiceboxSynthesizer()
    return StackChanSpeechSink(synthesizer=voicebox.synthesize, fallback=fallback or WindowsSpeechSink())


class ConversationReplyPipeline:
    """Turns one piece of recognized user speech into one spoken reply."""

    def __init__(
        self,
        responder: GeminiResponder,
        sink: SpeechSink,
        *,
        logger: Optional[Logger] = None,
    ) -> None:
        self._responder = responder
        self._sink = sink
        self._logger = logger or (lambda _msg: None)

    def handle_utterance(self, user_text: str) -> bool:
        """Generate a reply for user_text and speak it. Never raises.

        Returns False (without touching the sink) if reply generation
        fails or produces no usable text; returns whatever the sink's own
        speak() reports otherwise.
        """
        try:
            reply = self._responder.generate_reply(user_text)
        except (GeminiResponseError, ValueError) as exc:
            self._logger(f"[conversation_pipeline] reply generation failed: {type(exc).__name__}")
            return False
        return bool(self._sink.speak(reply.text))


__all__ = ["ConversationReplyPipeline", "build_voicebox_stackchan_sink"]
