"""Provider-neutral approval models for the Step 2 permission relay.

This module deliberately contains no Hook transport, UI, voice input, or
Claude-specific response logic.  It is an internal, fail-closed data-model
layer that can be used by a future PermissionRelay.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Mapping, Optional

SCHEMA_VERSION = "1.0"
MAX_SAFE_SUMMARY_LENGTH = 240
MAX_TOOL_NAME_LENGTH = 128
MAX_ACTION_TYPE_LENGTH = 64
MAX_IDENTIFIER_LENGTH = 256
MAX_METADATA_ENTRIES = 32
MAX_METADATA_DEPTH = 4
MAX_METADATA_VALUE_LENGTH = 256

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")
_SENSITIVE_KEY = re.compile(
    r"(?:token|secret|password|passwd|authorization|cookie|api[_-]?key|apikey|"
    r"access[_-]?token|refresh[_-]?token|private[_-]?key|credential|transcript|"
    r"tool[_-]?input|command|file[_-]?content|raw|hook|payload|prompt|message|input)",
    re.IGNORECASE,
)
_SENSITIVE_VALUE = re.compile(
    r"(?:Bearer\s+\S+|sk-[A-Za-z0-9_-]{8,})", re.IGNORECASE
)


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    ANNOUNCED = "announced"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    RELAY_FAILED = "relay_failed"
    INVALID = "invalid"


class ApprovalChoice(str, Enum):
    APPROVE_ONCE = "approve_once"
    REJECT = "reject"


class ApprovalRiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ApprovalActor(str, Enum):
    USER = "user"
    SYSTEM = "system"
    DEVICE = "device"
    UNKNOWN = "unknown"


class ConfirmationMethod(str, Enum):
    MANUAL = "manual"
    SIMULATED = "simulated"
    VOICE = "voice"
    DEVICE = "device"
    TIMEOUT = "timeout"
    SYSTEM = "system"


class ApprovalDecisionType(str, Enum):
    APPROVE_ONCE = "approve_once"
    REJECT = "reject"
    CANCEL = "cancel"
    EXPIRED = "expired"
    INVALID = "invalid"


_TERMINAL_STATUSES = frozenset(
    {
        ApprovalStatus.APPROVED,
        ApprovalStatus.REJECTED,
        ApprovalStatus.EXPIRED,
        ApprovalStatus.CANCELLED,
        ApprovalStatus.INVALID,
    }
)

_ALLOWED_TRANSITIONS = {
    ApprovalStatus.PENDING: frozenset(
        {
            ApprovalStatus.ANNOUNCED,
            ApprovalStatus.AWAITING_CONFIRMATION,
            ApprovalStatus.REJECTED,
            ApprovalStatus.EXPIRED,
            ApprovalStatus.CANCELLED,
            ApprovalStatus.INVALID,
            ApprovalStatus.RELAY_FAILED,
        }
    ),
    ApprovalStatus.ANNOUNCED: frozenset(
        {
            ApprovalStatus.AWAITING_CONFIRMATION,
            ApprovalStatus.REJECTED,
            ApprovalStatus.EXPIRED,
            ApprovalStatus.CANCELLED,
            ApprovalStatus.INVALID,
            ApprovalStatus.RELAY_FAILED,
        }
    ),
    ApprovalStatus.AWAITING_CONFIRMATION: frozenset(
        {
            ApprovalStatus.APPROVED,
            ApprovalStatus.REJECTED,
            ApprovalStatus.EXPIRED,
            ApprovalStatus.CANCELLED,
            ApprovalStatus.RELAY_FAILED,
            ApprovalStatus.INVALID,
        }
    ),
    ApprovalStatus.RELAY_FAILED: frozenset(),
}


def _parse_utc(value: str, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be an ISO 8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _utc_iso(parsed: datetime) -> str:
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_uuid(value: str, field_name: str, *, require_v4: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a UUID string")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{field_name} must be a UUID string") from exc
    if require_v4 and parsed.version != 4:
        raise ValueError(f"{field_name} must be a UUID4 string")
    return value


def _validate_identifier(value: Optional[str], field_name: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string or null")
    normalized = value.strip()
    if len(normalized) > MAX_IDENTIFIER_LENGTH or _CONTROL_CHARS.search(normalized):
        raise ValueError(f"{field_name} is invalid or too long")
    return normalized


def normalize_safe_summary(value: str) -> str:
    """Remove controls and collapse whitespace before hashing or display."""
    if not isinstance(value, str):
        raise ValueError("safe_summary must be a string")
    normalized = _WHITESPACE.sub(" ", _CONTROL_CHARS.sub(" ", value)).strip()
    if not normalized:
        raise ValueError("safe_summary must not be empty")
    if _SENSITIVE_VALUE.search(normalized):
        raise ValueError("safe_summary contains a credential-like value")
    if len(normalized) > MAX_SAFE_SUMMARY_LENGTH:
        raise ValueError("safe_summary is too long")
    return normalized


def _validate_short_text(value: str, field_name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    normalized = value.strip()
    if len(normalized) > maximum or _CONTROL_CHARS.search(normalized):
        raise ValueError(f"{field_name} is invalid or too long")
    return normalized


def _text_value(value: Any) -> str:
    """Use enum values when callers pass a str-compatible enum."""
    return str(getattr(value, "value", value))


def _sanitize_metadata(value: Any, key: str = "", depth: int = 0) -> Any:
    if depth > MAX_METADATA_DEPTH:
        return None
    if key and _SENSITIVE_KEY.search(key):
        return None
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for child_key, child_value in list(value.items())[:MAX_METADATA_ENTRIES]:
            safe_key = _validate_metadata_key(str(child_key))
            if safe_key is None:
                continue
            safe_value = _sanitize_metadata(child_value, safe_key, depth + 1)
            if safe_value is not None:
                result[safe_key] = safe_value
        return result
    if isinstance(value, (str, int, bool)) or value is None:
        if isinstance(value, str):
            if _SENSITIVE_VALUE.search(value):
                return None
            value = _CONTROL_CHARS.sub(" ", value).strip()
            return value[:MAX_METADATA_VALUE_LENGTH]
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("metadata contains a non-finite number")
        return value
    if isinstance(value, (list, tuple)):
        items = []
        for item in list(value)[:MAX_METADATA_ENTRIES]:
            safe_item = _sanitize_metadata(item, depth=depth + 1)
            if safe_item is not None:
                items.append(safe_item)
        return items
    raise ValueError("metadata contains an unsupported value")


def _validate_metadata_key(key: str) -> Optional[str]:
    normalized = key.strip()[:64]
    if not normalized or _CONTROL_CHARS.search(normalized) or _SENSITIVE_KEY.search(normalized):
        return None
    return normalized


def sanitize_approval_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(metadata, Mapping):
        raise ValueError("metadata must be an object")
    safe = _sanitize_metadata(metadata)
    return safe if isinstance(safe, dict) else {}


def make_approval_dedupe_key(
    source: str,
    session_id: Optional[str],
    action_type: str,
    tool_name: str,
    safe_summary: str,
) -> str:
    """Create a stable key from request content, never request/event IDs."""
    parts = (
        _validate_short_text(_text_value(source), "source", MAX_IDENTIFIER_LENGTH).casefold(),
        (session_id or "unknown").strip().casefold(),
        _validate_short_text(_text_value(action_type), "action_type", MAX_ACTION_TYPE_LENGTH).casefold(),
        _validate_short_text(_text_value(tool_name), "tool_name", MAX_TOOL_NAME_LENGTH).casefold(),
        normalize_safe_summary(safe_summary).casefold(),
    )
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"v1:{digest}"


@dataclass(frozen=True)
class ApprovalRequest:
    schema_version: str
    approval_id: str
    source: str
    event_id: Optional[str]
    session_id: Optional[str]
    project_id: Optional[str]
    device_id: Optional[str]
    requested_at: str
    expires_at: str
    action_type: str
    tool_name: str
    safe_summary: str
    risk_level: ApprovalRiskLevel
    choices: tuple[ApprovalChoice, ...]
    default_decision: ApprovalChoice
    requires_confirmation: bool
    correlation_id: Optional[str]
    dedupe_key: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported schema_version")
        _validate_uuid(self.approval_id, "approval_id", require_v4=True)
        if self.event_id is not None:
            _validate_uuid(self.event_id, "event_id")
            if self.event_id == self.approval_id:
                raise ValueError("event_id must differ from approval_id")
        object.__setattr__(self, "source", _validate_short_text(_text_value(self.source), "source", MAX_IDENTIFIER_LENGTH))
        for name, value in (
            ("session_id", self.session_id),
            ("project_id", self.project_id),
            ("device_id", self.device_id),
            ("correlation_id", self.correlation_id),
        ):
            _validate_identifier(value, name)
        requested = _parse_utc(self.requested_at, "requested_at")
        expires = _parse_utc(self.expires_at, "expires_at")
        if expires <= requested:
            raise ValueError("expires_at must be later than requested_at")
        object.__setattr__(self, "requested_at", _utc_iso(requested))
        object.__setattr__(self, "expires_at", _utc_iso(expires))
        object.__setattr__(self, "action_type", _validate_short_text(_text_value(self.action_type), "action_type", MAX_ACTION_TYPE_LENGTH))
        object.__setattr__(self, "tool_name", _validate_short_text(_text_value(self.tool_name), "tool_name", MAX_TOOL_NAME_LENGTH))
        object.__setattr__(self, "safe_summary", normalize_safe_summary(self.safe_summary))
        if not isinstance(self.risk_level, ApprovalRiskLevel):
            raise ValueError("risk_level must be ApprovalRiskLevel")
        if not isinstance(self.status, ApprovalStatus):
            raise ValueError("status must be ApprovalStatus")
        if not isinstance(self.choices, tuple) or not self.choices:
            raise ValueError("choices must be a non-empty tuple")
        if any(not isinstance(choice, ApprovalChoice) for choice in self.choices):
            raise ValueError("choices contains an invalid ApprovalChoice")
        if len(set(self.choices)) != len(self.choices):
            raise ValueError("choices must not contain duplicates")
        if set(self.choices) != {ApprovalChoice.APPROVE_ONCE, ApprovalChoice.REJECT}:
            raise ValueError("choices must contain approve_once and reject only")
        if self.default_decision is not ApprovalChoice.REJECT:
            raise ValueError("default_decision must be reject")
        if not isinstance(self.requires_confirmation, bool) or not self.requires_confirmation:
            raise ValueError("requires_confirmation must be true")
        _validate_short_text(self.dedupe_key, "dedupe_key", MAX_IDENTIFIER_LENGTH)
        expected_dedupe_key = make_approval_dedupe_key(
            self.source, self.session_id, self.action_type, self.tool_name, self.safe_summary
        )
        if self.dedupe_key != expected_dedupe_key:
            raise ValueError("dedupe_key does not match normalized request fields")
        object.__setattr__(self, "metadata", sanitize_approval_metadata(self.metadata))

    @classmethod
    def create(
        cls,
        *,
        source: str,
        requested_at: Optional[str] = None,
        expires_at: Optional[str] = None,
        session_id: Optional[str] = None,
        project_id: Optional[str] = None,
        device_id: Optional[str] = None,
        event_id: Optional[str] = None,
        action_type: str = "tool_permission",
        tool_name: str,
        safe_summary: str,
        risk_level: ApprovalRiskLevel = ApprovalRiskLevel.MEDIUM,
        correlation_id: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        ttl_seconds: float = 60.0,
    ) -> "ApprovalRequest":
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        requested = _parse_utc(requested_at, "requested_at") if requested_at else _now()
        expires = _parse_utc(expires_at, "expires_at") if expires_at else requested + timedelta(seconds=ttl_seconds)
        normalized_summary = normalize_safe_summary(safe_summary)
        dedupe_key = make_approval_dedupe_key(source, session_id, action_type, tool_name, normalized_summary)
        return cls(
            schema_version=SCHEMA_VERSION,
            approval_id=str(uuid.uuid4()),
            source=source,
            event_id=event_id,
            session_id=session_id,
            project_id=project_id,
            device_id=device_id,
            requested_at=_utc_iso(requested),
            expires_at=_utc_iso(expires),
            action_type=action_type,
            tool_name=tool_name,
            safe_summary=normalized_summary,
            risk_level=risk_level,
            choices=(ApprovalChoice.APPROVE_ONCE, ApprovalChoice.REJECT),
            default_decision=ApprovalChoice.REJECT,
            requires_confirmation=True,
            correlation_id=correlation_id,
            dedupe_key=dedupe_key,
            metadata=dict(metadata or {}),
        )

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        current = now or _now()
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now must include a timezone")
        current = current.astimezone(timezone.utc)
        return current >= _parse_utc(self.expires_at, "expires_at")

    def transition_to(self, status: ApprovalStatus) -> "ApprovalRequest":
        if not isinstance(status, ApprovalStatus):
            raise ValueError("status must be ApprovalStatus")
        if self.status in _TERMINAL_STATUSES:
            raise ValueError("terminal approval status cannot transition")
        if status not in _ALLOWED_TRANSITIONS.get(self.status, frozenset()):
            raise ValueError(f"invalid approval transition: {self.status.value} -> {status.value}")
        return replace(self, status=status)

    def expire_if_needed(self, now: Optional[datetime] = None) -> "ApprovalRequest":
        if self.status in _TERMINAL_STATUSES or not self.is_expired(now):
            return self
        return self.transition_to(ApprovalStatus.EXPIRED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "approval_id": self.approval_id,
            "source": self.source,
            "event_id": self.event_id,
            "session_id": self.session_id,
            "project_id": self.project_id,
            "device_id": self.device_id,
            "requested_at": self.requested_at,
            "expires_at": self.expires_at,
            "action_type": self.action_type,
            "tool_name": self.tool_name,
            "safe_summary": self.safe_summary,
            "risk_level": self.risk_level.value,
            "choices": [choice.value for choice in self.choices],
            "default_decision": self.default_decision.value,
            "requires_confirmation": self.requires_confirmation,
            "correlation_id": self.correlation_id,
            "dedupe_key": self.dedupe_key,
            "status": self.status.value,
            "metadata": self.metadata,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ApprovalRequest":
        required = {
            "schema_version", "approval_id", "source", "event_id", "session_id", "project_id",
            "device_id", "requested_at", "expires_at", "action_type", "tool_name", "safe_summary",
            "risk_level", "choices", "default_decision", "requires_confirmation", "correlation_id",
            "dedupe_key", "status", "metadata",
        }
        if not isinstance(data, Mapping):
            raise ValueError("ApprovalRequest JSON must be an object")
        missing = required - set(data)
        unknown = set(data) - required
        if missing:
            raise ValueError(f"missing ApprovalRequest fields: {sorted(missing)}")
        if unknown:
            raise ValueError(f"unknown ApprovalRequest fields: {sorted(unknown)}")
        choices = data["choices"]
        if not isinstance(choices, list):
            raise ValueError("choices must be an array")
        try:
            return cls(
                schema_version=data["schema_version"],
                approval_id=data["approval_id"],
                source=data["source"],
                event_id=data["event_id"],
                session_id=data["session_id"],
                project_id=data["project_id"],
                device_id=data["device_id"],
                requested_at=data["requested_at"],
                expires_at=data["expires_at"],
                action_type=data["action_type"],
                tool_name=data["tool_name"],
                safe_summary=data["safe_summary"],
                risk_level=ApprovalRiskLevel(data["risk_level"]),
                choices=tuple(ApprovalChoice(choice) for choice in choices),
                default_decision=ApprovalChoice(data["default_decision"]),
                requires_confirmation=data["requires_confirmation"],
                correlation_id=data["correlation_id"],
                dedupe_key=data["dedupe_key"],
                status=ApprovalStatus(data["status"]),
                metadata=data["metadata"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid ApprovalRequest fields") from exc

    @classmethod
    def from_json(cls, raw: str) -> "ApprovalRequest":
        try:
            decoded = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid ApprovalRequest JSON") from exc
        return cls.from_dict(decoded)


@dataclass(frozen=True)
class ApprovalDecision:
    schema_version: str
    decision_id: str
    approval_id: str
    decision: ApprovalDecisionType
    decided_at: str
    actor: ApprovalActor
    device_id: Optional[str]
    confirmation_method: ConfirmationMethod
    correlation_id: Optional[str]
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported schema_version")
        _validate_uuid(self.decision_id, "decision_id", require_v4=True)
        _validate_uuid(self.approval_id, "approval_id", require_v4=True)
        if self.decision_id == self.approval_id:
            raise ValueError("decision_id must differ from approval_id")
        _parse = _parse_utc(self.decided_at, "decided_at")
        object.__setattr__(self, "decided_at", _utc_iso(_parse))
        if not isinstance(self.decision, ApprovalDecisionType):
            raise ValueError("decision must be ApprovalDecisionType")
        if not isinstance(self.actor, ApprovalActor):
            raise ValueError("actor must be ApprovalActor")
        if not isinstance(self.confirmation_method, ConfirmationMethod):
            raise ValueError("confirmation_method must be ConfirmationMethod")
        _validate_identifier(self.device_id, "device_id")
        _validate_identifier(self.correlation_id, "correlation_id")
        object.__setattr__(self, "metadata", sanitize_approval_metadata(self.metadata))

    @classmethod
    def create(
        cls,
        approval_id: str,
        decision: ApprovalDecisionType,
        *,
        actor: ApprovalActor = ApprovalActor.USER,
        device_id: Optional[str] = None,
        confirmation_method: ConfirmationMethod = ConfirmationMethod.MANUAL,
        correlation_id: Optional[str] = None,
        decided_at: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> "ApprovalDecision":
        return cls(
            schema_version=SCHEMA_VERSION,
            decision_id=str(uuid.uuid4()),
            approval_id=approval_id,
            decision=decision,
            decided_at=decided_at or _utc_iso(_now()),
            actor=actor,
            device_id=device_id,
            confirmation_method=confirmation_method,
            correlation_id=correlation_id,
            metadata=dict(metadata or {}),
        )

    def validate_for(self, request: ApprovalRequest, now: Optional[datetime] = None) -> None:
        if self.approval_id != request.approval_id:
            raise ValueError("decision approval_id does not match request")
        if request.is_expired(now) and self.decision not in {
            ApprovalDecisionType.EXPIRED,
            ApprovalDecisionType.INVALID,
        }:
            raise ValueError("approval request has expired")
        if self.decision is ApprovalDecisionType.APPROVE_ONCE and ApprovalChoice.APPROVE_ONCE not in request.choices:
            raise ValueError("approve_once is not an allowed choice")
        if self.decision is ApprovalDecisionType.REJECT and ApprovalChoice.REJECT not in request.choices:
            raise ValueError("reject is not an allowed choice")
        if self.decision is ApprovalDecisionType.EXPIRED and not request.is_expired(now):
            raise ValueError("expired decision requires an expired request")
        if self.decision is ApprovalDecisionType.CANCEL and request.status in _TERMINAL_STATUSES:
            raise ValueError("cancel decision requires a non-terminal request")
        if self.decision in {ApprovalDecisionType.CANCEL, ApprovalDecisionType.EXPIRED, ApprovalDecisionType.INVALID}:
            return

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "approval_id": self.approval_id,
            "decision": self.decision.value,
            "decided_at": self.decided_at,
            "actor": self.actor.value,
            "device_id": self.device_id,
            "confirmation_method": self.confirmation_method.value,
            "correlation_id": self.correlation_id,
            "metadata": self.metadata,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ApprovalDecision":
        required = {
            "schema_version", "decision_id", "approval_id", "decision", "decided_at", "actor",
            "device_id", "confirmation_method", "correlation_id", "metadata",
        }
        if not isinstance(data, Mapping):
            raise ValueError("ApprovalDecision JSON must be an object")
        missing = required - set(data)
        unknown = set(data) - required
        if missing:
            raise ValueError(f"missing ApprovalDecision fields: {sorted(missing)}")
        if unknown:
            raise ValueError(f"unknown ApprovalDecision fields: {sorted(unknown)}")
        try:
            return cls(
                schema_version=data["schema_version"],
                decision_id=data["decision_id"],
                approval_id=data["approval_id"],
                decision=ApprovalDecisionType(data["decision"]),
                decided_at=data["decided_at"],
                actor=ApprovalActor(data["actor"]),
                device_id=data["device_id"],
                confirmation_method=ConfirmationMethod(data["confirmation_method"]),
                correlation_id=data["correlation_id"],
                metadata=data["metadata"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid ApprovalDecision fields") from exc

    @classmethod
    def from_json(cls, raw: str) -> "ApprovalDecision":
        try:
            decoded = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid ApprovalDecision JSON") from exc
        return cls.from_dict(decoded)


class ReplayError(ValueError):
    """Raised when an approval or decision identifier is reused."""


class DecisionReplayGuard:
    """Small thread-safe one-shot guard for a future relay/store.

    It intentionally keeps only opaque IDs.  Persistence and request storage
    belong to Step 2.2 and are not included here.
    """

    def __init__(self) -> None:
        self._decision_ids: set[str] = set()
        self._approval_ids: set[str] = set()
        self._lock = threading.Lock()

    def consume(self, decision: ApprovalDecision) -> None:
        _validate_uuid(decision.decision_id, "decision_id", require_v4=True)
        _validate_uuid(decision.approval_id, "approval_id", require_v4=True)
        with self._lock:
            if decision.decision_id in self._decision_ids:
                raise ReplayError("decision_id has already been used")
            if decision.approval_id in self._approval_ids:
                raise ReplayError("approval_id has already been decided")
            self._decision_ids.add(decision.decision_id)
            self._approval_ids.add(decision.approval_id)


__all__ = [
    "ApprovalActor",
    "ApprovalChoice",
    "ApprovalDecision",
    "ApprovalDecisionType",
    "ApprovalRequest",
    "ApprovalRiskLevel",
    "ApprovalStatus",
    "ConfirmationMethod",
    "DecisionReplayGuard",
    "ReplayError",
    "SCHEMA_VERSION",
    "MAX_SAFE_SUMMARY_LENGTH",
    "make_approval_dedupe_key",
    "normalize_safe_summary",
    "sanitize_approval_metadata",
]
