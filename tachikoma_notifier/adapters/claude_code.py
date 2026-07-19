"""Convert Claude Code hook payloads into the common TachikomaEvent format."""
from __future__ import annotations

from typing import Any, Mapping, Optional

from tachikoma_notifier.events import (
    EventSeverity,
    EventSource,
    EventType,
    MAX_MESSAGE_LENGTH,
    SCHEMA_VERSION,
    TachikomaEvent,
    make_dedupe_key,
    make_event_id,
    normalize_message,
    utc_now_iso,
)

MAX_SESSION_ID_LENGTH = 128
ERROR_NOTIFICATION_TYPES = frozenset({"error", "error_notification", "notification_error"})


def validate_hook_payload(payload: Mapping[str, Any]) -> Optional[str]:
    """Validate only bounded envelope fields; never inspect transcript content."""
    event_name = payload.get("hook_event_name")
    if not isinstance(event_name, str) or not event_name:
        return "hook_event_name is required"
    session_id = payload.get("session_id")
    if session_id is not None and (not isinstance(session_id, str) or len(session_id) > MAX_SESSION_ID_LENGTH):
        return "session_id must be a short string"
    for key in ("message", "last_assistant_message"):
        value = payload.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > MAX_MESSAGE_LENGTH):
            return f"{key} is invalid"
    if event_name == "Notification":
        notification_type = payload.get("notification_type")
        if notification_type is not None and not isinstance(notification_type, str):
            return "notification_type must be a string"
    return None


class ClaudeCodeAdapter:
    source = EventSource.CLAUDE_CODE

    def can_handle(self, payload: Mapping[str, Any]) -> bool:
        return isinstance(payload.get("hook_event_name"), str)

    def normalize(self, payload: Mapping[str, Any]) -> Optional[TachikomaEvent]:
        error = validate_hook_payload(payload)
        if error:
            return None
        raw_event_name = str(payload["hook_event_name"])
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            session_id = None

        if raw_event_name == "Notification":
            notification_type = payload.get("notification_type")
            if notification_type == "permission_prompt":
                return self._event(
                    event_type=EventType.APPROVAL_NEEDED,
                    severity=EventSeverity.WARNING,
                    title="承認待ち",
                    message="承認待ち",
                    requires_action=True,
                    session_id=session_id,
                    raw_event_name=raw_event_name,
                    metadata={"notification_type": "permission_prompt"},
                )
            if notification_type in ERROR_NOTIFICATION_TYPES:
                return self._event(
                    event_type=EventType.ERROR,
                    severity=EventSeverity.ERROR,
                    title="エラー",
                    message="エラー",
                    requires_action=False,
                    session_id=session_id,
                    raw_event_name=raw_event_name,
                    metadata={"notification_type": notification_type},
                )
            return None

        if raw_event_name == "Stop":
            # Keep the Step 1 behavior: active background work is not completion.
            if payload.get("background_tasks") or payload.get("session_crons"):
                return None
            return self._event(
                event_type=EventType.TASK_COMPLETED,
                severity=EventSeverity.INFO,
                title="タスク完了",
                message="タスク完了",
                requires_action=False,
                session_id=session_id,
                raw_event_name=raw_event_name,
            )

        if raw_event_name == "PostToolUseFailure":
            return self._event(
                event_type=EventType.TOOL_FAILED,
                severity=EventSeverity.ERROR,
                title="ツール失敗",
                message="ツール失敗",
                requires_action=False,
                session_id=session_id,
                raw_event_name=raw_event_name,
            )

        if raw_event_name == "StopFailure":
            return self._event(
                event_type=EventType.TASK_FAILED,
                severity=EventSeverity.ERROR,
                title="タスク失敗",
                message="タスク失敗",
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
