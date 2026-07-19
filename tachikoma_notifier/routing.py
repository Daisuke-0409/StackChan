"""Routing, deduplication, and speech formatting for common events."""
from __future__ import annotations

import hashlib
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Optional

from tachikoma_notifier.adapters.base import EventAdapter
from tachikoma_notifier.events import EventSource, EventType, TachikomaEvent


class SpeechFormatter:
    """Convert provider-neutral event types into short spoken phrases."""

    _phrases = {
        EventType.APPROVAL_NEEDED: "承認待ちだよ",
        EventType.TASK_COMPLETED: "タスクが終わったよ",
        EventType.TASK_FAILED: "タスクが失敗したみたい",
        EventType.TOOL_FAILED: "ツールの実行でエラーが出たみたい",
        EventType.ERROR: "エラーが出たみたい",
    }

    def format(self, event: TachikomaEvent) -> str:
        if event.event_type == EventType.APPROVAL_NEEDED and event.source == EventSource.CLAUDE_CODE:
            return "Claude Codeが承認待ちだよ"
        return self._phrases.get(event.event_type, event.message)


class Deduplicator:
    """Suppress equal common-event keys for a bounded time window."""

    def __init__(self, seconds: float = 5.0) -> None:
        self.seconds = seconds
        self._last_seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def is_duplicate(self, event: TachikomaEvent, now: Optional[float] = None) -> bool:
        current = time.monotonic() if now is None else now
        with self._lock:
            previous = self._last_seen.get(event.dedupe_key)
            if previous is not None and current - previous < self.seconds:
                return True
            self._last_seen[event.dedupe_key] = current
            return False


class EventRouter:
    """Select an adapter, deduplicate a common event, then dispatch its speech."""

    def __init__(
        self,
        adapters: Iterable[EventAdapter],
        sink: Any,
        debounce_seconds: float = 5.0,
        formatter: Optional[SpeechFormatter] = None,
        logger: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.adapters = tuple(adapters)
        self.sink = sink
        self.formatter = formatter or SpeechFormatter()
        self.deduplicator = Deduplicator(debounce_seconds)
        self.logger = logger or print

    def adapt(self, payload: Mapping[str, Any]) -> Optional[TachikomaEvent]:
        for adapter in self.adapters:
            if adapter.can_handle(payload):
                return adapter.normalize(payload)
        return None

    def route(self, event: TachikomaEvent, now: Optional[float] = None) -> Optional[TachikomaEvent]:
        session_ref = _session_ref(event.session_id)
        if self.deduplicator.is_duplicate(event, now=now):
            self._log(event, session_ref, "deduplicated=true")
            return None

        phrase = self.formatter.format(event)
        result = self.sink.speak(phrase)
        tts_status = "fallback" if result is False else "success"
        self._log(event, session_ref, f"deduplicated=false tts={tts_status}")
        return event

    def _log(self, event: TachikomaEvent, session_ref: str, status: str) -> None:
        self.logger(
            "[event] "
            f"event_id={event.event_id} source={event.source.value} "
            f"event_type={event.event_type.value} severity={event.severity.value} "
            f"session={session_ref} {status}"
        )


def _session_ref(session_id: Optional[str]) -> str:
    value = session_id or "unknown"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
