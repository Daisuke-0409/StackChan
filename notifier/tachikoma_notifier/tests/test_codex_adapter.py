import unittest

from tachikoma_notifier.adapters.codex import CodexAdapter, validate_codex_hook_payload
from tachikoma_notifier.events import EventSeverity, EventSource, EventType


class CodexAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = CodexAdapter()

    def test_can_handle_requires_hook_event_name(self):
        self.assertTrue(self.adapter.can_handle({"hook_event_name": "Stop"}))
        self.assertFalse(self.adapter.can_handle({}))
        self.assertFalse(self.adapter.can_handle({"hook_event_name": 123}))

    def test_permission_request_maps_to_approval_needed(self):
        event = self.adapter.normalize({"hook_event_name": "PermissionRequest", "session_id": "s1"})
        self.assertIsNotNone(event)
        self.assertEqual(event.source, EventSource.CODEX)
        self.assertEqual(event.event_type, EventType.APPROVAL_NEEDED)
        self.assertEqual(event.severity, EventSeverity.WARNING)
        self.assertTrue(event.requires_action)
        self.assertEqual(event.session_id, "s1")
        self.assertEqual(event.raw_event_name, "PermissionRequest")

    def test_stop_maps_to_task_completed(self):
        event = self.adapter.normalize({"hook_event_name": "Stop", "session_id": "s1"})
        self.assertIsNotNone(event)
        self.assertEqual(event.event_type, EventType.TASK_COMPLETED)
        self.assertFalse(event.requires_action)

    def test_unmapped_events_are_safely_ignored(self):
        for name in ("SessionStart", "SubagentStart", "PreToolUse", "PostToolUse", "PreCompact", "PostCompact", "UserPromptSubmit", "SubagentStop"):
            with self.subTest(hook_event_name=name):
                self.assertIsNone(self.adapter.normalize({"hook_event_name": name, "session_id": "s1"}))

    def test_missing_hook_event_name_is_invalid(self):
        self.assertIsNone(self.adapter.normalize({"session_id": "s1"}))
        self.assertEqual(validate_codex_hook_payload({}), "hook_event_name is required")

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
