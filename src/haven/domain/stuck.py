"""Stuck-loop detection.

If the model keeps proposing the same tool call with the same arguments and
keeps getting the same result, it is not making progress and the run must stop
instead of burning budget.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from haven.domain.digest import digest_of


def call_fingerprint(tool_name: str, canonical_args_json: str, result_digest: str) -> str:
    return digest_of({"tool": tool_name, "args": canonical_args_json, "result": result_digest})


@dataclass(slots=True)
class StuckLoopDetector:
    """Counts consecutive identical (tool, args, result) observations."""

    threshold: int = 3
    _last_fingerprint: str | None = field(default=None, repr=False)
    _repeat_count: int = field(default=0, repr=False)

    def observe(self, fingerprint: str) -> bool:
        """Record one observation; return True when the loop is stuck."""
        if fingerprint == self._last_fingerprint:
            self._repeat_count += 1
        else:
            self._last_fingerprint = fingerprint
            self._repeat_count = 1
        return self._repeat_count >= self.threshold
