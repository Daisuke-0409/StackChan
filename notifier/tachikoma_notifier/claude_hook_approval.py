"""R4: the Claude Code side of the approval relay -- a PreToolUse hook client.

Claude Code runs this once per tool call it wants permission for, with the
hook JSON on stdin, and reads the permission decision from stdout. The
daemon (approval_daemon.py) does everything slow -- speaking through
Tachikoma, listening on the desk microphone, deciding; this client only
ferries JSON across localhost and maps the daemon's answer onto the hook
protocol:

    allow            -> permissionDecision "allow"  (spoken yes, low-risk tool)
    deny             -> permissionDecision "deny"   (spoken no)
    ask, or ANY failure -> print nothing, exit 0

The last line is the safety heart of the whole relay. Printing nothing
makes Claude Code fall through to its normal permission prompt, so a dead
daemon, a refused connection, a timeout, or malformed JSON can never
approve anything -- the worst outcome is being asked on the PC as if this
hook did not exist. For the same reason the process must never die loudly:
a traceback on stderr would make Claude Code report the hook itself as
broken on every tool call.

Stdlib only and self-contained on purpose: .claude/settings.json invokes it
as a plain `python claude_hook_approval.py` with no PYTHONPATH, no imports
from the rest of this package. See README.md for the registration snippet.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Any, Optional, TextIO

DAEMON_URL = "http://127.0.0.1:8378/approval"
# Longer than the daemon's whole announce-and-listen budget (~25s worst
# case), shorter than the hook timeout registered in settings.json.
TIMEOUT_SECONDS = 30.0

_DECISION_REASONS = {
    "allow": "voice approval by Daisuke",
    "deny": "voice denial by Daisuke",
}


def hook_output(decision: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": _DECISION_REASONS[decision],
        }
    }


def _fetch_decision(raw_payload: bytes) -> Optional[str]:
    request = urllib.request.Request(
        DAEMON_URL,
        data=raw_payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        body = json.loads(response.read().decode("utf-8"))
    if not isinstance(body, dict):
        return None
    decision = body.get("decision")
    return decision if isinstance(decision, str) else None


def main(stdin: Optional[TextIO] = None, stdout: Optional[TextIO] = None) -> int:
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    try:
        raw = stdin.read()
        # Parse before forwarding: if Claude Code ever hands us non-JSON,
        # silence here is the correct answer, not a daemon round trip.
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return 0
        decision = _fetch_decision(raw.encode("utf-8"))
        if decision in ("allow", "deny"):
            stdout.write(json.dumps(hook_output(decision), ensure_ascii=False) + "\n")
        return 0
    except Exception:
        # Daemon down, timeout, bad JSON, anything unforeseen: silence IS
        # the fail-safe answer -- Claude Code asks the user as usual.
        return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["DAEMON_URL", "TIMEOUT_SECONDS", "hook_output", "main"]
