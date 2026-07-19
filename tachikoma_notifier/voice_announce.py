"""Step 2.6: read approval requests aloud via the existing TTS SpeechSink.

This reuses the same Windows SAPI / LogSpeechSink path already used by
notifier.py to speak ordinary status events -- it only adds a phrase for a
pending approval. It does not add a microphone, does not implement any
response channel, and does not connect to StackChan or Even G2 hardware.
"""
from __future__ import annotations

from typing import Callable

from tachikoma_notifier.approvals import ApprovalRequest

SpeakFn = Callable[[str], bool]
Announcer = Callable[[ApprovalRequest], bool]


def format_announcement(request: ApprovalRequest) -> str:
    return f"{request.tool_name}の承認が必要だよ。{request.safe_summary}"


def build_speech_announcer(speak: SpeakFn) -> Announcer:
    """Adapt a SpeechSink.speak-shaped callable into a relay Announcer."""

    def announce(request: ApprovalRequest) -> bool:
        return bool(speak(format_announcement(request)))

    return announce


__all__ = ["Announcer", "SpeakFn", "build_speech_announcer", "format_announcement"]
