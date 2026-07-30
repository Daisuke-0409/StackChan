"""Step 2.5: PC manual approval CLI.

A minimal command-line front end over ApprovalRequestStore for a human at
the keyboard to approve_once or reject one pending approval request. It
never introduces approve_session / always_allow, and it never applies a
second decision to an already-decided request -- ApprovalRequestStore's
one-shot apply_decision already enforces that atomically.
"""
from __future__ import annotations

from typing import Callable, Optional, Sequence

from tachikoma_notifier.approval_store import ApprovalRequestStore
from tachikoma_notifier.approvals import (
    ApprovalActor,
    ApprovalDecision,
    ApprovalDecisionType,
    ApprovalRequest,
    ConfirmationMethod,
)

_CHOICE_TO_DECISION = {
    "1": ApprovalDecisionType.APPROVE_ONCE,
    "2": ApprovalDecisionType.REJECT,
}


class ManualApprovalCli:
    """Interactive, testable approve_once/reject prompt for one request."""

    def __init__(
        self,
        store: ApprovalRequestStore,
        *,
        input_fn: Callable[[str], str] = input,
        output_fn: Callable[[str], None] = print,
    ) -> None:
        self._store = store
        self._input = input_fn
        self._output = output_fn

    def list_pending(self) -> Sequence[ApprovalRequest]:
        return self._store.list_pending()

    def decide(self, request: ApprovalRequest, raw_choice: str) -> ApprovalRequest:
        """Apply exactly one decision for one request. Raises on a bad choice
        or on a request that is not actionable (store enforces the latter)."""
        decision_type = _CHOICE_TO_DECISION.get(raw_choice.strip())
        if decision_type is None:
            raise ValueError("choice must be 1 (approve_once) or 2 (reject)")
        decision = ApprovalDecision.create(
            request.approval_id,
            decision_type,
            actor=ApprovalActor.USER,
            confirmation_method=ConfirmationMethod.MANUAL,
        )
        return self._store.apply_decision(decision)

    def prompt_for(self, request: ApprovalRequest) -> ApprovalRequest:
        self._output(
            f"[approval] {request.approval_id} tool={request.tool_name} "
            f"risk={request.risk_level.value} summary={request.safe_summary}"
        )
        self._output("1) approve_once  2) reject")
        raw = self._input("> ")
        return self.decide(request, raw)

    def prompt_for_id(self, approval_id: str) -> ApprovalRequest:
        request = self._store.require(approval_id)
        return self.prompt_for(request)

    def run_once(self) -> Optional[ApprovalRequest]:
        """Prompt for the single pending request, if exactly one exists."""
        pending = self.list_pending()
        if not pending:
            self._output("[approval] no pending requests")
            return None
        if len(pending) > 1:
            ids = ", ".join(request.approval_id for request in pending)
            self._output(f"[approval] {len(pending)} pending requests; specify one by id: {ids}")
            return None
        return self.prompt_for(pending[0])
