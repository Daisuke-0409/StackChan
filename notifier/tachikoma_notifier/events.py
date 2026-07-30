"""Versioned, provider-neutral event types for Tachikoma notifications."""
from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Mapping, Optional

SCHEMA_VERSION = "1.0"
MAX_TITLE_LENGTH = 128
MAX_MESSAGE_LENGTH = 2000
MAX_METADATA_VALUE_LENGTH = 256
_SENSITIVE_KEY = re.compile(r"(?:token|api[_-]?key|authorization|password|secret|transcript|credential)", re.I)
_SENSITIVE_VALUE = re.compile(r"(?:Bearer\s+\S+|sk-[A-Za-z0-9_-]{8,})", re.I)


class EventSource(str, Enum):
    CLAUDE_CODE = "claude_code"
    CODEX = "codex"
    GEMINI_CLI = "gemini_cli"
    GITHUB_ACTIONS = "github_actions"
    BUILD_SYSTEM = "build_system"
    STACKCHAN = "stackchan"
    SYSTEM = "system"


class EventType(str, Enum):
    APPROVAL_NEEDED = "approval_needed"
    TASK_STARTED = "task_started"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    TOOL_FAILED = "tool_failed"
    BLOCKED = "blocked"
    WAITING = "waiting"
    ERROR = "error"
    INFO = "info"


class EventSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


def utc_now_iso() -> str:
    """Return a compact UTC ISO-8601 timestamp with a Z suffix."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def make_event_id() -> str:
    return str(uuid.uuid4())


def normalize_message(message: str) -> str:
    return " ".join(message.split())[:MAX_MESSAGE_LENGTH]


def make_dedupe_key(
    source: EventSource,
    session_id: Optional[str],
    event_type: EventType,
    message: str,
) -> str:
    """Build a stable opaque key; event_id is intentionally not used."""
    parts = (
        source.value,
        session_id or "unknown",
        event_type.value,
        normalize_message(message).casefold(),
    )
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"v1:{digest}"


def _sanitize_metadata(value: Any, key: str = "") -> Any:
    if key and _SENSITIVE_KEY.search(key):
        return None
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for child_key, child_value in value.items():
            safe_key = str(child_key)[:64]
            safe_value = _sanitize_metadata(child_value, safe_key)
            if safe_value is not None:
                result[safe_key] = safe_value
        return result
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str):
            if _SENSITIVE_VALUE.search(value):
                return None
            return value[:MAX_METADATA_VALUE_LENGTH]
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    if isinstance(value, (list, tuple)):
        return [_sanitize_metadata(item) for item in value[:16]]
    return None


def sanitize_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    safe = _sanitize_metadata(metadata)
    return safe if isinstance(safe, dict) else {}


@dataclass(frozen=True)
class TachikomaEvent:
    """Stable JSON-compatible event envelope shared by all providers."""

    schema_version: str
    event_id: str
    source: EventSource
    event_type: EventType
    severity: EventSeverity
    occurred_at: str
    title: str
    message: str
    requires_action: bool
    dedupe_key: str
    session_id: Optional[str] = None
    project_id: Optional[str] = None
    device_id: Optional[str] = None
    raw_event_name: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported schema_version")
        if not self.event_id:
            raise ValueError("event_id is required")
        try:
            uuid.UUID(self.event_id)
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError("event_id must be a UUID") from exc
        if not isinstance(self.source, EventSource):
            raise ValueError("source must be EventSource")
        if not isinstance(self.event_type, EventType):
            raise ValueError("event_type must be EventType")
        if not isinstance(self.severity, EventSeverity):
            raise ValueError("severity must be EventSeverity")
        try:
            occurred = datetime.fromisoformat(self.occurred_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("occurred_at must be UTC ISO 8601") from exc
        if not self.occurred_at.endswith("Z") or occurred.tzinfo is None or occurred.utcoffset() != timedelta(0):
            raise ValueError("occurred_at must be UTC ISO 8601")
        if len(self.title) > MAX_TITLE_LENGTH:
            raise ValueError("title is too long")
        if len(self.message) > MAX_MESSAGE_LENGTH:
            raise ValueError("message is too long")
        if not self.dedupe_key:
            raise ValueError("dedupe_key is required")
        object.__setattr__(self, "metadata", sanitize_metadata(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "source": self.source.value,
            "event_type": self.event_type.value,
            "severity": self.severity.value,
            "occurred_at": self.occurred_at,
            "session_id": self.session_id,
            "project_id": self.project_id,
            "device_id": self.device_id,
            "title": self.title,
            "message": self.message,
            "raw_event_name": self.raw_event_name,
            "requires_action": self.requires_action,
            "dedupe_key": self.dedupe_key,
            "metadata": self.metadata,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
