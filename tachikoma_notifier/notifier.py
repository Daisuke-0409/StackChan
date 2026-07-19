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
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping, Optional

from tachikoma_notifier.adapters.claude_code import ClaudeCodeAdapter, validate_hook_payload
from tachikoma_notifier.events import TachikomaEvent
from tachikoma_notifier.routing import EventRouter, SpeechFormatter

MAX_BODY_BYTES = 64 * 1024
DEFAULT_DEBOUNCE_SECONDS = 5.0


@dataclass(frozen=True)
class NormalizedEvent:
    state: str
    phrase: str
    session_id: str
    event: Optional[TachikomaEvent] = None

    @classmethod
    def from_event(cls, event: TachikomaEvent, phrase: str) -> "NormalizedEvent":
        state = {
            "task_completed": "completed",
            "task_failed": "error",
            "tool_failed": "error",
        }.get(event.event_type.value, event.event_type.value)
        return cls(state, phrase, event.session_id or "unknown", event)


class EventNormalizer:
    """Backward-compatible facade around ClaudeCodeAdapter and SpeechFormatter."""

    def __init__(self) -> None:
        self.adapter = ClaudeCodeAdapter()
        self.formatter = SpeechFormatter()

    def normalize(self, payload: Mapping[str, Any]) -> Optional[NormalizedEvent]:
        event = self.adapter.normalize(payload)
        if event is None:
            return None
        return NormalizedEvent.from_event(event, self.formatter.format(event))


class SpeechSink:
    def speak(self, phrase: str) -> bool:
        raise NotImplementedError


class LogSpeechSink(SpeechSink):
    """Deterministic fallback useful on machines without a Japanese voice."""

    def __init__(self, output: Optional[Callable[[str], None]] = None) -> None:
        self.output = output or print

    def speak(self, phrase: str) -> bool:
        self.output(f"[TACHIKOMA TTS] {phrase}")
        return True


class WindowsSpeechSink(SpeechSink):
    """Uses the Windows PowerShell/System.Speech engine without cloud calls."""

    def __init__(self, fallback: Optional[SpeechSink] = None) -> None:
        self.fallback = fallback or LogSpeechSink()

    def speak(self, phrase: str) -> bool:
        if os.name != "nt":
            self.fallback.speak(phrase)
            return False
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
                return False
            return True
        except (OSError, subprocess.SubprocessError):
            self.fallback.speak(phrase)
            return False


class NotifierService:
    def __init__(
        self,
        token: str,
        sink: SpeechSink,
        debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS,
        logger: Optional[Callable[[str], None]] = None,
    ) -> None:
        if not token:
            raise ValueError("TACHIKOMA_NOTIFY_TOKEN is required")
        self.token = token
        self.sink = sink
        self.debounce_seconds = debounce_seconds
        self.normalizer = EventNormalizer()
        self.adapter = ClaudeCodeAdapter()
        self.router = EventRouter(
            adapters=(self.adapter,),
            sink=sink,
            debounce_seconds=debounce_seconds,
            logger=logger,
        )

    def authorized(self, header: str) -> bool:
        expected = f"Bearer {self.token}".encode("utf-8")
        return hmac.compare_digest(header.encode("utf-8"), expected)

    def validate_payload(self, payload: Mapping[str, Any]) -> Optional[str]:
        return validate_hook_payload(payload)

    def handle(self, payload: Mapping[str, Any], now: Optional[float] = None) -> Optional[NormalizedEvent]:
        event = self.router.adapt(payload)
        if event is None:
            return None
        routed = self.router.route(event, now=now)
        if routed is None:
            return None
        return NormalizedEvent.from_event(routed, self.router.formatter.format(routed))


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
        validation_error = self.service.validate_payload(payload)
        if validation_error:
            _json_response(self, 400, {"error": "invalid_payload"})
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
