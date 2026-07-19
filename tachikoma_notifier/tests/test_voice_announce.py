import unittest
from datetime import datetime, timedelta, timezone

from tachikoma_notifier.approvals import ApprovalRequest, ApprovalRiskLevel
from tachikoma_notifier.voice_announce import build_speech_announcer, format_announcement


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class VoiceAnnounceTests(unittest.TestCase):
    def setUp(self):
        now = datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc)
        self.request = ApprovalRequest.create(
            source="claude_code",
            session_id="s1",
            action_type="tool_permission",
            tool_name="Read",
            safe_summary="read config.json",
            risk_level=ApprovalRiskLevel.LOW,
            requested_at=iso(now),
            expires_at=iso(now + timedelta(seconds=60)),
        )

    def test_format_announcement_includes_tool_and_summary(self):
        phrase = format_announcement(self.request)
        self.assertIn("Read", phrase)
        self.assertIn("read config.json", phrase)

    def test_build_speech_announcer_calls_speak_with_formatted_phrase_and_returns_bool(self):
        calls = []

        def speak(phrase: str) -> bool:
            calls.append(phrase)
            return True

        announcer = build_speech_announcer(speak)
        result = announcer(self.request)
        self.assertTrue(result)
        self.assertEqual(calls, [format_announcement(self.request)])

    def test_build_speech_announcer_propagates_falsy_result(self):
        announcer = build_speech_announcer(lambda phrase: False)
        self.assertFalse(announcer(self.request))

    def test_build_speech_announcer_coerces_non_bool_return(self):
        announcer = build_speech_announcer(lambda phrase: None)
        self.assertFalse(announcer(self.request))


if __name__ == "__main__":
    unittest.main()
