"""Step 2.3: a simulated (mock) permission relay.

This wires ApprovalRequest / ApprovalDecision / ApprovalRequestStore into a
single lifecycle: register -> announce -> await confirmation -> decide. The
"announcer" and "decision provider" are caller-supplied callables so this
module can be exercised deterministically in tests or a demo script.

This module intentionally contains no real voice/microphone input, no
StackChan or Even G2 transport, and no Claude Code Hook response transport.
A "decision provider" here is a stand-in for a future confirmation source
(voice, device button, etc.); nothing in this module sends any result back to
Claude Code. Whether an official Claude Code approval channel exists at all is
a separate, later investigation (Step 2.4) and is out of scope here.

Fail-closed: an announcer failure marks the request relay_failed, and any
decision-provider failure resolves to a system-issued reject. Nothing here
can result in an implicit approval.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Optional

from tachikoma_notifier.approval_store import (
    ApprovalRequestStore,
)
from tachikoma_notifier.approvals import (
    ApprovalActor,
    ApprovalChoice,
    ApprovalDecision,
    ApprovalDecisionType,
    ApprovalRequest,
    ApprovalStatus,
    ConfirmationMethod,
)

Announcer = Callable[[ApprovalRequest], bool]
DecisionProvider = Callable[[ApprovalRequest], ApprovalChoice]

_CHOICE_TO_DECISION = {
    ApprovalChoice.APPROVE_ONCE: ApprovalDecisionType.APPROVE_ONCE,
    ApprovalChoice.REJECT: ApprovalDecisionType.REJECT,
}


class SimulatedPermissionRelay:
    """Drives one ApprovalRequest through announce -> confirm -> decide.

    This is a Step 2.3 simulation harness, not a production relay. It has no
    opinion on transport: the announcer and decision provider are ordinary
    Python callables supplied by the caller (tests, or a future demo script).
    """

    def __init__(
        self,
        store: ApprovalRequestStore,
        announcer: Announcer,
        *,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._store = store
        self._announcer = announcer
        self._clock = clock

    def submit(self, request: ApprovalRequest) -> ApprovalRequest:
        """Register a new request with the store."""
        return self._store.register(request)

    def announce(self, approval_id: str) -> ApprovalRequest:
        """Call the announcer and record the outcome.

        A truthy return advances the request to ``announced``. A falsy
        return, or any exception raised by the announcer, is treated as a
        failed announcement: the request is marked ``relay_failed`` and is
        never retried silently.
        """
        request = self._store.require(approval_id)
        if request.status is not ApprovalStatus.PENDING:
            raise ValueError("announce requires a pending request")
        try:
            announced_ok = bool(self._announcer(request))
        except Exception:
            announced_ok = False
        now = self._now()
        if not announced_ok:
            return self._store.advance_status(approval_id, ApprovalStatus.RELAY_FAILED, now=now)
        return self._store.advance_status(approval_id, ApprovalStatus.ANNOUNCED, now=now)

    def begin_confirmation(self, approval_id: str) -> ApprovalRequest:
        """Move a pending or announced request into awaiting_confirmation."""
        return self._store.advance_status(
            approval_id, ApprovalStatus.AWAITING_CONFIRMATION, now=self._now()
        )

    def resolve(
        self,
        approval_id: str,
        choice: ApprovalChoice,
        *,
        actor: ApprovalActor = ApprovalActor.USER,
        correlation_id: Optional[str] = None,
    ) -> ApprovalRequest:
        """Apply a simulated confirmation result as a one-shot decision."""
        if choice not in _CHOICE_TO_DECISION:
            raise ValueError("choice must be approve_once or reject")
        decision = ApprovalDecision.create(
            approval_id,
            _CHOICE_TO_DECISION[choice],
            actor=actor,
            confirmation_method=ConfirmationMethod.SIMULATED,
            correlation_id=correlation_id,
            decided_at=self._iso_now(),
        )
        return self._store.apply_decision(decision, now=self._now())

    def run(
        self,
        request: ApprovalRequest,
        decision_provider: DecisionProvider,
    ) -> ApprovalRequest:
        """Convenience end-to-end simulation: submit, announce, confirm, decide.

        If the announcer fails, the request is returned as relay_failed
        without calling the decision provider. If the decision provider
        raises, the outcome is a system-issued reject rather than a silent
        approval.
        """
        submitted = self.submit(request)
        announced = self.announce(submitted.approval_id)
        if announced.status is ApprovalStatus.RELAY_FAILED:
            return announced
        awaiting = self.begin_confirmation(announced.approval_id)

        try:
            choice = decision_provider(awaiting)
            actor = ApprovalActor.USER
        except Exception:
            choice = ApprovalChoice.REJECT
            actor = ApprovalActor.SYSTEM
        if choice not in _CHOICE_TO_DECISION:
            choice = ApprovalChoice.REJECT
            actor = ApprovalActor.SYSTEM
        return self.resolve(awaiting.approval_id, choice, actor=actor)

    def _now(self) -> Optional[datetime]:
        return self._clock() if self._clock is not None else None

    def _iso_now(self) -> Optional[str]:
        now = self._now()
        if now is None:
            return None
        return now.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


__all__ = [
    "Announcer",
    "DecisionProvider",
    "SimulatedPermissionRelay",
]
