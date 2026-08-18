"""时钟端口，使时间可以注入测试和评估。"""

from __future__ import annotations

from typing import Protocol


class ClockPort(Protocol):
    def now_iso(self) -> str: ...

    def monotonic(self) -> float: ...
