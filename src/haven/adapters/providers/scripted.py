"""Scripted (fake) model: deterministic event playback for tests and eval.

A script is a sequence of turns; each call to generate_stream() plays the next
turn. Fixtures are plain JSON so eval cases are reviewable by hand.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

from pydantic import TypeAdapter

from haven.contracts.model import ModelEvent, ModelRequest
from haven.ports.model import ProviderError

_TURNS_ADAPTER: TypeAdapter[list[list[ModelEvent]]] = TypeAdapter(list[list[ModelEvent]])


class ScriptedModel:
    """Implements ModelPort by replaying pre-authored turns."""

    def __init__(
        self,
        turns: Sequence[Sequence[ModelEvent]],
        *,
        repeat_last: bool = False,
        name: str = "scripted",
    ) -> None:
        self._turns: list[list[ModelEvent]] = [list(t) for t in turns]
        self._cursor = 0
        self._repeat_last = repeat_last
        self._name = name
        self.requests_seen: list[ModelRequest] = []

    @property
    def model_name(self) -> str:
        return self._name

    @classmethod
    def from_json(cls, text: str, *, repeat_last: bool = False) -> ScriptedModel:
        raw = json.loads(text)
        turns = _TURNS_ADAPTER.validate_python(raw["turns"])
        return cls(turns, repeat_last=repeat_last or bool(raw.get("repeat_last", False)))

    @classmethod
    def from_file(cls, path: Path) -> ScriptedModel:
        return cls.from_json(path.read_text(encoding="utf-8"))

    def generate_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        self.requests_seen.append(request)
        if self._cursor >= len(self._turns):
            if self._repeat_last and self._turns:
                turn = self._turns[-1]
            else:
                raise ProviderError("exhausted", "scripted model has no more turns")
        else:
            turn = self._turns[self._cursor]
            self._cursor += 1
        return self._play(list(turn))

    @staticmethod
    async def _play(events: list[ModelEvent]) -> AsyncIterator[ModelEvent]:
        for event in events:
            yield event
