import json
import unittest
from datetime import datetime, timedelta, timezone

from tachikoma_notifier.approvals import (
    ApprovalActor,
    ApprovalChoice,
    ApprovalDecision,
    ApprovalDecisionType,
    ApprovalRequest,
    ApprovalRiskLevel,
    ApprovalStatus,
    ConfirmationMethod,
    DecisionReplayGuard,
    MAX_SAFE_SUMMARY_LENGTH,
    ReplayError,
    make_approval_dedupe_key,
    normalize_safe_summary,
)


def timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ApprovalModelTests(unittest.TestCase):
    def setUp(self):
        self.requested = datetime(2026, 7, 19, 1, 0, tzinfo=timezone.utc)
        self.expires = self.requested + timedelta(seconds=60)
        self.request = ApprovalRequest.create(
            source="claude_code",
            event_id="11111111-1111-4111-8111-111111111111",
            session_id="session-1",
            action_type="tool_permission",
            tool_name="Read",
            safe_summary="  Read   a project file\n  ",
            risk_level=ApprovalRiskLevel.LOW,
            requested_at=timestamp(self.requested),
            expires_at=timestamp(self.expires),
            metadata={"safe": "ok", "nested": {"Authorization": "Bearer hidden", "label": "x"}},
        )

    def test_request_normal_creation_and_ids(self):
        self.assertEqual(self.request.schema_version, "1.0")
        self.assertEqual(self.request.status, ApprovalStatus.PENDING)
        self.assertNotEqual(self.request.approval_id, self.request.event_id)
        self.assertEqual(self.request.safe_summary, "Read a project file")

    def test_decision_normal_creation_and_ids(self):
        decision = ApprovalDecision.create(
            self.request.approval_id,
            ApprovalDecisionType.APPROVE_ONCE,
            actor=ApprovalActor.USER,
            confirmation_method=ConfirmationMethod.MANUAL,
            decided_at=timestamp(self.requested + timedelta(seconds=1)),
        )
        self.assertNotEqual(decision.decision_id, decision.approval_id)
        decision.validate_for(self.request, now=self.requested + timedelta(seconds=1))

    def test_unique_ids(self):
        other = ApprovalRequest.create(source="claude_code", tool_name="Read", safe_summary="read")
        self.assertNotEqual(self.request.approval_id, other.approval_id)
        first = ApprovalDecision.create(self.request.approval_id, ApprovalDecisionType.REJECT)
        second = ApprovalDecision.create(self.request.approval_id, ApprovalDecisionType.REJECT)
        self.assertNotEqual(first.decision_id, second.decision_id)

    def test_timestamps_are_utc_and_expiry_is_after_request(self):
        self.assertTrue(self.request.requested_at.endswith("Z"))
        self.assertTrue(self.request.expires_at.endswith("Z"))
        parsed = datetime.fromisoformat(self.request.expires_at.replace("Z", "+00:00"))
        self.assertGreater(parsed, self.requested)

    def test_expiry_rejects_equal_or_before(self):
        with self.assertRaises(ValueError):
            ApprovalRequest.create(
                source="claude_code", tool_name="Read", safe_summary="read",
                requested_at=timestamp(self.requested), expires_at=timestamp(self.requested),
            )

    def test_expiry_is_injected_clock_and_boundary_is_inclusive(self):
        before = self.requested + timedelta(seconds=59, milliseconds=999)
        at = self.expires
        self.assertFalse(self.request.is_expired(before))
        self.assertTrue(self.request.is_expired(at))
        self.assertEqual(self.request.expire_if_needed(before).status, ApprovalStatus.PENDING)
        self.assertEqual(self.request.expire_if_needed(at).status, ApprovalStatus.EXPIRED)

    def test_expired_request_rejects_approve_once(self):
        decision = ApprovalDecision.create(self.request.approval_id, ApprovalDecisionType.APPROVE_ONCE)
        with self.assertRaises(ValueError):
            decision.validate_for(self.request, now=self.expires)

    def test_json_round_trip_request(self):
        decoded = ApprovalRequest.from_json(self.request.to_json())
        self.assertEqual(decoded, self.request)
        self.assertEqual(json.loads(decoded.to_json()), decoded.to_dict())

    def test_json_round_trip_decision(self):
        decision = ApprovalDecision.create(self.request.approval_id, ApprovalDecisionType.REJECT)
        decoded = ApprovalDecision.from_json(decision.to_json())
        self.assertEqual(decoded, decision)

    def test_invalid_json_is_rejected(self):
        with self.assertRaises(ValueError):
            ApprovalRequest.from_json("not-json")
        with self.assertRaises(ValueError):
            ApprovalDecision.from_json("[]")

    def test_missing_and_unknown_fields_are_rejected(self):
        data = self.request.to_dict()
        data.pop("tool_name")
        with self.assertRaises(ValueError):
            ApprovalRequest.from_dict(data)
        data = self.request.to_dict()
        data["unexpected"] = True
        with self.assertRaises(ValueError):
            ApprovalRequest.from_dict(data)

    def test_invalid_enum_is_rejected(self):
        data = self.request.to_dict()
        data["risk_level"] = "danger"
        with self.assertRaises(ValueError):
            ApprovalRequest.from_dict(data)

    def test_safe_summary_validation(self):
        with self.assertRaises(ValueError):
            normalize_safe_summary(" ")
        with self.assertRaises(ValueError):
            normalize_safe_summary("x" * (MAX_SAFE_SUMMARY_LENGTH + 1))
        with self.assertRaises(ValueError):
            ApprovalRequest.create(source="claude_code", tool_name="Read", safe_summary=" ")
        with self.assertRaises(ValueError):
            ApprovalRequest.create(source="claude_code", tool_name="Read", safe_summary="Bearer hidden-secret-value")

    def test_safe_summary_exact_limit_is_accepted_and_over_limit_rejected(self):
        exact = "x" * MAX_SAFE_SUMMARY_LENGTH
        request = ApprovalRequest.create(source="claude_code", tool_name="Read", safe_summary=exact)
        self.assertEqual(len(request.safe_summary), MAX_SAFE_SUMMARY_LENGTH)
        with self.assertRaises(ValueError):
            ApprovalRequest.create(source="claude_code", tool_name="Read", safe_summary=exact + "x")

    def test_timezone_offset_is_normalized_and_naive_clock_is_rejected(self):
        request = ApprovalRequest.create(
            source="claude_code", tool_name="Read", safe_summary="read",
            requested_at="2026-07-19T10:00:00+09:00",
            expires_at="2026-07-19T10:01:00+09:00",
        )
        self.assertEqual(request.requested_at, "2026-07-19T01:00:00.000Z")
        with self.assertRaises(ValueError):
            request.is_expired(datetime(2026, 7, 19, 1, 0))

    def test_choices_empty_duplicate_and_session_wide_are_rejected(self):
        for choices in ([], ["approve_once", "approve_once"], ["approve_session"]):
            data = self.request.to_dict()
            data["choices"] = choices
            with self.assertRaises(ValueError):
                ApprovalRequest.from_dict(data)

    def test_invalid_request_and_decision_ids_are_rejected(self):
        request_data = self.request.to_dict()
        request_data["approval_id"] = "not-a-uuid"
        with self.assertRaises(ValueError):
            ApprovalRequest.from_dict(request_data)
        decision = ApprovalDecision.create(self.request.approval_id, ApprovalDecisionType.REJECT)
        decision_data = decision.to_dict()
        decision_data["decision_id"] = "not-a-uuid"
        with self.assertRaises(ValueError):
            ApprovalDecision.from_dict(decision_data)

    def test_predictable_non_v4_ids_are_rejected(self):
        request_data = self.request.to_dict()
        request_data["approval_id"] = "11111111-1111-1111-8111-111111111111"
        with self.assertRaises(ValueError):
            ApprovalRequest.from_dict(request_data)
        decision = ApprovalDecision.create(self.request.approval_id, ApprovalDecisionType.REJECT)
        decision_data = decision.to_dict()
        decision_data["decision_id"] = "22222222-2222-2222-8222-222222222222"
        with self.assertRaises(ValueError):
            ApprovalDecision.from_dict(decision_data)

    def test_metadata_removes_secrets_recursively(self):
        encoded = self.request.to_json().lower()
        self.assertNotIn("authorization", encoded)
        self.assertNotIn("bearer hidden", encoded)
        self.assertIn('"safe":"ok"', encoded)
        decision = ApprovalDecision.create(
            self.request.approval_id,
            ApprovalDecisionType.REJECT,
            metadata={"nested": {"api_key": "secret", "safe": "yes"}},
        )
        self.assertNotIn("api_key", decision.to_json().lower())
        self.assertIn('"safe":"yes"', decision.to_json())

    def test_metadata_list_nested_secrets_and_deep_values(self):
        request = ApprovalRequest.create(
            source="claude_code", tool_name="Read", safe_summary="read",
            metadata={
                "items": [{"Cookie": "hidden", "safe": "yes"}],
                "deep": {"a": {"b": {"c": {"d": {"secret": "hidden", "safe": "ok"}}}}},
            },
        )
        encoded = request.to_json().lower()
        self.assertNotIn("cookie", encoded)
        self.assertNotIn("secret", encoded)
        self.assertIn('"safe":"yes"', encoded)

    def test_metadata_non_json_values_and_non_finite_numbers_are_rejected(self):
        class CustomValue:
            pass

        for value in (b"bytes", {"set"}, CustomValue(), float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValueError):
                ApprovalRequest.create(
                    source="claude_code", tool_name="Read", safe_summary="read",
                    metadata={"value": value},
                )

    def test_bool_metadata_is_not_coerced_to_integer(self):
        request = ApprovalRequest.create(
            source="claude_code", tool_name="Read", safe_summary="read",
            metadata={"flag": True, "count": 1},
        )
        self.assertIs(type(request.metadata["flag"]), bool)
        self.assertIs(type(request.metadata["count"]), int)

    def test_dedupe_key_ignores_ids_and_normalizes_summary(self):
        first = make_approval_dedupe_key("claude_code", "s", "tool_permission", "Read", "Read  file")
        second = make_approval_dedupe_key("claude_code", "s", "tool_permission", "Read", " Read\nfile ")
        self.assertEqual(first, second)
        self.assertNotIn(self.request.approval_id, first)
        self.assertNotIn(self.request.event_id, first)
        self.assertTrue(first.startswith("v1:"))

    def test_same_content_different_approval_ids_has_same_dedupe_key(self):
        first = ApprovalRequest.create(
            source="claude_code", session_id="s", action_type="tool_permission",
            tool_name="Read", safe_summary="Read  FILE",
        )
        second = ApprovalRequest.create(
            source="claude_code", session_id="s", action_type="tool_permission",
            tool_name="read", safe_summary=" read\nfile ",
        )
        self.assertNotEqual(first.approval_id, second.approval_id)
        self.assertEqual(first.dedupe_key, second.dedupe_key)

    def test_session_id_alone_does_not_make_dedupe_key(self):
        first = make_approval_dedupe_key("claude_code", "s", "tool_permission", "Read", "read")
        second = make_approval_dedupe_key("claude_code", "s", "tool_permission", "Write", "read")
        self.assertNotEqual(first, second)

    def test_state_transitions_and_terminal_protection(self):
        awaiting = self.request.transition_to(ApprovalStatus.AWAITING_CONFIRMATION)
        approved = awaiting.transition_to(ApprovalStatus.APPROVED)
        self.assertEqual(approved.status, ApprovalStatus.APPROVED)
        with self.assertRaises(ValueError):
            approved.transition_to(ApprovalStatus.REJECTED)
        with self.assertRaises(ValueError):
            self.request.transition_to(ApprovalStatus.APPROVED)
        with self.assertRaises(ValueError):
            self.request.transition_to(ApprovalStatus.PENDING)

    def test_all_required_state_transitions(self):
        self.assertEqual(self.request.transition_to(ApprovalStatus.ANNOUNCED).status, ApprovalStatus.ANNOUNCED)
        announced = self.request.transition_to(ApprovalStatus.ANNOUNCED)
        self.assertEqual(announced.transition_to(ApprovalStatus.AWAITING_CONFIRMATION).status,
                         ApprovalStatus.AWAITING_CONFIRMATION)
        awaiting = announced.transition_to(ApprovalStatus.AWAITING_CONFIRMATION)
        self.assertEqual(awaiting.transition_to(ApprovalStatus.RELAY_FAILED).status, ApprovalStatus.RELAY_FAILED)

    def test_replay_guard_rejects_decision_and_approval_reuse(self):
        guard = DecisionReplayGuard()
        decision = ApprovalDecision.create(self.request.approval_id, ApprovalDecisionType.REJECT)
        guard.consume(decision)
        with self.assertRaises(ReplayError):
            guard.consume(decision)
        different_id = ApprovalDecision.create(self.request.approval_id, ApprovalDecisionType.REJECT)
        with self.assertRaises(ReplayError):
            guard.consume(different_id)

    def test_expired_request_accepts_only_expired_internal_decision(self):
        now = self.expires
        reject = ApprovalDecision.create(self.request.approval_id, ApprovalDecisionType.REJECT)
        cancel = ApprovalDecision.create(self.request.approval_id, ApprovalDecisionType.CANCEL)
        expired = ApprovalDecision.create(self.request.approval_id, ApprovalDecisionType.EXPIRED)
        with self.assertRaises(ValueError):
            reject.validate_for(self.request, now=now)
        with self.assertRaises(ValueError):
            cancel.validate_for(self.request, now=now)
        expired.validate_for(self.request, now=now)

    def test_approval_request_is_distinct_from_tachikoma_event(self):
        self.assertNotIn("event_type", self.request.to_dict())
        self.assertNotIn("message", self.request.to_dict())


if __name__ == "__main__":
    unittest.main()
