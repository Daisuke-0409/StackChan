"""Convert Gemini CLI hook payloads into the common TachikomaEvent format.

Gemini CLI's hook system sends one JSON object on stdin per hook
invocation. The universal envelope (`session_id`, `transcript_path`, `cwd`,
`hook_event_name`, `timestamp`) and the `Notification` hook event's fields
(`notification_type`, `message`, `details`) closely parallel Claude Code's
own hook shape.

This adapter only maps the one event with a confidently-confirmed,
unambiguous mapping to an existing TachikomaEvent type:

  - hook_event_name == "Notification" with notification_type == "ToolPermission"
    -> approval_needed (a tool call is awaiting a permission decision, the
    same situation Claude Code's Notification/permission_prompt represents)

Public documentation references a separate "session complete" notification
concept but does not confirm its exact `notification_type` value (or
whether it arrives via a hook at all, as opposed to the separate
experimental terminal-notification feature). Rather than guess at an
unconfirmed value, task_completed / task_failed mappings are intentionally
left unimplemented here until verified against a live Gemini CLI instance.
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


def validate_gemini_cli_hook_payload(payload: Mapping[str, Any]) -> Optional[str]:
    """Validate only bounded envelope fields; never inspect transcript content."""
    event_name = payload.get("hook_event_name")
    if not isinstance(event_name, str) or not event_name:
        return "hook_event_name is required"
    session_id = payload.get("session_id")
    if session_id is not None and (not isinstance(session_id, str) or len(session_id) > MAX_SESSION_ID_LENGTH):
        return "session_id must be a short string"
    if event_name == "Notification":
        notification_type = payload.get("notification_type")
        if notification_type is not None and not isinstance(notification_type, str):
            return "notification_type must be a string"
    return None


class GeminiCliAdapter:
    source = EventSource.GEMINI_CLI

    def can_handle(self, payload: Mapping[str, Any]) -> bool:
        return isinstance(payload.get("hook_event_name"), str)

    def normalize(self, payload: Mapping[str, Any]) -> Optional[TachikomaEvent]:
        error = validate_gemini_cli_hook_payload(payload)
        if error:
            return None
        raw_event_name = str(payload["hook_event_name"])
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            session_id = None

        if raw_event_name == "Notification":
            notification_type = payload.get("notification_type")
            if notification_type == "ToolPermission":
                return self._event(
                    event_type=EventType.APPROVAL_NEEDED,
                    severity=EventSeverity.WARNING,
                    title="承認待ち",
                    message="承認待ち",
                    requires_action=True,
                    session_id=session_id,
                    raw_event_name=raw_event_name,
                    metadata={"notification_type": "ToolPermission"},
                )
            return None

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


__all__ = ["GeminiCliAdapter", "validate_gemini_cli_hook_payload"]
