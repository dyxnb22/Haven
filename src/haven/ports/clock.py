"""时钟端口，使时间可以注入测试和评估。"""

from __future__ import annotations

from typing import Protocol


class ClockPort(Protocol):
    """为应用层提供可替换的墙上时间和单调时间。"""

    def now_iso(self) -> str:
        """返回当前墙上时间的 ISO-8601 字符串。"""
        ...

    def monotonic(self) -> float:
        """返回只用于计算耗时的单调时钟秒数。"""
        ...
