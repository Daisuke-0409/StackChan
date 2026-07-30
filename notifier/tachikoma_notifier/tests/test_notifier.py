import json
import os
import threading
import unittest
from datetime import datetime
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tachikoma_notifier.adapters.claude_code import ClaudeCodeAdapter
from tachikoma_notifier.events import (
    EventSeverity,
    EventSource,
    EventType,
    TachikomaEvent,
    make_dedupe_key,
    make_event_id,
    utc_now_iso,
)
from tachikoma_notifier.notifier import (
    EventNormalizer,
    LogSpeechSink,
    NotifierHandler,
    NotifierService,
    SpeechSink,
    WindowsSpeechSink,
    _build_sink,
)
from tachikoma_notifier.routing import SpeechFormatter
from tachikoma_notifier.stackchan_speech_sink import StackChanSpeechSink


class CaptureSink(SpeechSink):
    def __init__(self, output):
        self.output = output

    def speak(self, phrase):
        self.output.append(phrase)


class NotifierTests(unittest.TestCase):
    def setUp(self):
        self.spoken = []
        self.logs = []
        self.service = NotifierService(
            "test-token",
            CaptureSink(self.spoken),
            debounce_seconds=5,
            logger=self.logs.append,
        )

    def test_permission_prompt_to_common_event(self):
        event = self.service.handle({
            "hook_event_name": "Notification",
            "notification_type": "permission_prompt",
            "session_id": "s1",
        }, now=10)
        self.assertEqual(event.state, "approval_needed")
        self.assertEqual(event.event.event_type, EventType.APPROVAL_NEEDED)
        self.assertEqual(event.event.source, EventSource.CLAUDE_CODE)
        self.assertTrue(event.event.requires_action)
        self.assertEqual(event.event.message, "承認待ち")
        self.assertEqual(self.spoken, ["Claude Codeが承認待ちだよ"])

    def test_stop_to_task_completed(self):
        event = self.service.handle({"hook_event_name": "Stop", "session_id": "s1"}, now=10)
        self.assertEqual(event.state, "completed")
        self.assertEqual(event.event.event_type, EventType.TASK_COMPLETED)
        self.assertFalse(event.event.requires_action)

    def test_subagent_stop_is_not_completion(self):
        # agent_id/agent_type mark this as a subagent's Stop, not the main
        # session's -- see ClaudeCodeAdapter's dedicated tests for full
        # coverage of this behavior.
        self.assertIsNone(self.service.handle({
            "hook_event_name": "Stop", "session_id": "s1", "agent_id": "reviewer"
        }, now=10))

    def test_post_tool_use_failure_to_tool_failed(self):
        event = self.service.handle({"hook_event_name": "PostToolUseFailure", "session_id": "s1"}, now=10)
        self.assertEqual(event.state, "error")
        self.assertEqual(event.event.event_type, EventType.TOOL_FAILED)
        self.assertEqual(event.event.severity, EventSeverity.ERROR)
        self.assertEqual(self.spoken, ["ツールの実行でエラーが出たみたい"])

    def test_stop_failure_to_task_failed(self):
        event = self.service.handle({"hook_event_name": "StopFailure", "session_id": "s1"}, now=10)
        self.assertEqual(event.event.event_type, EventType.TASK_FAILED)
        self.assertEqual(self.spoken, ["タスクが失敗したみたい"])

    def test_unknown_event_is_safe(self):
        self.assertIsNone(self.service.handle({"hook_event_name": "FutureEvent", "session_id": "s1"}))
        self.assertEqual(self.spoken, [])

    def test_invalid_json(self):
        with self._server() as server:
            request = Request(
                f"http://127.0.0.1:{server.server_port}/events",
                data=b"{not-json",
                headers={"Authorization": "Bearer test-token", "Content-Type": "application/json"},
            )
            with self.assertRaises(HTTPError) as raised:
                urlopen(request, timeout=2)
            self.assertEqual(raised.exception.code, 400)

    def test_missing_required_field(self):
        with self._server() as server:
            response = self._post(server, {})
            self.assertEqual(response, 400)

    def test_message_max_length(self):
        with self._server() as server:
            response = self._post(server, {"hook_event_name": "Stop", "message": "x" * 2001})
            self.assertEqual(response, 400)

    def test_event_id_is_unique(self):
        adapter = ClaudeCodeAdapter()
        payload = {"hook_event_name": "Stop", "session_id": "s1"}
        first = adapter.normalize(payload)
        second = adapter.normalize(payload)
        self.assertNotEqual(first.event_id, second.event_id)

    def test_occurred_at_is_utc_iso8601(self):
        event = ClaudeCodeAdapter().normalize({"hook_event_name": "Stop"})
        parsed = datetime.fromisoformat(event.occurred_at.replace("Z", "+00:00"))
        self.assertIsNotNone(parsed.tzinfo)
        self.assertTrue(event.occurred_at.endswith("Z"))

    def test_json_serialization(self):
        event = ClaudeCodeAdapter().normalize({"hook_event_name": "Stop", "session_id": "s1"})
        decoded = json.loads(event.to_json())
        self.assertEqual(decoded["schema_version"], "1.0")
        self.assertEqual(decoded["event_type"], "task_completed")
        self.assertEqual(decoded["source"], "claude_code")

    def test_dedupe_key_is_stable_and_not_event_id(self):
        adapter = ClaudeCodeAdapter()
        first = adapter.normalize({"hook_event_name": "Stop", "session_id": "s1"})
        second = adapter.normalize({"hook_event_name": "Stop", "session_id": "s1"})
        other = adapter.normalize({"hook_event_name": "StopFailure", "session_id": "s1"})
        self.assertEqual(first.dedupe_key, second.dedupe_key)
        self.assertNotEqual(first.event_id, second.event_id)
        self.assertNotEqual(first.dedupe_key, other.dedupe_key)
        self.assertTrue(first.dedupe_key.startswith("v1:"))

    def test_deduplicates_within_five_seconds(self):
        payload = {"hook_event_name": "Notification", "notification_type": "permission_prompt", "session_id": "s1"}
        self.service.handle(payload, now=10)
        self.assertIsNone(self.service.handle(payload, now=14.999))
        self.assertEqual(len(self.spoken), 1)

    def test_re_notifies_after_five_seconds(self):
        payload = {"hook_event_name": "Notification", "notification_type": "permission_prompt", "session_id": "s1"}
        self.service.handle(payload, now=10)
        self.assertIsNotNone(self.service.handle(payload, now=15))
        self.assertEqual(len(self.spoken), 2)

    def test_authentication_success(self):
        self.assertTrue(self.service.authorized("Bearer test-token"))

    def test_authentication_failure(self):
        self.assertFalse(self.service.authorized("Bearer wrong"))

    def test_http_authentication_and_notification(self):
        with self._server() as server:
            payload = {
                "hook_event_name": "Notification",
                "notification_type": "permission_prompt",
                "session_id": "http-session",
            }
            body = json.dumps(payload).encode()
            with self.assertRaises(HTTPError) as raised:
                urlopen(Request(
                    f"http://127.0.0.1:{server.server_port}/events",
                    data=body,
                    headers={"Authorization": "Bearer wrong", "Content-Type": "application/json"},
                ), timeout=2)
            self.assertEqual(raised.exception.code, 401)
            request = Request(
                f"http://127.0.0.1:{server.server_port}/events",
                data=body,
                headers={"Authorization": "Bearer test-token", "Content-Type": "application/json"},
            )
            with urlopen(request, timeout=2) as response:
                self.assertEqual(response.status, 200)
                self.assertTrue(json.loads(response.read())["notified"])

    def test_speech_formatter_mapping(self):
        adapter = ClaudeCodeAdapter()
        formatter = SpeechFormatter()
        self.assertEqual(formatter.format(adapter.normalize({
            "hook_event_name": "Notification", "notification_type": "permission_prompt"
        })), "Claude Codeが承認待ちだよ")
        self.assertEqual(formatter.format(adapter.normalize({"hook_event_name": "Stop"})), "タスクが終わったよ")

    def test_metadata_drops_sensitive_keys(self):
        event = TachikomaEvent(
            schema_version="1.0",
            event_id=make_event_id(),
            source=EventSource.SYSTEM,
            event_type=EventType.INFO,
            severity=EventSeverity.INFO,
            occurred_at=utc_now_iso(),
            title="test",
            message="safe",
            requires_action=False,
            dedupe_key=make_dedupe_key(EventSource.SYSTEM, None, EventType.INFO, "safe"),
            metadata={"api_key": "dummy-secret", "token": "dummy-token", "safe": "ok", "note": "Bearer dummy-token"},
        )
        self.assertNotIn("api_key", event.metadata)
        self.assertNotIn("token", event.metadata)
        self.assertNotIn("note", event.metadata)
        self.assertEqual(event.metadata["safe"], "ok")

    def test_existing_normalizer_facade(self):
        event = EventNormalizer().normalize({
            "hook_event_name": "Notification",
            "notification_type": "permission_prompt",
            "session_id": "legacy",
        })
        self.assertEqual(event.state, "approval_needed")
        self.assertEqual(event.phrase, "Claude Codeが承認待ちだよ")

    def test_existing_hook_payload_compatibility(self):
        payload = {
            "hook_event_name": "Stop",
            "session_id": "legacy",
            "cwd": "C:/project",
            "transcript_path": "C:/private/transcript.jsonl",
        }
        event = self.service.handle(payload, now=10)
        self.assertEqual(event.state, "completed")
        self.assertNotIn("transcript", event.event.to_json().lower())

    def _server(self):
        handler = type("BoundNotifierHandler", (NotifierHandler,), {"service": self.service})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return _ServerContext(server)

    def _post(self, server, payload):
        request = Request(
            f"http://127.0.0.1:{server.server_port}/events",
            data=json.dumps(payload).encode(),
            headers={"Authorization": "Bearer test-token", "Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=2) as response:
                return response.status
        except HTTPError as error:
            return error.code


class _ServerContext:
    def __init__(self, server):
        self.server = server
        self.server_port = server.server_port

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.server.shutdown()
        self.server.server_close()


class BuildSinkTests(unittest.TestCase):
    """_build_sink() chooses StackChanSpeechSink only when fully configured."""

    _ENV_VARS = (
        "TACHIKOMA_STACKCHAN_SPEAK_URL",
        "TACHIKOMA_STACKCHAN_DEVICE_TOKEN",
        "TACHIKOMA_STACKCHAN_DEVICE_ID",
    )

    def test_log_only_wins_regardless_of_env(self):
        env = {name: "x" for name in self._ENV_VARS}
        with patch.dict("os.environ", env, clear=False):
            self.assertIsInstance(_build_sink(log_only=True), LogSpeechSink)

    def test_no_stackchan_env_vars_falls_back_to_windows_sink(self):
        with patch.dict("os.environ", {}, clear=False):
            for name in self._ENV_VARS:
                os.environ.pop(name, None)
            self.assertIsInstance(_build_sink(log_only=False), WindowsSpeechSink)

    def test_partial_stackchan_env_vars_fall_back_to_windows_sink(self):
        with patch.dict("os.environ", {}, clear=False):
            for name in self._ENV_VARS:
                os.environ.pop(name, None)
            os.environ["TACHIKOMA_STACKCHAN_SPEAK_URL"] = "https://gateway.example/v1/speak"
            os.environ["TACHIKOMA_STACKCHAN_DEVICE_TOKEN"] = "token"
            # TACHIKOMA_STACKCHAN_DEVICE_ID intentionally left unset.
            self.assertIsInstance(_build_sink(log_only=False), WindowsSpeechSink)

    def test_all_stackchan_env_vars_selects_stackchan_sink_with_windows_fallback(self):
        env = {
            "TACHIKOMA_STACKCHAN_SPEAK_URL": "https://gateway.example/v1/speak",
            "TACHIKOMA_STACKCHAN_DEVICE_TOKEN": "token",
            "TACHIKOMA_STACKCHAN_DEVICE_ID": "device-1",
        }
        with patch.dict("os.environ", env, clear=False):
            sink = _build_sink(log_only=False)
            self.assertIsInstance(sink, StackChanSpeechSink)
            self.assertIsInstance(sink._fallback, WindowsSpeechSink)


if __name__ == "__main__":
    unittest.main()
