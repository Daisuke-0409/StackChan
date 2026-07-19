"""One-way Claude Code status notifier.

Receives Claude Code HTTP hook payloads, normalizes safe status events, and
speaks a short Japanese notification on Windows. It never returns permission
decisions and never forwards commands back to Claude Code.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping, Optional

MAX_BODY_BYTES = 64 * 1024
DEFAULT_DEBOUNCE_SECONDS = 5.0


@dataclass(frozen=True)
class NormalizedEvent:
    state: str
    phrase: str
    session_id: str


class EventNormalizer:
    """Maps documented Claude Code hook events to a small stable vocabulary."""

    def normalize(self, payload: Mapping[str, Any]) -> Optional[NormalizedEvent]:
        event = payload.get("hook_event_name")
        session_id = str(payload.get("session_id") or "unknown")

        if event == "Notification":
            notification_type = payload.get("notification_type")
            if notification_type == "permission_prompt":
                return NormalizedEvent("approval_needed", "Claude Codeが承認待ちだよ", session_id)
            return None

        if event == "Stop":
            # Stop may fire while background work or a scheduled wakeup remains.
            if payload.get("background_tasks") or payload.get("session_crons"):
                return None
            return NormalizedEvent("completed", "タスクが終わったよ", session_id)

        if event in ("PostToolUseFailure", "StopFailure"):
            return NormalizedEvent("error", "エラーが出たみたい", session_id)

        return None


class SpeechSink:
    def speak(self, phrase: str) -> None:
        raise NotImplementedError


class LogSpeechSink(SpeechSink):
    """Deterministic fallback useful on machines without a Japanese voice."""

    def __init__(self, output: Optional[Callable[[str], None]] = None) -> None:
        self.output = output or print

    def speak(self, phrase: str) -> None:
        self.output(f"[TACHIKOMA TTS] {phrase}")


class WindowsSpeechSink(SpeechSink):
    """Uses the Windows PowerShell/System.Speech engine without cloud calls."""

    def __init__(self, fallback: Optional[SpeechSink] = None) -> None:
        self.fallback = fallback or LogSpeechSink()

    def speak(self, phrase: str) -> None:
        if os.name != "nt":
            self.fallback.speak(phrase)
            return
        script = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
  $voice = $env:TACHIKOMA_TTS_VOICE
  if ($voice) { $s.SelectVoice($voice) }
  else { $s.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::NotSet,
                               [System.Speech.Synthesis.VoiceAge]::NotSet, 0, 'ja-JP') }
} catch { }
$s.Speak($env:TACHIKOMA_TTS_TEXT)
$s.Dispose()
"""
        env = os.environ.copy()
        env["TACHIKOMA_TTS_TEXT"] = phrase
        try:
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            if completed.returncode != 0:
                self.fallback.speak(phrase)
        except (OSError, subprocess.SubprocessError):
            self.fallback.speak(phrase)


class NotifierService:
    def __init__(self, token: str, sink: SpeechSink, debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS) -> None:
        if not token:
            raise ValueError("TACHIKOMA_NOTIFY_TOKEN is required")
        self.token = token
        self.sink = sink
        self.debounce_seconds = debounce_seconds
        self.normalizer = EventNormalizer()
        self._last_seen: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    def authorized(self, header: str) -> bool:
        expected = f"Bearer {self.token}".encode("utf-8")
        return hmac.compare_digest(header.encode("utf-8"), expected)

    def handle(self, payload: Mapping[str, Any], now: Optional[float] = None) -> Optional[NormalizedEvent]:
        event = self.normalizer.normalize(payload)
        if event is None:
            return None
        current = time.monotonic() if now is None else now
        key = (event.session_id, event.state)
        with self._lock:
            previous = self._last_seen.get(key)
            if previous is not None and current - previous < self.debounce_seconds:
                return None
            self._last_seen[key] = current
        self.sink.speak(event.phrase)
        return event


def _json_response(handler: BaseHTTPRequestHandler, status: int, body: Mapping[str, Any]) -> None:
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


class NotifierHandler(BaseHTTPRequestHandler):
    service: NotifierService

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            _json_response(self, 200, {"ok": True, "service": "tachikoma-notifier"})
            return
        _json_response(self, 404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/events":
            _json_response(self, 404, {"error": "not_found"})
            return
        if not self.service.authorized(self.headers.get("Authorization", "")):
            _json_response(self, 401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY_BYTES:
                raise ValueError("invalid body length")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            _json_response(self, 400, {"error": "invalid_json"})
            return
        event = self.service.handle(payload)
        _json_response(self, 200, {"ok": True, "notified": event is not None})

    def log_message(self, fmt: str, *args: Any) -> None:
        # Do not log tokens, full prompts, or assistant messages.
        print(f"[notifier] {self.command} {self.path} {args[1] if len(args) > 1 else ''}")


def run(host: str, port: int, service: NotifierService) -> None:
    handler = type("BoundNotifierHandler", (NotifierHandler,), {"service": service})
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Tachikoma notifier listening on http://{host}:{port}/events")
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Claude Code one-way Tachikoma notifier")
    parser.add_argument("--host", default=os.getenv("TACHIKOMA_NOTIFY_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("TACHIKOMA_NOTIFY_PORT", "8787")))
    parser.add_argument("--log-only", action="store_true", help="print phrases instead of invoking Windows TTS")
    args = parser.parse_args()
    token = os.getenv("TACHIKOMA_NOTIFY_TOKEN", "")
    sink: SpeechSink = LogSpeechSink() if args.log_only else WindowsSpeechSink()
    run(args.host, args.port, NotifierService(token, sink))


if __name__ == "__main__":
    main()
