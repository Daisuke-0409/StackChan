"""Convert OpenAI Codex CLI hook payloads into the common TachikomaEvent format.

Codex's hook system (shipped v0.114+) sends one JSON object on stdin per
hook invocation, using `hook_event_name`, `session_id`, `cwd`, and
`transcript_path` fields that closely parallel Claude Code's own hook
envelope. Confirmed hook event names include SessionStart, SubagentStart,
PreToolUse, PermissionRequest, PostToolUse, PreCompact, PostCompact,
UserPromptSubmit, SubagentStop, and Stop.

This adapter only maps the two events with a confidently-confirmed,
unambiguous mapping to an existing TachikomaEvent type:

  - PermissionRequest -> approval_needed (a tool call is awaiting a
    permission decision, the same situation Claude Code's
    Notification/permission_prompt represents)
  - Stop -> task_completed (a turn has ended, the same situation Claude
    Code's Stop represents)

PreToolUse/PostToolUse tool-failure detection is deliberately not
implemented yet: public documentation does not confirm a stable
success/failure field shape for `tool_response`, and guessing at an
unconfirmed field risks silently misclassifying events. Extend this
adapter once that shape is verified against a live Codex instance.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from tachikoma_notifier.events import (
    EventSeverity,
    EventSource,
    EventType,
    SCHEMA_VERSION,
    TachikomaEvent,
    make_dedupe_key,
    make_event_id,
    normalize_message,
    utc_now_iso,
)

MAX_SESSION_ID_LENGTH = 128


def validate_codex_hook_payload(payload: Mapping[str, Any]) -> Optional[str]:
    """Validate only bounded envelope fields; never inspect transcript content."""
    event_name = payload.get("hook_event_name")
    if not isinstance(event_name, str) or not event_name:
        return "hook_event_name is required"
    session_id = payload.get("session_id")
    if session_id is not None and (not isinstance(session_id, str) or len(session_id) > MAX_SESSION_ID_LENGTH):
        return "session_id must be a short string"
    return None


class CodexAdapter:
    source = EventSource.CODEX

    def can_handle(self, payload: Mapping[str, Any]) -> bool:
        return isinstance(payload.get("hook_event_name"), str)

    def normalize(self, payload: Mapping[str, Any]) -> Optional[TachikomaEvent]:
        error = validate_codex_hook_payload(payload)
        if error:
            return None
        raw_event_name = str(payload["hook_event_name"])
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            session_id = None

        if raw_event_name == "PermissionRequest":
            return self._event(
                event_type=EventType.APPROVAL_NEEDED,
                severity=EventSeverity.WARNING,
                title="承認待ち",
                message="承認待ち",
                requires_action=True,
                session_id=session_id,
                raw_event_name=raw_event_name,
            )

        if raw_event_name == "Stop":
            return self._event(
                event_type=EventType.TASK_COMPLETED,
                severity=EventSeverity.INFO,
                title="タスク完了",
                message="タスク完了",
                requires_action=False,
                session_id=session_id,
                raw_event_name=raw_event_name,
            )

        return None

    def _event(
        self,
        *,
        event_type: EventType,
        severity: EventSeverity,
        title: str,
        message: str,
        requires_action: bool,
        session_id: Optional[str],
        raw_event_name: str,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> TachikomaEvent:
        safe_message = normalize_message(message)
        return TachikomaEvent(
            schema_version=SCHEMA_VERSION,
            event_id=make_event_id(),
            source=self.source,
            event_type=event_type,
            severity=severity,
            occurred_at=utc_now_iso(),
            session_id=session_id,
            title=title,
            message=safe_message,
            raw_event_name=raw_event_name,
            requires_action=requires_action,
            dedupe_key=make_dedupe_key(self.source, session_id, event_type, safe_message),
            metadata=dict(metadata or {}),
        )


__all__ = ["CodexAdapter", "validate_codex_hook_payload"]
