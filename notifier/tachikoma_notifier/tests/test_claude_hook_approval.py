"""claude_hook_approval: the hook client's contract with Claude Code.

Two things may ever reach stdout: an allow decision or a deny decision.
Everything else -- "ask", a dead daemon, a timeout, garbage JSON in either
direction -- must be silence with exit code 0, because silence is what
makes Claude Code fall through to its normal permission prompt.
"""
import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from tachikoma_notifier.claude_hook_approval import DAEMON_URL, main


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self):
        return self._body.encode("utf-8")


def daemon_answering(decision, captured=None):
    def opener(request, timeout=None):
        if captured is not None:
            captured["url"] = request.full_url
            captured["data"] = request.data
            captured["timeout"] = timeout
        return FakeResponse(json.dumps({"decision": decision, "reason": "test"}))

    return opener


def run_main(stdin_text, opener):
    stdout = io.StringIO()
    with patch("urllib.request.urlopen", side_effect=opener):
        code = main(stdin=io.StringIO(stdin_text), stdout=stdout)
    return code, stdout.getvalue()


HOOK_JSON = json.dumps(
    {
        "hook_event_name": "PreToolUse",
        "session_id": "s1",
        "tool_name": "Read",
        "tool_input": {"file_path": "config.json"},
    }
)


class HookClientDecisionTests(unittest.TestCase):
    def test_allow_prints_the_permission_decision(self):
        code, out = run_main(HOOK_JSON, daemon_answering("allow"))
        self.assertEqual(code, 0)
        output = json.loads(out)
        self.assertEqual(
            output["hookSpecificOutput"]["permissionDecision"], "allow"
        )
        self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertEqual(
            output["hookSpecificOutput"]["permissionDecisionReason"],
            "voice approval by Daisuke",
        )

    def test_deny_prints_the_permission_decision(self):
        code, out = run_main(HOOK_JSON, daemon_answering("deny"))
        self.assertEqual(code, 0)
        output = json.loads(out)
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_ask_is_silent_exit_zero(self):
        code, out = run_main(HOOK_JSON, daemon_answering("ask"))
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    def test_forwards_the_hook_body_to_the_daemon(self):
        captured = {}
        run_main(HOOK_JSON, daemon_answering("ask", captured))
        self.assertEqual(captured["url"], DAEMON_URL)
        self.assertEqual(json.loads(captured["data"].decode("utf-8")), json.loads(HOOK_JSON))


class HookClientFailureTests(unittest.TestCase):
    """Every failure is silence with exit 0 -- never a decision, never a crash."""

    def assert_silent(self, code, out):
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    def test_daemon_down_connection_refused(self):
        def opener(request, timeout=None):
            raise URLError("connection refused")

        self.assert_silent(*run_main(HOOK_JSON, opener))

    def test_daemon_timeout(self):
        def opener(request, timeout=None):
            raise TimeoutError("timed out")

        self.assert_silent(*run_main(HOOK_JSON, opener))

    def test_daemon_http_error(self):
        def opener(request, timeout=None):
            raise HTTPError(DAEMON_URL, 400, "bad request", None, None)

        self.assert_silent(*run_main(HOOK_JSON, opener))

    def test_daemon_returns_garbage_body(self):
        def opener(request, timeout=None):
            return FakeResponse("this is not json")

        self.assert_silent(*run_main(HOOK_JSON, opener))

    def test_daemon_returns_non_object_body(self):
        def opener(request, timeout=None):
            return FakeResponse("[1, 2, 3]")

        self.assert_silent(*run_main(HOOK_JSON, opener))

    def test_daemon_returns_unknown_decision(self):
        # A daemon that ever invents "approve_forever" gets ignored.
        self.assert_silent(*run_main(HOOK_JSON, daemon_answering("approve_forever")))

    def test_unparseable_stdin_never_contacts_the_daemon(self):
        calls = []

        def opener(request, timeout=None):
            calls.append(request)
            return FakeResponse("{}")

        self.assert_silent(*run_main("{not json", opener))
        self.assertEqual(calls, [])

    def test_non_object_stdin_is_silent(self):
        self.assert_silent(*run_main("[]", daemon_answering("allow")))


if __name__ == "__main__":
    unittest.main()
