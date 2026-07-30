import unittest
from datetime import datetime, timedelta, timezone

from tachikoma_notifier.approval_store import ApprovalRequestStore, DecisionReplayError
from tachikoma_notifier.approvals import ApprovalRequest, ApprovalRiskLevel, ApprovalStatus
from tachikoma_notifier.voice_approval_gate import (
    VoiceApprovalDenied,
    VoiceApprovalGate,
    normalize_utterance,
)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class MutableClock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value = self.value + timedelta(seconds=seconds)


class VoiceApprovalGateTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc)
        self.clock = MutableClock(self.start)
        self.store = ApprovalRequestStore(clock=self.clock, terminal_retention_seconds=300)
        self.gate = VoiceApprovalGate(self.store)

    def request(self, *, ttl=60, session="s1", tool="Read", risk=ApprovalRiskLevel.LOW):
        return ApprovalRequest.create(
            source="claude_code",
            session_id=session,
            action_type="tool_permission",
            tool_name=tool,
            safe_summary="read config.json",
            risk_level=risk,
            requested_at=iso(self.clock.value),
            expires_at=iso(self.clock.value + timedelta(seconds=ttl)),
        )

    def awaiting(self, request):
        self.store.register(request)
        self.store.advance_status(request.approval_id, ApprovalStatus.ANNOUNCED)
        self.store.advance_status(request.approval_id, ApprovalStatus.AWAITING_CONFIRMATION)
        return request

    # -- happy path --------------------------------------------------------

    def test_exact_approval_phrase_approves(self):
        request = self.awaiting(self.request())
        result = self.gate.try_approve(request.approval_id, "承認します")
        self.assertEqual(result.status, ApprovalStatus.APPROVED)

    def test_normalization_handles_fullwidth_and_trailing_punctuation(self):
        request = self.awaiting(self.request())
        result = self.gate.try_approve(request.approval_id, "承認します。")
        self.assertEqual(result.status, ApprovalStatus.APPROVED)

    # -- each gating condition denies on its own ---------------------------

    def test_denies_when_more_than_one_pending(self):
        first = self.awaiting(self.request(session="s1"))
        self.store.register(self.request(session="s2"))
        with self.assertRaises(VoiceApprovalDenied):
            self.gate.try_approve(first.approval_id, "承認します")

    def test_denies_when_no_pending(self):
        with self.assertRaises(VoiceApprovalDenied):
            self.gate.try_approve("00000000-0000-4000-8000-000000000000", "承認します")

    def test_denies_when_approval_id_does_not_match_single_pending(self):
        self.awaiting(self.request())
        with self.assertRaises(VoiceApprovalDenied):
            self.gate.try_approve("00000000-0000-4000-8000-000000000000", "承認します")

    def test_denies_when_risk_level_is_not_low(self):
        request = self.awaiting(self.request(risk=ApprovalRiskLevel.MEDIUM))
        with self.assertRaises(VoiceApprovalDenied):
            self.gate.try_approve(request.approval_id, "承認します")

    def test_denies_when_not_yet_awaiting_confirmation(self):
        request = self.store.register(self.request())
        with self.assertRaises(VoiceApprovalDenied):
            self.gate.try_approve(request.approval_id, "承認します")

    def test_denies_when_tool_not_on_safe_allowlist(self):
        request = self.awaiting(self.request(tool="Bash"))
        with self.assertRaises(VoiceApprovalDenied):
            self.gate.try_approve(request.approval_id, "承認します")

    def test_denies_write_tool(self):
        request = self.awaiting(self.request(tool="Write"))
        with self.assertRaises(VoiceApprovalDenied):
            self.gate.try_approve(request.approval_id, "承認します")

    def test_denies_when_expired(self):
        request = self.awaiting(self.request(ttl=5))
        self.clock.advance(6)
        with self.assertRaises(VoiceApprovalDenied):
            self.gate.try_approve(request.approval_id, "承認します")

    def test_denies_non_matching_utterance(self):
        request = self.awaiting(self.request())
        with self.assertRaises(VoiceApprovalDenied):
            self.gate.try_approve(request.approval_id, "たぶん大丈夫")

    def test_denies_negation_despite_containing_approval_substring(self):
        request = self.awaiting(self.request())
        with self.assertRaises(VoiceApprovalDenied):
            self.gate.try_approve(request.approval_id, "承認しません")

    def test_denies_partial_match(self):
        request = self.awaiting(self.request())
        with self.assertRaises(VoiceApprovalDenied):
            self.gate.try_approve(request.approval_id, "うーん承認します、かな")

    def test_second_voice_approval_attempt_is_replay_rejected(self):
        request = self.awaiting(self.request())
        self.gate.try_approve(request.approval_id, "承認します")
        with self.assertRaises((VoiceApprovalDenied, DecisionReplayError)):
            self.gate.try_approve(request.approval_id, "承認します")


class NormalizeUtteranceTests(unittest.TestCase):
    def test_strips_whitespace_and_trailing_punctuation(self):
        self.assertEqual(normalize_utterance(" 承認 します。 "), "承認します")

    def test_fullwidth_exclamation_is_stripped(self):
        self.assertEqual(normalize_utterance("承認します！"), "承認します")


if __name__ == "__main__":
    unittest.main()
