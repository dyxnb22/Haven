"""Event sink port: where envelopes go after persistence (UI, log, replay)."""

from __future__ import annotations

from typing import Protocol

from haven.contracts.events import EventEnvelope


class EventSinkPort(Protocol):
    async def emit(self, envelope: EventEnvelope) -> None: ...
