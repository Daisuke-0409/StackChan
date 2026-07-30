"""Step 2.8: strictly-gated voice approve_once.

Letting a spoken word become an approve_once decision is powerful and easy
to get wrong -- background noise, someone else's voice, a misheard word.
This module only ever applies an approve_once decision when every one of
the following holds:

  1. Exactly one request is currently pending (ApprovalRequestStore.list_pending()).
  2. The caller-supplied approval_id matches that single pending request --
     never "whatever is most recent".
  3. request.risk_level is ApprovalRiskLevel.LOW.
  4. request.status is already awaiting_confirmation (the relay has
     announced it and is actively waiting).
  5. request.tool_name is on an explicit, small allowlist of read-only,
     non-destructive tools (SAFE_VOICE_TOOL_NAMES). This is how "no file
     deletion, no git push, no external send, no credential access, no
     system config change, no arbitrary command execution" is enforced
     mechanically: those categories are simply never on the allowlist,
     rather than detected by inspecting free-text tool input -- which is
     deliberately stripped from metadata elsewhere and can't be reliably
     classified after the fact.
  6. The request has not expired -- enforced by list_pending() itself,
     which resolves the store's own clock and excludes anything already
     past expiry, so a request reaching condition 2 above is already
     known-unexpired as of that same call.
  7. The recognized utterance, after normalization, exactly matches one of
     a small fixed set of explicit approval phrases (EXPLICIT_APPROVAL_PHRASES)
     -- never a substring or keyword match, so a misrecognized negation
     ("承認しません") cannot match just because it contains "承認".

If any condition fails, VoiceApprovalDenied is raised and no decision is
applied. There is no voice-driven reject path here: rejection still happens
through expiry or the manual CLI (Step 2.5). ApprovalRequestStore.apply_decision
already enforces one-shot application (replay guard + terminal transition),
so this module does not duplicate that logic -- it only decides whether to
call it at all.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import FrozenSet, Optional

from tachikoma_notifier.approval_store import ApprovalRequestStore
from tachikoma_notifier.approvals import (
    ApprovalActor,
    ApprovalDecision,
    ApprovalDecisionType,
    ApprovalRequest,
    ApprovalRiskLevel,
    ApprovalStatus,
    ConfirmationMethod,
)

SAFE_VOICE_TOOL_NAMES: FrozenSet[str] = frozenset({"Read", "Glob", "Grep"})

EXPLICIT_APPROVAL_PHRASES: FrozenSet[str] = frozenset(
    {
        "承認",
        "承認します",
        "承認する",
        "はい承認します",
        "許可",
        "許可します",
    }
)

_WHITESPACE = re.compile(r"\s+")
_TRAILING_PUNCTUATION = re.compile(r"[。.!！]+$")


class VoiceApprovalDenied(Exception):
    """Raised with a safe, non-sensitive reason. Never approves anything."""


def normalize_utterance(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    normalized = _WHITESPACE.sub("", normalized)
    normalized = _TRAILING_PUNCTUATION.sub("", normalized)
    return normalized


class VoiceApprovalGate:
    def __init__(self, store: ApprovalRequestStore) -> None:
        self._store = store

    def try_approve(
        self,
        approval_id: str,
        utterance: str,
        *,
        now: Optional[datetime] = None,
    ) -> ApprovalRequest:
        # list_pending() resolves "now" through the store's own clock and
        # excludes anything already past expiry, so a request appearing here
        # is guaranteed non-expired as of this same call -- no separate
        # is_expired() check is needed (and one using an unresolved `now`
        # would race against the store's clock instead of matching it).
        pending = self._store.list_pending(now=now)
        if len(pending) != 1:
            raise VoiceApprovalDenied("voice approval requires exactly one pending request")
        request = pending[0]
        if request.approval_id != approval_id:
            raise VoiceApprovalDenied("approval_id does not match the single pending request")
        if request.risk_level is not ApprovalRiskLevel.LOW:
            raise VoiceApprovalDenied("voice approval requires risk_level=low")
        if request.status is not ApprovalStatus.AWAITING_CONFIRMATION:
            raise VoiceApprovalDenied("request is not awaiting confirmation")
        if request.tool_name not in SAFE_VOICE_TOOL_NAMES:
            raise VoiceApprovalDenied("tool_name is not eligible for voice approval")
        if normalize_utterance(utterance) not in EXPLICIT_APPROVAL_PHRASES:
            raise VoiceApprovalDenied("utterance did not exactly match an explicit approval phrase")

        decision = ApprovalDecision.create(
            request.approval_id,
            ApprovalDecisionType.APPROVE_ONCE,
            actor=ApprovalActor.USER,
            confirmation_method=ConfirmationMethod.VOICE,
        )
        return self._store.apply_decision(decision, now=now)


__all__ = [
    "EXPLICIT_APPROVAL_PHRASES",
    "SAFE_VOICE_TOOL_NAMES",
    "VoiceApprovalDenied",
    "VoiceApprovalGate",
    "normalize_utterance",
]
