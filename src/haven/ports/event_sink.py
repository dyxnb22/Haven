"""事件接收端口：事件封装持久化后的去向（UI、日志、重放）。"""

from __future__ import annotations

from typing import Protocol

from haven.contracts.events import EventEnvelope


class EventSinkPort(Protocol):
    async def emit(self, envelope: EventEnvelope) -> None: ...
