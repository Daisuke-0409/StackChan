import unittest
import json
import threading
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tachikoma_notifier.notifier import NotifierHandler, NotifierService, SpeechSink


class CaptureSink(SpeechSink):
    def __init__(self, output):
        self.output = output

    def speak(self, phrase):
        self.output.append(phrase)


class NotifierTests(unittest.TestCase):
    def setUp(self):
        self.spoken = []
        self.service = NotifierService("test-token", CaptureSink(self.spoken), debounce_seconds=5)

    def test_permission_notification(self):
        event = self.service.handle({
            "hook_event_name": "Notification",
            "notification_type": "permission_prompt",
            "session_id": "s1",
        }, now=10)
        self.assertEqual(event.state, "approval_needed")
        self.assertEqual(self.spoken, ["Claude Codeが承認待ちだよ"])

    def test_stop_completion(self):
        event = self.service.handle({"hook_event_name": "Stop", "session_id": "s1"}, now=10)
        self.assertEqual(event.state, "completed")

    def test_background_stop_is_not_completion(self):
        self.assertIsNone(self.service.handle({
            "hook_event_name": "Stop", "session_id": "s1", "background_tasks": [{"id": "t"}]
        }, now=10))

    def test_failure(self):
        event = self.service.handle({"hook_event_name": "PostToolUseFailure", "session_id": "s1"}, now=10)
        self.assertEqual(event.state, "error")

    def test_deduplicates(self):
        payload = {"hook_event_name": "Notification", "notification_type": "permission_prompt", "session_id": "s1"}
        self.service.handle(payload, now=10)
        self.assertIsNone(self.service.handle(payload, now=12))

    def test_authentication(self):
        self.assertTrue(self.service.authorized("Bearer test-token"))
        self.assertFalse(self.service.authorized("Bearer wrong"))

    def test_http_endpoint_requires_bearer_and_accepts_payload(self):
        handler = type("BoundNotifierHandler", (NotifierHandler,), {"service": self.service})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = json.dumps({
                "hook_event_name": "Notification",
                "notification_type": "permission_prompt",
                "session_id": "http-session",
            }).encode()
            with self.assertRaises(HTTPError) as raised:
                urlopen(Request(f"http://127.0.0.1:{server.server_port}/events", data=body), timeout=2)
            self.assertEqual(raised.exception.code, 401)
            request = Request(
                f"http://127.0.0.1:{server.server_port}/events",
                data=body,
                headers={"Authorization": "Bearer test-token", "Content-Type": "application/json"},
            )
            with urlopen(request, timeout=2) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read())["notified"], True)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
