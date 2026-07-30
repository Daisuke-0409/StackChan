import unittest
from datetime import datetime, timedelta, timezone

from tachikoma_notifier.approval_store import (
    ApprovalAlreadyFinalizedError,
    ApprovalRequestStore,
    DecisionReplayError,
)
from tachikoma_notifier.approvals import ApprovalRequest, ApprovalRiskLevel, ApprovalStatus
from tachikoma_notifier.manual_approval_cli import ManualApprovalCli


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class MutableClock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class ManualApprovalCliTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc)
        self.clock = MutableClock(self.start)
        self.store = ApprovalRequestStore(clock=self.clock, terminal_retention_seconds=300)
        self.output = []
        self.cli = ManualApprovalCli(self.store, output_fn=self.output.append)

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

    def test_run_once_with_no_pending_requests(self):
        result = self.cli.run_once()
        self.assertIsNone(result)
        self.assertTrue(any("no pending" in line for line in self.output))

    def test_run_once_with_multiple_pending_requests_asks_to_specify(self):
        self.store.register(self.request(session="s1"))
        self.store.register(self.request(session="s2"))
        result = self.cli.run_once()
        self.assertIsNone(result)
        self.assertTrue(any("2 pending" in line for line in self.output))

    def test_run_once_approve_once(self):
        request = self.store.register(self.request())
        self.store.advance_status(request.approval_id, ApprovalStatus.ANNOUNCED)
        self.store.advance_status(request.approval_id, ApprovalStatus.AWAITING_CONFIRMATION)
        cli = ManualApprovalCli(self.store, input_fn=lambda prompt: "1", output_fn=self.output.append)
        result = cli.run_once()
        self.assertEqual(result.status.value, "approved")

    def test_run_once_reject(self):
        self.store.register(self.request())
        cli = ManualApprovalCli(self.store, input_fn=lambda prompt: "2", output_fn=self.output.append)
        result = cli.run_once()
        self.assertEqual(result.status.value, "rejected")

    def test_invalid_choice_raises(self):
        self.store.register(self.request())
        cli = ManualApprovalCli(self.store, input_fn=lambda prompt: "9", output_fn=self.output.append)
        with self.assertRaises(ValueError):
            cli.run_once()

    def test_double_decision_is_rejected(self):
        self.store.register(self.request())
        cli = ManualApprovalCli(self.store, input_fn=lambda prompt: "2", output_fn=self.output.append)
        first = cli.run_once()
        self.assertEqual(first.status.value, "rejected")
        with self.assertRaises((ApprovalAlreadyFinalizedError, DecisionReplayError)):
            cli.prompt_for_id(first.approval_id)

    def test_prompt_for_id_targets_a_specific_request_among_several(self):
        first = self.store.register(self.request(session="s1"))
        self.store.register(self.request(session="s2"))
        cli = ManualApprovalCli(self.store, input_fn=lambda prompt: "2", output_fn=self.output.append)
        result = cli.prompt_for_id(first.approval_id)
        self.assertEqual(result.approval_id, first.approval_id)
        self.assertEqual(result.status.value, "rejected")


if __name__ == "__main__":
    unittest.main()
