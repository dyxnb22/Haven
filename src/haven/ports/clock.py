"""Clock port so time is injectable in tests and eval."""

from __future__ import annotations

from typing import Protocol


class ClockPort(Protocol):
    def now_iso(self) -> str: ...

    def monotonic(self) -> float: ...
