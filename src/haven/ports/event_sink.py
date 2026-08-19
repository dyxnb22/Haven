"""事件接收端口：事件封装持久化后的去向（UI、日志、重放）。"""

from __future__ import annotations

from typing import Protocol

from haven.contracts.events import EventEnvelope


class EventSinkPort(Protocol):
    """事件输出端口；CLI、TUI 和持久化层都可作为消费者。"""

    async def emit(self, envelope: EventEnvelope) -> None:
        """接收一个已经封装并分配序号的应用事件。"""
        ...
