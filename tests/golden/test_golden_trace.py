"""黄金追踪：相同的夹具 + ScriptedModel 必须始终产生相同的核心事件序列，TUI
也必须产生与无头模式相同的追踪。

黄金文件已提交。需要明确重新生成时运行：

    HAVEN_UPDATE_GOLDEN=1 uv run pytest tests/golden -q

然后审阅差异——这里的变更意味着代理可见行为发生了变化。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from haven.contracts.events import EventEnvelope
from haven.domain.enums import RunStatus
from tests.integration.harness import Harness, finish, make_repo, text, tool

GOLDEN_DIR = Path(__file__).parent / "data"

#: 每次运行之间合理变化的字段，因此不属于 golden 契约（计时、绝对路径、
#: 内容摘要、生成的 ID）。
_VOLATILE = {
    "run_id",
    "workspace",
    "workspace_digest",
    "approval_id",
    "request_digest",
    "ticket_digest",
    # 设计上保持不透明：它让运行时能够察觉 envelope 发生变化，但不会说明
    # “什么”发生了变化。`system_prompt_chars` 特意保留在 golden 中，因此
    # prompt 编辑会显示为 diff。
    "system_prompt_digest",
    "duration_ms",
    "ttft_ms",
    "at",
    "seq",
    "git_branch",
    "git_commit",
    "cost_usd",
}


def normalize(envelopes: list[EventEnvelope]) -> list[dict[str, Any]]:
    """将追踪归约为稳定且定义行为的形状。"""
    normalized: list[dict[str, Any]] = []
    for envelope in envelopes:
        payload = envelope.event.model_dump(mode="json")
        stable = {k: v for k, v in payload.items() if k not in _VOLATILE}
        if "summary" in stable and isinstance(stable["summary"], str):
            # 人类摘要中会出现摘要和字节数
            stable["summary"] = _mask(stable["summary"])
        if "preview" in stable:
            # 预览包含绝对路径（运行 recipe 的解释器、工作区根目录），因此
            # 连长度都会因机器而异。预览必须包含什么由相关集成测试断言；
            # 在这里，只有它存在属于契约。
            stable["preview"] = "<preview>"
        if stable.get("kind") == "context.built":
            stable = _mask_timing_sizes(stable)
        if stable.get("usage_estimated") is True:
            # 估算值是对包含实测耗时的 transcript 取 characters//4，因此一次
            # 检查耗时 99 ms 而不是 100 ms 就可能使它变化一个 token。使用估算
            # 是行为的一部分，应保留；具体数值只是秒表产生的结果。
            for key in ("input_tokens", "output_tokens"):
                if key in stable:
                    stable[key] = "<estimated>"
        if "text" in stable and len(str(stable["text"])) > 120:
            stable["text"] = f"<{len(str(stable['text']))} chars>"
        normalized.append(stable)
    return normalized


def _mask_timing_sizes(event: dict[str, Any]) -> dict[str, Any]:
    """去除可能随计时器变化的字节数。

    工具输出带有 `duration_ms`，因此耗时 99 ms 的检查会产生比耗时 100 ms 少一个
    字节的区段。值得固定的是程序编写的大小——尤其是系统提示词——因此保留这些
    大小，只屏蔽带有计时信息的区段。
    """
    masked = dict(event)
    masked.pop("total_bytes", None)
    masked["segments"] = [
        {**segment, "size_bytes": "<varies>"} if segment.get("source") == "tool_output" else segment
        for segment in event.get("segments", [])
    ]
    return masked


def _mask(value: str) -> str:
    masked = re.sub(r"\b[0-9a-f]{8,}\b", "<digest>", value)
    masked = re.sub(r"\b\d+ms\b", "<ms>", masked)
    return re.sub(r'"duration_ms":\s*\d+', '"duration_ms": <ms>', masked)


def load_or_write_golden(name: str, actual: list[dict[str, Any]]) -> list[dict[str, Any]]:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    path = GOLDEN_DIR / f"{name}.json"
    if os.environ.get("HAVEN_UPDATE_GOLDEN") or not path.exists():
        path.write_text(json.dumps(actual, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def edit_journey_turns() -> list[list[Any]]:
    return [
        [
            text("Locating the bug."),
            tool("c1", "repo.search", pattern="BUG", path="."),
            finish("tool_calls"),
        ],
        [tool("c2", "repo.read", path="src/calc.py"), finish("tool_calls")],
        [
            tool(
                "c3",
                "repo.edit",
                path="src/calc.py",
                old_string="return a - b  # BUG: should be +",
                new_string="return a + b",
                summary="fix add()",
            ),
            finish("tool_calls"),
        ],
        [tool("c4", "repo.diff"), finish("tool_calls")],
        [tool("c5", "repo.check", recipe_id="verify-calc"), finish("tool_calls")],
        [text("Fixed add(); diff and verify-calc both recorded."), finish()],
    ]


class TestGoldenTrace:
    async def test_edit_journey_matches_golden(self, tmp_path: Path) -> None:
        h = Harness(make_repo(tmp_path), edit_journey_turns())
        outcome = await h.service.run("Fix the bug in add()")
        assert outcome.status is RunStatus.SUCCEEDED

        stored = await h.store.load_events(outcome.run_id)
        actual = normalize(stored)
        expected = load_or_write_golden("edit_journey", actual)
        assert actual == expected

    async def test_trace_is_stable_across_identical_runs(self, tmp_path: Path) -> None:
        first_repo = make_repo(tmp_path / "a")
        second_repo = make_repo(tmp_path / "b")

        h1 = Harness(first_repo, edit_journey_turns())
        outcome1 = await h1.service.run("Fix the bug in add()")
        h2 = Harness(second_repo, edit_journey_turns())
        outcome2 = await h2.service.run("Fix the bug in add()")

        assert normalize(await h1.store.load_events(outcome1.run_id)) == normalize(
            await h2.store.load_events(outcome2.run_id)
        )

    async def test_golden_covers_the_whole_pipeline(self, tmp_path: Path) -> None:
        """黄金追踪只有覆盖每个门禁时才有意义。"""
        h = Harness(make_repo(tmp_path), edit_journey_turns())
        outcome = await h.service.run("Fix the bug in add()")
        kinds = {env.event.kind for env in await h.store.load_events(outcome.run_id)}
        assert {
            "run.created",
            "step.started",
            "context.built",
            "model.completed",
            "tool.proposed",
            "policy.decided",
            "approval.requested",
            "approval.decided",
            "execution.started",
            "tool.completed",
            "evidence.recorded",
            "diff.preview",
            "run.finished",
        } <= kinds


@pytest.mark.timeout(60)
async def test_tui_and_headless_produce_the_same_trace(tmp_path: Path) -> None:
    """第 6 周不变量：TUI 仅是接口层，因此通过它驱动同一个 ScriptedModel，必须
    产生与无头模式相同的核心追踪。"""
    from haven.interfaces.tui.app import HavenApp
    from tests.tui.test_tui_journey import (
        _approve_pending,
        _settle,
        _submit,
        _wait_ready,
        make_builder,
    )

    headless_repo = make_repo(tmp_path / "headless")
    h = Harness(headless_repo, edit_journey_turns())
    outcome = await h.service.run("Fix the bug in add()")
    headless = normalize(await h.store.load_events(outcome.run_id))

    tui_repo = make_repo(tmp_path / "tui")
    app = HavenApp(
        workspace=tui_repo, services_builder=make_builder(tui_repo, edit_journey_turns())
    )
    async with app.run_test() as pilot:
        await _wait_ready(app, pilot)
        await _submit(app, pilot, "Fix the bug in add()")
        await _approve_pending(app, pilot, "a", count=2)
        await _settle(pilot, 60)
        assert app._state.status == "succeeded"  # noqa: SLF001
        tui_run_id = app._state.run_id  # noqa: SLF001
        tui = normalize(await app._services.store.load_events(tui_run_id))  # noqa: SLF001

    assert tui == headless
