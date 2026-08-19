"""脚本化（fake）模型：用于测试和评估的确定性事件回放。

脚本是一系列轮次；每次调用 generate_stream() 都会播放下一轮。夹具使用普通 JSON，
因此评估案例可以人工审阅。
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
    """按预先编排的事件序列响应，用于测试确定性的应用流程。

    通过回放预先编写的轮次实现 ModelPort。
    """

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
        """返回脚本模型名称，用于评估报告。"""
        return self._name

    @classmethod
    def from_json(cls, text: str, *, repeat_last: bool = False) -> ScriptedModel:
        """从包含 turns 的 JSON 文本构造脚本模型。"""
        raw = json.loads(text)
        turns = _TURNS_ADAPTER.validate_python(raw["turns"])
        return cls(turns, repeat_last=repeat_last or bool(raw.get("repeat_last", False)))

    @classmethod
    def from_file(cls, path: Path) -> ScriptedModel:
        """读取 JSON 文件并构造脚本模型。"""
        return cls.from_json(path.read_text(encoding="utf-8"))

    def generate_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        """记录请求并播放下一轮预置事件；耗尽时抛出稳定的 exhausted 错误。"""
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
