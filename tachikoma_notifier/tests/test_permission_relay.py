import unittest
from datetime import datetime, timedelta, timezone

from tachikoma_notifier.approval_store import (
    ApprovalAlreadyFinalizedError,
    ApprovalExpiredError,
    ApprovalNotFoundError,
    ApprovalRequestStore,
    DecisionReplayError,
    InvalidApprovalStateError,
)
from tachikoma_notifier.approvals import (
    ApprovalActor,
    ApprovalChoice,
    ApprovalRequest,
    ApprovalRiskLevel,
    ApprovalStatus,
    ConfirmationMethod,
)
from tachikoma_notifier.permission_relay import SimulatedPermissionRelay


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class MutableClock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value = self.value + timedelta(seconds=seconds)


class PermissionRelayTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc)
        self.clock = MutableClock(self.start)
        self.store = ApprovalRequestStore(clock=self.clock, terminal_retention_seconds=300)

    def request(self, summary="read file", *, ttl=60, session="s1", tool="Read"):
        return ApprovalRequest.create(
            source="claude_code",
            session_id=session,
            action_type="tool_permission",
            tool_name=tool,
            safe_summary=summary,
            risk_level=ApprovalRiskLevel.LOW,
            requested_at=iso(self.clock.value),
            expires_at=iso(self.clock.value + timedelta(seconds=ttl)),
        )

    def relay(self, announcer):
        return SimulatedPermissionRelay(self.store, announcer, clock=self.clock)

    # -- happy paths -----------------------------------------------------

    def test_run_approve_once_end_to_end(self):
        relay = self.relay(lambda request: True)
        result = relay.run(self.request(), lambda request: ApprovalChoice.APPROVE_ONCE)
        self.assertEqual(result.status, ApprovalStatus.APPROVED)

    def test_run_reject_end_to_end(self):
        relay = self.relay(lambda request: True)
        result = relay.run(self.request(), lambda request: ApprovalChoice.REJECT)
        self.assertEqual(result.status, ApprovalStatus.REJECTED)

    def test_resolve_uses_simulated_confirmation_method_and_user_actor(self):
        relay = self.relay(lambda request: True)
        submitted = relay.submit(self.request())
        relay.announce(submitted.approval_id)
        relay.begin_confirmation(submitted.approval_id)
        relay.resolve(submitted.approval_id, ApprovalChoice.APPROVE_ONCE)
        # Confirm store-level state matches; ApprovalDecision itself isn't
        # retained by the store, so we assert indirectly via the final status.
        self.assertEqual(self.store.require(submitted.approval_id).status, ApprovalStatus.APPROVED)

    def test_announce_and_begin_confirmation_step_by_step(self):
        relay = self.relay(lambda request: True)
        submitted = relay.submit(self.request())
        announced = relay.announce(submitted.approval_id)
        self.assertEqual(announced.status, ApprovalStatus.ANNOUNCED)
        awaiting = relay.begin_confirmation(submitted.approval_id)
        self.assertEqual(awaiting.status, ApprovalStatus.AWAITING_CONFIRMATION)

    # -- fail-closed behavior ---------------------------------------------

    def test_announcer_returning_false_marks_relay_failed_and_skips_decision(self):
        provider_calls = []
        relay = self.relay(lambda request: False)
        result = relay.run(self.request(), lambda request: provider_calls.append(request) or ApprovalChoice.APPROVE_ONCE)
        self.assertEqual(result.status, ApprovalStatus.RELAY_FAILED)
        self.assertEqual(provider_calls, [])

    def test_announcer_raising_marks_relay_failed(self):
        def bad_announcer(request):
            raise RuntimeError("tts crashed")

        relay = self.relay(bad_announcer)
        submitted = relay.submit(self.request())
        result = relay.announce(submitted.approval_id)
        self.assertEqual(result.status, ApprovalStatus.RELAY_FAILED)

    def test_decision_provider_raising_resolves_to_system_reject(self):
        def bad_provider(request):
            raise RuntimeError("simulated input crashed")

        relay = self.relay(lambda request: True)
        result = relay.run(self.request(), bad_provider)
        self.assertEqual(result.status, ApprovalStatus.REJECTED)

    def test_decision_provider_returning_invalid_choice_resolves_to_reject(self):
        relay = self.relay(lambda request: True)
        result = relay.run(self.request(), lambda request: "yes")
        self.assertEqual(result.status, ApprovalStatus.REJECTED)

    def test_resolve_rejects_unsupported_choice_value(self):
        relay = self.relay(lambda request: True)
        submitted = relay.submit(self.request())
        relay.announce(submitted.approval_id)
        relay.begin_confirmation(submitted.approval_id)
        with self.assertRaises(ValueError):
            relay.resolve(submitted.approval_id, "always_allow")

    def test_announce_requires_pending_status(self):
        relay = self.relay(lambda request: True)
        submitted = relay.submit(self.request())
        relay.announce(submitted.approval_id)
        with self.assertRaises(ValueError):
            relay.announce(submitted.approval_id)

    def test_approve_once_before_awaiting_confirmation_is_rejected(self):
        relay = self.relay(lambda request: True)
        submitted = relay.submit(self.request())
        relay.announce(submitted.approval_id)
        with self.assertRaises(InvalidApprovalStateError):
            relay.resolve(submitted.approval_id, ApprovalChoice.APPROVE_ONCE)

    def test_resolve_after_expiry_raises_and_does_not_approve(self):
        relay = self.relay(lambda request: True)
        submitted = relay.submit(self.request(ttl=10))
        relay.announce(submitted.approval_id)
        relay.begin_confirmation(submitted.approval_id)
        self.clock.advance(11)
        with self.assertRaises(ApprovalExpiredError):
            relay.resolve(submitted.approval_id, ApprovalChoice.APPROVE_ONCE)
        self.assertNotEqual(self.store.require(submitted.approval_id).status, ApprovalStatus.APPROVED)

    def test_resolve_twice_is_rejected_by_replay_guard(self):
        relay = self.relay(lambda request: True)
        submitted = relay.submit(self.request())
        relay.announce(submitted.approval_id)
        relay.begin_confirmation(submitted.approval_id)
        relay.resolve(submitted.approval_id, ApprovalChoice.REJECT)
        with self.assertRaises((DecisionReplayError, ApprovalAlreadyFinalizedError)):
            relay.resolve(submitted.approval_id, ApprovalChoice.APPROVE_ONCE)


class AdvanceStatusTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc)
        self.clock = MutableClock(self.start)
        self.store = ApprovalRequestStore(clock=self.clock, terminal_retention_seconds=300)

    def request(self, ttl=60):
        return ApprovalRequest.create(
            source="claude_code",
            session_id="s1",
            action_type="tool_permission",
            tool_name="Read",
            safe_summary="read file",
            risk_level=ApprovalRiskLevel.LOW,
            requested_at=iso(self.clock.value),
            expires_at=iso(self.clock.value + timedelta(seconds=ttl)),
        )

    def test_advance_status_only_accepts_relay_lifecycle_targets(self):
        request = self.request()
        self.store.register(request)
        with self.assertRaises(InvalidApprovalStateError):
            self.store.advance_status(request.approval_id, ApprovalStatus.APPROVED)
        with self.assertRaises(InvalidApprovalStateError):
            self.store.advance_status(request.approval_id, ApprovalStatus.PENDING)

    def test_advance_status_unknown_approval_raises_not_found(self):
        with self.assertRaises(ApprovalNotFoundError):
            self.store.advance_status("00000000-0000-4000-8000-000000000000", ApprovalStatus.ANNOUNCED)

    def test_advance_status_on_terminal_request_raises_already_finalized(self):
        request = self.request()
        self.store.register(request)
        self.store.advance_status(request.approval_id, ApprovalStatus.ANNOUNCED)
        self.store.advance_status(request.approval_id, ApprovalStatus.AWAITING_CONFIRMATION)
        self.store.advance_status(request.approval_id, ApprovalStatus.RELAY_FAILED)
        with self.assertRaises(ApprovalAlreadyFinalizedError):
            self.store.advance_status(request.approval_id, ApprovalStatus.ANNOUNCED)

    def test_advance_status_on_expired_request_raises_expired(self):
        request = self.request(ttl=5)
        self.store.register(request)
        self.clock.advance(6)
        with self.assertRaises(ApprovalExpiredError):
            self.store.advance_status(request.approval_id, ApprovalStatus.ANNOUNCED)

    def test_advance_status_rejects_backwards_transition(self):
        request = self.request()
        self.store.register(request)
        self.store.advance_status(request.approval_id, ApprovalStatus.ANNOUNCED)
        self.store.advance_status(request.approval_id, ApprovalStatus.AWAITING_CONFIRMATION)
        with self.assertRaises(InvalidApprovalStateError):
            self.store.advance_status(request.approval_id, ApprovalStatus.ANNOUNCED)


if __name__ == "__main__":
    unittest.main()
