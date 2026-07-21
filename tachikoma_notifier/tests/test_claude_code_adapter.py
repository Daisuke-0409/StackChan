import unittest

from tachikoma_notifier.adapters.claude_code import ClaudeCodeAdapter, validate_hook_payload
from tachikoma_notifier.events import EventSeverity, EventSource, EventType


class ClaudeCodeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = ClaudeCodeAdapter()

    def test_can_handle_requires_hook_event_name(self):
        self.assertTrue(self.adapter.can_handle({"hook_event_name": "Stop"}))
        self.assertFalse(self.adapter.can_handle({}))
        self.assertFalse(self.adapter.can_handle({"hook_event_name": 123}))

    def test_permission_prompt_maps_to_approval_needed(self):
        # Real Notification payload shape (all 8 fields Claude Code sends).
        payload = {
            "session_id": "sess_abc123xyz",
            "prompt_id": "550e8400-e29b-41d4-a716-446655440000",
            "transcript_path": "/Users/you/.claude/projects/my-project/.claude/transcript.jsonl",
            "cwd": "/Users/you/my-project",
            "permission_mode": "default",
            "hook_event_name": "Notification",
            "message": "Claude needs your permission to run: bash (command: npm test)",
            "notification_type": "permission_prompt",
        }
        event = self.adapter.normalize(payload)
        self.assertIsNotNone(event)
        self.assertEqual(event.source, EventSource.CLAUDE_CODE)
        self.assertEqual(event.event_type, EventType.APPROVAL_NEEDED)
        self.assertEqual(event.severity, EventSeverity.WARNING)
        self.assertTrue(event.requires_action)
        self.assertEqual(event.session_id, "sess_abc123xyz")

    def test_other_real_notification_types_are_ignored(self):
        # These are real notification_type values (per Claude Code hooks
        # docs) that are out of scope for now -- must not raise, must not
        # notify.
        for notification_type in (
            "idle_prompt",
            "auth_success",
            "elicitation_dialog",
            "elicitation_complete",
            "elicitation_response",
            "agent_needs_input",
            "agent_completed",
        ):
            with self.subTest(notification_type=notification_type):
                event = self.adapter.normalize({
                    "hook_event_name": "Notification",
                    "notification_type": notification_type,
                    "session_id": "s1",
                })
                self.assertIsNone(event)

    def test_unknown_notification_type_does_not_raise(self):
        # Fail-closed: an unrecognized/future value must not crash the adapter.
        event = self.adapter.normalize({
            "hook_event_name": "Notification",
            "notification_type": "something_claude_code_added_later",
            "session_id": "s1",
        })
        self.assertIsNone(event)

    def test_fictional_error_notification_types_no_longer_produce_error_events(self):
        # Regression guard: these values were never real (see investigation
        # notes); confirm the removed dead-code path is really gone.
        for notification_type in ("error", "error_notification", "notification_error"):
            with self.subTest(notification_type=notification_type):
                event = self.adapter.normalize({
                    "hook_event_name": "Notification",
                    "notification_type": notification_type,
                    "session_id": "s1",
                })
                self.assertIsNone(event)

    def test_main_session_stop_is_notified_as_task_completed(self):
        # Real Stop payload shape for the main session (no agent_id/agent_type).
        payload = {
            "session_id": "sess_abc123xyz",
            "prompt_id": "550e8400-e29b-41d4-a716-446655440000",
            "transcript_path": "/Users/you/.claude/projects/my-project/.claude/transcript.jsonl",
            "cwd": "/Users/you/my-project",
            "permission_mode": "default",
            "hook_event_name": "Stop",
            "effort": {"level": "high"},
            "last_assistant_message": "I've analyzed the codebase and created a plan for refactoring.",
        }
        event = self.adapter.normalize(payload)
        self.assertIsNotNone(event)
        self.assertEqual(event.event_type, EventType.TASK_COMPLETED)
        self.assertFalse(event.requires_action)

    def test_subagent_stop_is_not_notified(self):
        # Real Stop payload shape for a subagent context (agent_id/agent_type
        # present). Must be ignored, not read as overall task completion.
        with_agent_id = {
            "hook_event_name": "Stop",
            "session_id": "sess_abc123xyz",
            "last_assistant_message": "Code review complete. Found 3 issues.",
            "agent_id": "reviewer",
            "agent_type": "code-reviewer",
        }
        self.assertIsNone(self.adapter.normalize(with_agent_id))

        with_agent_id_only = {"hook_event_name": "Stop", "session_id": "s1", "agent_id": "reviewer"}
        self.assertIsNone(self.adapter.normalize(with_agent_id_only))

        with_agent_type_only = {"hook_event_name": "Stop", "session_id": "s1", "agent_type": "code-reviewer"}
        self.assertIsNone(self.adapter.normalize(with_agent_type_only))

    def test_post_tool_use_failure_maps_to_tool_failed(self):
        event = self.adapter.normalize({"hook_event_name": "PostToolUseFailure", "session_id": "s1"})
        self.assertIsNotNone(event)
        self.assertEqual(event.event_type, EventType.TOOL_FAILED)
        self.assertEqual(event.severity, EventSeverity.ERROR)

    def test_stop_failure_maps_to_task_failed(self):
        event = self.adapter.normalize({"hook_event_name": "StopFailure", "session_id": "s1"})
        self.assertIsNotNone(event)
        self.assertEqual(event.event_type, EventType.TASK_FAILED)
        self.assertEqual(event.severity, EventSeverity.ERROR)

    def test_unmapped_events_are_safely_ignored(self):
        for name in ("SessionStart", "SubagentStart", "PreToolUse", "PostToolUse", "PreCompact", "PostCompact",
                     "UserPromptSubmit", "SubagentStop"):
            with self.subTest(hook_event_name=name):
                self.assertIsNone(self.adapter.normalize({"hook_event_name": name, "session_id": "s1"}))

    def test_missing_hook_event_name_is_invalid(self):
        self.assertIsNone(self.adapter.normalize({"session_id": "s1"}))
        self.assertEqual(validate_hook_payload({}), "hook_event_name is required")

    def test_oversized_session_id_is_invalid(self):
        payload = {"hook_event_name": "Stop", "session_id": "s" * 200}
        self.assertIsNone(self.adapter.normalize(payload))

    def test_missing_session_id_produces_none_session_id(self):
        event = self.adapter.normalize({"hook_event_name": "Stop"})
        self.assertIsNone(event.session_id)

    def test_dedupe_key_is_stable_for_same_content(self):
        first = self.adapter.normalize({"hook_event_name": "Stop", "session_id": "s1"})
        second = self.adapter.normalize({"hook_event_name": "Stop", "session_id": "s1"})
        self.assertEqual(first.dedupe_key, second.dedupe_key)
        self.assertNotEqual(first.event_id, second.event_id)


if __name__ == "__main__":
    unittest.main()
