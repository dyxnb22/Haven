"""Stuck-loop detection.

If the model keeps proposing the same tool call with the same arguments and
keeps getting the same result, it is not making progress and the run must stop
instead of burning budget.

A warning tier was tried here and removed: it never fired once in 42 live runs,
and a trace study of those journals (`evals/trace_study.py`) found that a
non-converging run is not a repeating one — only 1 of 11 slow runs repeated any
call at all, the same rate as the fast cohort. The record is in
`docs/notes/rejected/0002`. This detector remains a backstop against literal
thrash, not an answer to non-convergence.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from haven.domain.digest import digest_of


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
