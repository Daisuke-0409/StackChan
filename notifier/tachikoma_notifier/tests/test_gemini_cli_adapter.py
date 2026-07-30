import unittest

from tachikoma_notifier.adapters.gemini_cli import GeminiCliAdapter, validate_gemini_cli_hook_payload
from tachikoma_notifier.events import EventSeverity, EventSource, EventType


class GeminiCliAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = GeminiCliAdapter()

    def test_can_handle_requires_hook_event_name(self):
        self.assertTrue(self.adapter.can_handle({"hook_event_name": "Notification"}))
        self.assertFalse(self.adapter.can_handle({}))

    def test_tool_permission_notification_maps_to_approval_needed(self):
        event = self.adapter.normalize({
            "hook_event_name": "Notification",
            "notification_type": "ToolPermission",
            "session_id": "s1",
        })
        self.assertIsNotNone(event)
        self.assertEqual(event.source, EventSource.GEMINI_CLI)
        self.assertEqual(event.event_type, EventType.APPROVAL_NEEDED)
        self.assertEqual(event.severity, EventSeverity.WARNING)
        self.assertTrue(event.requires_action)
        self.assertEqual(event.session_id, "s1")

    def test_other_notification_types_are_safely_ignored(self):
        event = self.adapter.normalize({
            "hook_event_name": "Notification",
            "notification_type": "SomethingUnconfirmed",
            "session_id": "s1",
        })
        self.assertIsNone(event)

    def test_unmapped_hook_events_are_safely_ignored(self):
        for name in ("SessionStart", "SessionEnd", "BeforeTool", "AfterTool", "BeforeAgent", "AfterAgent", "PreCompress"):
            with self.subTest(hook_event_name=name):
                self.assertIsNone(self.adapter.normalize({"hook_event_name": name, "session_id": "s1"}))

    def test_missing_hook_event_name_is_invalid(self):
        self.assertIsNone(self.adapter.normalize({"session_id": "s1"}))
        self.assertEqual(validate_gemini_cli_hook_payload({}), "hook_event_name is required")

    def test_non_string_notification_type_is_invalid(self):
        payload = {"hook_event_name": "Notification", "notification_type": 42, "session_id": "s1"}
        self.assertIsNone(self.adapter.normalize(payload))

    def test_oversized_session_id_is_invalid(self):
        payload = {"hook_event_name": "Notification", "notification_type": "ToolPermission", "session_id": "s" * 200}
        self.assertIsNone(self.adapter.normalize(payload))


if __name__ == "__main__":
    unittest.main()
