"""Stuck-loop detection.

If the model keeps proposing the same tool call with the same arguments and
keeps getting the same result, it is not making progress and the run must stop
instead of burning budget.

Stopping is the last resort, not the first response. The dominant live failure
class is the model not converging in time (ADR 0023), and by the time a run is
killed for repetition its remaining budget is already spent. So repetition
escalates: the second identical observation returns `nudge`, which the loop
turns into one program-written note telling the model the call produced nothing
new, and only a further repeat returns `stuck`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from haven.domain.digest import digest_of

#: `ok` keep going · `nudge` warn the model once · `stuck` stop the run.
StuckVerdict = Literal["ok", "nudge", "stuck"]


def call_fingerprint(tool_name: str, arguments_json: str, result_text: str) -> str:
    """Identity of one (call, outcome) observation.

    The only definition — the run loop calls this rather than composing its
    own digest, so what the tests pin is what ships. The value is compared
    only against other fingerprints inside one run: it is never persisted,
    journaled, or compared across versions, so its exact shape is free to
    change.

    `result_text` is the observation the model actually saw (a digest of it
    would discriminate identically); passing the text keeps the caller from
    having to hash twice.
    """
    return digest_of({"tool": tool_name, "args": arguments_json, "result": result_text})


@dataclass(slots=True)
class StuckLoopDetector:
    """Counts consecutive identical (tool, args, result) observations."""

    threshold: int = 3
    #: Repeat count that earns the one-time warning. Below `threshold`, so the
    #: model is told before the budget is gone rather than after.
    nudge_at: int = 2
    _last_fingerprint: str | None = field(default=None, repr=False)
    _repeat_count: int = field(default=0, repr=False)

    def observe(self, fingerprint: str) -> StuckVerdict:
        """Record one observation and say what the loop should do about it.

        The nudge fires on the exact repeat count rather than from it onwards,
        so one episode produces at most one warning; a second would only spend
        context restating the first. Any different observation is progress and
        resets the count, so a later repetition is a fresh episode.
        """
        if fingerprint == self._last_fingerprint:
            self._repeat_count += 1
        else:
            self._last_fingerprint = fingerprint
            self._repeat_count = 1
        if self._repeat_count >= self.threshold:
            return "stuck"
        return "nudge" if self._repeat_count == self.nudge_at else "ok"
