import threading
import unittest
from datetime import datetime, timedelta, timezone

from tachikoma_notifier.approval_store import (
    ApprovalAlreadyFinalizedError,
    ApprovalExpiredError,
    ApprovalNotFoundError,
    ApprovalRequestStore,
    DecisionReplayError,
    DuplicateApprovalError,
    InvalidApprovalStateError,
    StoreCapacityError,
)
from tachikoma_notifier.approvals import (
    ApprovalActor,
    ApprovalDecision,
    ApprovalDecisionType,
    ApprovalRequest,
    ApprovalRiskLevel,
    ApprovalStatus,
    ConfirmationMethod,
)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class MutableClock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class ApprovalStoreTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc)
        self.clock = MutableClock(self.start)
        self.store = ApprovalRequestStore(clock=self.clock, terminal_retention_seconds=300)

    def request(self, summary="read", *, status=ApprovalStatus.PENDING, ttl=60, session="s1", tool="Read"):
        request = ApprovalRequest.create(
            source="claude_code",
            session_id=session,
            action_type="tool_permission",
            tool_name=tool,
            safe_summary=summary,
            risk_level=ApprovalRiskLevel.LOW,
            requested_at=iso(self.clock.value),
            expires_at=iso(self.clock.value + timedelta(seconds=ttl)),
        )
        if status is ApprovalStatus.PENDING:
            return request
        if status is ApprovalStatus.APPROVED:
            return request.transition_to(ApprovalStatus.AWAITING_CONFIRMATION).transition_to(status)
        return request.transition_to(status)

    def decision(self, request, kind, *, actor=ApprovalActor.USER):
        return ApprovalDecision.create(
            request.approval_id,
            kind,
            actor=actor,
            confirmation_method=ConfirmationMethod.MANUAL,
            decided_at=iso(self.clock.value),
        )

    def test_register_get_require_and_count(self):
        request = self.request()
        self.assertIs(self.store.register(request), request)
        self.assertIs(self.store.get(request.approval_id), request)
        self.assertIs(self.store.require(request.approval_id), request)
        self.assertEqual(self.store.count(), 1)
        self.assertIsNone(self.store.get("00000000-0000-4000-8000-000000000000"))
        with self.assertRaises(ApprovalNotFoundError):
            self.store.require("00000000-0000-4000-8000-000000000000")

    def test_register_validates_type_duplicate_id_status_and_expiry(self):
        request = self.request()
        with self.assertRaises(TypeError):
            self.store.register(object())
        self.store.register(request)
        with self.assertRaises(DuplicateApprovalError):
            self.store.register(request)
        announced = self.request(summary="announced", status=ApprovalStatus.ANNOUNCED)
        self.store.register(announced)
        expired = self.request(summary="expired", ttl=1)
        self.clock.value += timedelta(seconds=1)
        with self.assertRaises(ApprovalExpiredError):
            self.store.register(expired)
        terminal = self.request(summary="terminal", status=ApprovalStatus.APPROVED)
        with self.assertRaises(InvalidApprovalStateError):
            self.store.register(terminal)

    def test_active_dedupe_returns_existing_and_does_not_use_session_only(self):
        first = self.request(summary="same")
        second = self.request(summary="same")
        self.assertIs(self.store.register(first), first)
        self.assertIs(self.store.register(second), first)
        self.assertEqual(self.store.count(), 1)
        different_tool = self.request(summary="same", tool="Write")
        self.assertIs(self.store.register(different_tool), different_tool)
        self.assertEqual(self.store.count(), 2)

    def test_terminal_dedupe_is_reused_until_retention_then_allows_new_request(self):
        first = self.request(summary="same")
        self.store.register(first)
        self.store.apply_decision(self.decision(first, ApprovalDecisionType.REJECT), now=self.clock.value)
        second = self.request(summary="same")
        self.assertEqual(self.store.register(second).approval_id, first.approval_id)
        self.assertEqual(self.store.require(first.approval_id).status, ApprovalStatus.REJECTED)
        self.clock.value += timedelta(seconds=301)
        third = self.request(summary="same")
        self.assertIs(self.store.register(third), third)
        self.assertEqual(self.store.count(), 1)

    def test_list_pending_and_list_by_status(self):
        pending = self.request(summary="pending")
        rejected = self.request(summary="rejected")
        self.store.register(pending)
        self.store.register(rejected)
        self.store.apply_decision(self.decision(rejected, ApprovalDecisionType.REJECT))
        self.assertEqual([item.approval_id for item in self.store.list_pending()], [pending.approval_id])
        self.assertEqual([item.approval_id for item in self.store.list_by_status(ApprovalStatus.REJECTED)],
                         [rejected.approval_id])

    def test_approve_once_requires_awaiting_confirmation(self):
        request = self.request()
        self.store.register(request)
        with self.assertRaises(InvalidApprovalStateError):
            self.store.apply_decision(self.decision(request, ApprovalDecisionType.APPROVE_ONCE))

        awaiting = self.request(summary="awaiting", status=ApprovalStatus.AWAITING_CONFIRMATION)
        self.store.register(awaiting)
        updated = self.store.apply_decision(self.decision(awaiting, ApprovalDecisionType.APPROVE_ONCE))
        self.assertEqual(updated.status, ApprovalStatus.APPROVED)

    def test_reject_cancel_invalid_and_expired_decisions(self):
        rejectable = self.request(summary="reject")
        self.store.register(rejectable)
        self.assertEqual(
            self.store.apply_decision(self.decision(rejectable, ApprovalDecisionType.REJECT)).status,
            ApprovalStatus.REJECTED,
        )

        cancellable = self.request(summary="cancel", status=ApprovalStatus.ANNOUNCED)
        self.store.register(cancellable)
        self.assertEqual(
            self.store.apply_decision(self.decision(cancellable, ApprovalDecisionType.CANCEL)).status,
            ApprovalStatus.CANCELLED,
        )

        invalid = self.request(summary="invalid")
        self.store.register(invalid)
        with self.assertRaises(InvalidApprovalStateError):
            self.store.apply_decision(self.decision(invalid, ApprovalDecisionType.INVALID))
        self.assertEqual(
            self.store.apply_decision(
                self.decision(invalid, ApprovalDecisionType.INVALID, actor=ApprovalActor.SYSTEM)
            ).status,
            ApprovalStatus.INVALID,
        )

        expired = self.request(summary="expired", ttl=1)
        self.store.register(expired)
        self.clock.value += timedelta(seconds=1)
        self.assertEqual(
            self.store.apply_decision(self.decision(expired, ApprovalDecisionType.EXPIRED)).status,
            ApprovalStatus.EXPIRED,
        )

    def test_expired_reject_and_cancel_are_rejected(self):
        request = self.request(ttl=1)
        self.store.register(request)
        self.clock.value += timedelta(seconds=1)
        with self.assertRaises(ApprovalExpiredError):
            self.store.apply_decision(self.decision(request, ApprovalDecisionType.REJECT))
        with self.assertRaises(ApprovalExpiredError):
            self.store.apply_decision(self.decision(request, ApprovalDecisionType.CANCEL))

    def test_expire_due_boundary_is_idempotent(self):
        before = self.request(summary="before", ttl=10)
        at = self.request(summary="at", ttl=10)
        self.store.register(before)
        self.store.register(at)
        before_expiry = self.start + timedelta(seconds=9, milliseconds=999)
        self.assertEqual(self.store.expire_due(before_expiry), [])
        expired = self.store.expire_due(self.start + timedelta(seconds=10))
        self.assertEqual({item.approval_id for item in expired}, {before.approval_id, at.approval_id})
        self.assertEqual(self.store.expire_due(self.start + timedelta(seconds=11)), [])
        self.assertEqual(self.store.list_pending(now=self.start + timedelta(seconds=11)), [])

    def test_terminal_states_are_not_expired_again(self):
        request = self.request()
        self.store.register(request)
        self.store.apply_decision(self.decision(request, ApprovalDecisionType.REJECT))
        self.clock.value += timedelta(seconds=1000)
        self.assertEqual(self.store.expire_due(), [])
        self.assertEqual(self.store.get(request.approval_id).status, ApprovalStatus.REJECTED)

    def test_decision_replay_and_finalization_are_rejected(self):
        request = self.request()
        self.store.register(request)
        decision = self.decision(request, ApprovalDecisionType.REJECT)
        self.store.apply_decision(decision)
        with self.assertRaises(DecisionReplayError):
            self.store.apply_decision(decision)
        different = self.decision(request, ApprovalDecisionType.REJECT)
        with self.assertRaises(DecisionReplayError):
            self.store.apply_decision(different)

    def test_missing_decision_request_is_rejected(self):
        missing = ApprovalDecision.create("00000000-0000-4000-8000-000000000000", ApprovalDecisionType.REJECT)
        with self.assertRaises(ApprovalNotFoundError):
            self.store.apply_decision(missing)

    def test_capacity_limit_and_explicit_cleanup(self):
        store = ApprovalRequestStore(max_requests=1, clock=self.clock, terminal_retention_seconds=0)
        first = self.request(summary="first")
        store.register(first)
        with self.assertRaises(StoreCapacityError):
            store.register(self.request(summary="second"))
        store.apply_decision(self.decision(first, ApprovalDecisionType.REJECT))
        self.clock.value += timedelta(seconds=1)
        self.assertEqual(store.remove_terminal_before(self.clock.value), 1)
        store.register(self.request(summary="second"))
        self.assertEqual(store.count(), 1)

    def test_cleanup_does_not_remove_active_requests(self):
        active = self.request(summary="active")
        self.store.register(active)
        self.clock.value += timedelta(seconds=1000)
        self.assertEqual(self.store.cleanup(self.clock.value), 0)
        self.assertIsNotNone(self.store.get(active.approval_id))

    def test_thread_safe_duplicate_registration(self):
        results = []
        errors = []
        requests = [self.request(summary="same") for _ in range(8)]

        def register(item):
            try:
                results.append(self.store.register(item))
            except Exception as exc:  # pragma: no cover - diagnostic collection
                errors.append(exc)

        threads = [threading.Thread(target=register, args=(item,)) for item in requests]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.store.count(), 1)
        self.assertEqual({item.approval_id for item in results}, {requests[0].approval_id})

    def test_store_does_not_expose_raw_hook_or_sensitive_data(self):
        request = ApprovalRequest.create(
            source="claude_code", tool_name="Read", safe_summary="safe",
            metadata={"token": "hidden", "safe": "ok"},
        )
        self.store.register(request)
        stored = self.store.require(request.approval_id)
        self.assertNotIn("token", stored.to_json().lower())
        self.assertNotIn("hook_event_name", stored.to_json().lower())


if __name__ == "__main__":
    unittest.main()
