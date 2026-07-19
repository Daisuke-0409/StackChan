"""Thread-safe in-memory store for Step 2 approval requests.

The store deliberately has no persistence, transport, logging, or UI.  It
keeps only validated ApprovalRequest objects and opaque lifecycle timestamps;
durable storage and a PermissionRelay belong to later phases.
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from tachikoma_notifier.approvals import (
    ApprovalActor,
    ApprovalChoice,
    ApprovalDecision,
    ApprovalDecisionType,
    ApprovalRequest,
    ApprovalStatus,
    ReplayError,
)

DEFAULT_MAX_REQUESTS = 1000
DEFAULT_TERMINAL_RETENTION_SECONDS = 300.0

_ACTIVE_STATUSES = frozenset(
    {
        ApprovalStatus.PENDING,
        ApprovalStatus.ANNOUNCED,
        ApprovalStatus.AWAITING_CONFIRMATION,
    }
)
_TERMINAL_STATUSES = frozenset(
    {
        ApprovalStatus.APPROVED,
        ApprovalStatus.REJECTED,
        ApprovalStatus.EXPIRED,
        ApprovalStatus.CANCELLED,
        ApprovalStatus.INVALID,
        ApprovalStatus.RELAY_FAILED,
    }
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


class ApprovalStoreError(ValueError):
    """Base class for safe, non-sensitive store errors."""


class ApprovalNotFoundError(ApprovalStoreError):
    pass


class DuplicateApprovalError(ApprovalStoreError):
    pass


class ApprovalExpiredError(ApprovalStoreError):
    pass


class ApprovalAlreadyFinalizedError(ApprovalStoreError):
    pass


class DecisionReplayError(ReplayError):
    pass


class StoreCapacityError(ApprovalStoreError):
    pass


class InvalidApprovalStateError(ApprovalStoreError):
    pass


@dataclass(frozen=True)
class _StoredApproval:
    request: ApprovalRequest
    stored_at: datetime
    updated_at: datetime
    terminal_at: Optional[datetime] = None


class ApprovalRequestStore:
    """A bounded, thread-safe, process-local approval request store."""

    def __init__(
        self,
        *,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        terminal_retention_seconds: float = DEFAULT_TERMINAL_RETENTION_SECONDS,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        if not isinstance(max_requests, int) or isinstance(max_requests, bool) or max_requests <= 0:
            raise ValueError("max_requests must be a positive integer")
        if terminal_retention_seconds < 0:
            raise ValueError("terminal_retention_seconds must not be negative")
        self.max_requests = max_requests
        self.terminal_retention_seconds = float(terminal_retention_seconds)
        self._clock = clock or _utc_now
        self._records: dict[str, _StoredApproval] = {}
        self._dedupe_index: dict[str, str] = {}
        self._decision_ids: set[str] = set()
        self._decided_approval_ids: set[str] = set()
        self._replay_order: deque[tuple[str, str]] = deque()
        self._lock = threading.RLock()

    def _now(self) -> datetime:
        return _as_utc(self._clock(), "clock value")

    @staticmethod
    def _is_terminal(request: ApprovalRequest) -> bool:
        return request.status in _TERMINAL_STATUSES

    @staticmethod
    def _is_active(request: ApprovalRequest) -> bool:
        return request.status in _ACTIVE_STATUSES

    def register(self, request: ApprovalRequest) -> ApprovalRequest:
        """Register a new pending request, or return an active duplicate."""
        if not isinstance(request, ApprovalRequest):
            raise TypeError("request must be ApprovalRequest")
        now = self._now()
        with self._lock:
            if request.approval_id in self._records:
                raise DuplicateApprovalError("approval_id already exists")
            if request.status not in _ACTIVE_STATUSES:
                raise InvalidApprovalStateError("only active requests may be registered")
            if request.is_expired(now):
                raise ApprovalExpiredError("approval request is expired")

            duplicate_id = self._dedupe_index.get(request.dedupe_key)
            if duplicate_id is not None:
                existing = self._records.get(duplicate_id)
                if existing is not None:
                    if self._is_active(existing.request):
                        return existing.request
                    if self._is_terminal(existing.request) and existing.terminal_at is not None:
                        age = now - existing.terminal_at
                        if age < timedelta(seconds=self.terminal_retention_seconds):
                            return existing.request
                        self._remove_locked(existing.request.approval_id)

            if len(self._records) >= self.max_requests:
                raise StoreCapacityError("approval request store is full")
            self._records[request.approval_id] = _StoredApproval(
                request=request,
                stored_at=now,
                updated_at=now,
            )
            self._dedupe_index[request.dedupe_key] = request.approval_id
            return request

    def get(self, approval_id: str) -> Optional[ApprovalRequest]:
        with self._lock:
            record = self._records.get(approval_id)
            return record.request if record is not None else None

    def require(self, approval_id: str) -> ApprovalRequest:
        request = self.get(approval_id)
        if request is None:
            raise ApprovalNotFoundError("approval request was not found")
        return request

    def list_pending(self, *, now: Optional[datetime] = None) -> list[ApprovalRequest]:
        current = _as_utc(now, "now") if now is not None else self._now()
        with self._lock:
            self._expire_due_locked(current)
            return [
                record.request
                for record in self._sorted_records_locked()
                if self._is_active(record.request)
            ]

    def list_by_status(
        self,
        status: ApprovalStatus,
        *,
        now: Optional[datetime] = None,
    ) -> list[ApprovalRequest]:
        if not isinstance(status, ApprovalStatus):
            raise ValueError("status must be ApprovalStatus")
        current = _as_utc(now, "now") if now is not None else self._now()
        with self._lock:
            self._expire_due_locked(current)
            return [
                record.request
                for record in self._sorted_records_locked()
                if record.request.status is status
            ]

    def apply_decision(
        self,
        decision: ApprovalDecision,
        now: Optional[datetime] = None,
    ) -> ApprovalRequest:
        """Validate and atomically apply exactly one decision."""
        if not isinstance(decision, ApprovalDecision):
            raise TypeError("decision must be ApprovalDecision")
        current = _as_utc(now, "now") if now is not None else self._now()
        with self._lock:
            if decision.decision_id in self._decision_ids or decision.approval_id in self._decided_approval_ids:
                raise DecisionReplayError("decision has already been used")
            record = self._records.get(decision.approval_id)
            if record is None:
                raise ApprovalNotFoundError("approval request was not found")
            request = record.request
            if self._is_terminal(request):
                raise ApprovalAlreadyFinalizedError("approval request is already finalized")
            if request.status not in _ACTIVE_STATUSES:
                raise InvalidApprovalStateError("approval request is not actionable")

            try:
                decision.validate_for(request, now=current)
            except ValueError as exc:
                if request.is_expired(current):
                    raise ApprovalExpiredError("approval request is expired") from exc
                raise InvalidApprovalStateError("decision is not valid for approval state") from exc

            target = self._target_status(request, decision, current)
            next_request = request.transition_to(target)
            # Consume the decision only after every validation and transition
            # check succeeds.  No later operation in this locked section can
            # fail, so a consumed ID cannot be left without its state update.
            self._remember_replay_locked(decision.decision_id, decision.approval_id)
            terminal_at = current if self._is_terminal(next_request) else None
            self._records[request.approval_id] = _StoredApproval(
                request=next_request,
                stored_at=record.stored_at,
                updated_at=current,
                terminal_at=terminal_at,
            )
            return next_request

    def expire_due(self, now: Optional[datetime] = None) -> list[ApprovalRequest]:
        """Mark all currently active requests at or past expiry as expired."""
        current = _as_utc(now, "now") if now is not None else self._now()
        with self._lock:
            return self._expire_due_locked(current)

    def remove_terminal_before(self, cutoff: datetime) -> int:
        cutoff_utc = _as_utc(cutoff, "cutoff")
        with self._lock:
            removed = 0
            for approval_id, record in list(self._records.items()):
                if self._is_terminal(record.request) and record.terminal_at is not None:
                    if record.terminal_at < cutoff_utc:
                        self._remove_locked(approval_id)
                        removed += 1
            return removed

    def cleanup(self, cutoff: datetime) -> int:
        """Compatibility alias for terminal cleanup."""
        return self.remove_terminal_before(cutoff)

    def count(self) -> int:
        with self._lock:
            return len(self._records)

    def _target_status(
        self,
        request: ApprovalRequest,
        decision: ApprovalDecision,
        now: datetime,
    ) -> ApprovalStatus:
        if decision.decision is ApprovalDecisionType.APPROVE_ONCE:
            if request.status is not ApprovalStatus.AWAITING_CONFIRMATION:
                raise InvalidApprovalStateError("approve_once requires awaiting_confirmation")
            if ApprovalChoice.APPROVE_ONCE not in request.choices:
                raise InvalidApprovalStateError("approve_once is not an allowed choice")
            return ApprovalStatus.APPROVED
        if decision.decision is ApprovalDecisionType.REJECT:
            if request.status not in _ACTIVE_STATUSES:
                raise InvalidApprovalStateError("reject requires an active request")
            if ApprovalChoice.REJECT not in request.choices:
                raise InvalidApprovalStateError("reject is not an allowed choice")
            return ApprovalStatus.REJECTED
        if decision.decision is ApprovalDecisionType.CANCEL:
            if request.status not in _ACTIVE_STATUSES:
                raise InvalidApprovalStateError("cancel requires an active request")
            return ApprovalStatus.CANCELLED
        if decision.decision is ApprovalDecisionType.EXPIRED:
            if request.status not in _ACTIVE_STATUSES or not request.is_expired(now):
                raise ApprovalExpiredError("expired decision requires an expired request")
            return ApprovalStatus.EXPIRED
        if decision.decision is ApprovalDecisionType.INVALID:
            if decision.actor is not ApprovalActor.SYSTEM:
                raise InvalidApprovalStateError("invalid decision is system-only")
            return ApprovalStatus.INVALID
        raise InvalidApprovalStateError("unsupported decision")

    def _expire_due_locked(self, now: datetime) -> list[ApprovalRequest]:
        expired: list[ApprovalRequest] = []
        for approval_id, record in list(self._records.items()):
            request = record.request
            if self._is_active(request) and request.is_expired(now):
                updated = request.transition_to(ApprovalStatus.EXPIRED)
                self._records[approval_id] = _StoredApproval(
                    request=updated,
                    stored_at=record.stored_at,
                    updated_at=now,
                    terminal_at=now,
                )
                expired.append(updated)
        return expired

    def _sorted_records_locked(self) -> list[_StoredApproval]:
        return sorted(self._records.values(), key=lambda record: record.stored_at)

    def _remove_locked(self, approval_id: str) -> None:
        record = self._records.pop(approval_id, None)
        if record is not None and self._dedupe_index.get(record.request.dedupe_key) == approval_id:
            self._dedupe_index.pop(record.request.dedupe_key, None)

    def _remember_replay_locked(self, decision_id: str, approval_id: str) -> None:
        self._decision_ids.add(decision_id)
        self._decided_approval_ids.add(approval_id)
        self._replay_order.append((decision_id, approval_id))
        replay_limit = self.max_requests * 2
        while len(self._replay_order) > replay_limit:
            old_decision_id, old_approval_id = self._replay_order.popleft()
            self._decision_ids.discard(old_decision_id)
            self._decided_approval_ids.discard(old_approval_id)


__all__ = [
    "ApprovalAlreadyFinalizedError",
    "ApprovalExpiredError",
    "ApprovalNotFoundError",
    "ApprovalRequestStore",
    "ApprovalStoreError",
    "DEFAULT_MAX_REQUESTS",
    "DEFAULT_TERMINAL_RETENTION_SECONDS",
    "DecisionReplayError",
    "DuplicateApprovalError",
    "InvalidApprovalStateError",
    "StoreCapacityError",
]
