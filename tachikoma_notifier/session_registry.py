"""Step 6: multi-PC / multi-session state tracking.

Tracks per-session state observed from TachikomaEvents across multiple
devices, projects, and workspaces, so a future announcer can distinguish or
prioritize between several simultaneous sessions (e.g. two PCs both running
Claude Code, or a background CI job versus a customer-facing session).

This module only tracks and reports state derived from ordinary
TachikomaEvents. It does not implement, call, or depend on any approval,
rejection, or voice-confirmation logic -- see approval_store.py,
permission_relay.py, and voice_approval_gate.py for that, which this module
never imports.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import List, Optional, Tuple

from tachikoma_notifier.events import EventSource, TachikomaEvent

SessionKey = Tuple[str, str, str, str, str]


class SessionRegistryError(ValueError):
    """Base class for safe, non-sensitive session registry errors."""


class SessionRegistryCapacityError(SessionRegistryError):
    pass


def _key_part(value: Optional[str]) -> str:
    return value if value else ""


def make_session_key(
    source: EventSource,
    device_id: Optional[str],
    session_id: Optional[str],
    project_id: Optional[str],
    workspace: Optional[str],
) -> SessionKey:
    return (
        source.value,
        _key_part(device_id),
        _key_part(session_id),
        _key_part(project_id),
        _key_part(workspace),
    )


@dataclass(frozen=True)
class SessionInfo:
    source: EventSource
    device_id: Optional[str]
    session_id: Optional[str]
    project_id: Optional[str]
    workspace: Optional[str]
    status: str
    priority: int
    last_updated_at: str
    last_event_id: str

    @property
    def key(self) -> SessionKey:
        return make_session_key(self.source, self.device_id, self.session_id, self.project_id, self.workspace)


class SessionRegistry:
    """Bounded, thread-safe, process-local session state tracker."""

    def __init__(self, *, max_sessions: int = 500) -> None:
        if not isinstance(max_sessions, int) or isinstance(max_sessions, bool) or max_sessions <= 0:
            raise ValueError("max_sessions must be a positive integer")
        self.max_sessions = max_sessions
        self._sessions: dict[SessionKey, SessionInfo] = {}
        self._lock = threading.Lock()

    def upsert_from_event(
        self,
        event: TachikomaEvent,
        *,
        workspace: Optional[str] = None,
        priority: Optional[int] = None,
    ) -> SessionInfo:
        """Record the latest known state for one (source, device, session,
        project, workspace) identity. An event older than the currently
        recorded state (by occurred_at) is ignored and the existing state is
        returned unchanged, so an out-of-order delivery cannot regress a
        session's tracked status."""
        key = make_session_key(event.source, event.device_id, event.session_id, event.project_id, workspace)
        with self._lock:
            existing = self._sessions.get(key)
            if existing is not None and event.occurred_at < existing.last_updated_at:
                return existing
            resolved_priority = priority if priority is not None else (existing.priority if existing is not None else 0)
            info = SessionInfo(
                source=event.source,
                device_id=event.device_id,
                session_id=event.session_id,
                project_id=event.project_id,
                workspace=workspace,
                status=event.event_type.value,
                priority=resolved_priority,
                last_updated_at=event.occurred_at,
                last_event_id=event.event_id,
            )
            if existing is None and len(self._sessions) >= self.max_sessions:
                raise SessionRegistryCapacityError("session registry is full")
            self._sessions[key] = info
            return info

    def get(self, key: SessionKey) -> Optional[SessionInfo]:
        with self._lock:
            return self._sessions.get(key)

    def list_all(self) -> List[SessionInfo]:
        with self._lock:
            return list(self._sessions.values())

    def list_by_priority(self) -> List[SessionInfo]:
        """Highest priority first; ties broken by most-recently-updated first."""
        with self._lock:
            sessions = list(self._sessions.values())
        sessions.sort(key=lambda info: info.last_updated_at, reverse=True)
        sessions.sort(key=lambda info: info.priority, reverse=True)
        return sessions

    def remove_stale(self, cutoff_iso: str) -> int:
        """Remove sessions whose last_updated_at is older than cutoff_iso
        (a UTC ISO-8601 timestamp in the same format as TachikomaEvent.occurred_at)."""
        with self._lock:
            removed = 0
            for key, info in list(self._sessions.items()):
                if info.last_updated_at < cutoff_iso:
                    del self._sessions[key]
                    removed += 1
            return removed

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)


__all__ = [
    "SessionInfo",
    "SessionKey",
    "SessionRegistry",
    "SessionRegistryCapacityError",
    "SessionRegistryError",
    "make_session_key",
]
