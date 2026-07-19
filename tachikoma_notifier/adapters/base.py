"""Adapter protocol shared by Claude Code and future providers."""
from __future__ import annotations

from typing import Any, Mapping, Optional, Protocol

from tachikoma_notifier.events import TachikomaEvent


class EventAdapter(Protocol):
    def can_handle(self, payload: Mapping[str, Any]) -> bool:
        ...

    def normalize(self, payload: Mapping[str, Any]) -> Optional[TachikomaEvent]:
        ...
