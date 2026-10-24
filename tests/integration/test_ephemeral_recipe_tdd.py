"""Integration tests for ephemeral reproducer recipe proposal and TDD cycle (ADR 0043)."""

from __future__ import annotations

import sys
from pathlib import Path

from haven.contracts.events import ToolCompleted
from haven.domain.enums import RunStatus, StopReason
from tests.integration.harness import Harness, finish, make_repo, text, tool


def completed(h: Harness) -> list[ToolCompleted]:
    return [e for e in h.sink.events_of("tool.completed") if isinstance(e, ToolCompleted)]


class TestEphemeralReproducerTDD:
    async def test_bare_repo_without_recipes_fails_evidence_gate_on_edit(
        self, tmp_path: Path
    ) -> None:
        """没有配置验证配方的代码库，一旦进行代码编辑，Evidence Gate 会因无法验证而失败关闭。"""
        repo = make_repo(tmp_path)
        turns = [
            [tool("c1", "repo.read", path="src/calc.py"), finish("tool_calls")],
            [
                tool(
                    "c2",
                    "repo.edit",
                    path="src/calc.py",
                    old_string="a - b  # BUG: should be +",
                    new_string="a + b",
                ),
                finish("tool_calls"),
            ],
            [tool("c3", "repo.diff"), finish("tool_calls")],
            [text("I fixed the bug."), finish()],
        ]
        # 初始化时不挂载任何已注册配方（传入空字典 recipes）
        h = Harness(repo, turns, recipes={})
        outcome = await h.service.run("Fix the addition bug")

        assert outcome.status is RunStatus.STOPPED
        assert outcome.stop_reason is StopReason.VERIFICATION_UNAVAILABLE
        assert outcome.gate_reason == "verification_unavailable"

    async def test_ephemeral_recipe_propose_enables_tdd_evidence_satisfaction(
        self, tmp_path: Path
    ) -> None:
        """通过 recipe.propose 动态提议复现配方，走通 Red -> Green -> Evidence Gate 闭环。"""
        repo = make_repo(tmp_path)
        turns = [
            [tool("c1", "repo.read", path="src/calc.py"), finish("tool_calls")],
            # 1. 提议临时复现配方
            [
                tool(
                    "c2",
                    "recipe.propose",
                    recipe_id="calc-repro",
                    argv=[sys.executable, "verify_calc.py"],
                    timeout_seconds=30.0,
                    description="verify 2 + 3 == 5",
                ),
                finish("tool_calls"),
            ],
            # 2. 执行检查证明失败 (Red: 2 - 3 == -1 != 5, exit code 1)
            [tool("c3", "repo.check", recipe_id="calc-repro"), finish("tool_calls")],
            # 3. 修复代码
            [
                tool(
                    "c4",
                    "repo.edit",
                    path="src/calc.py",
                    old_string="a - b  # BUG: should be +",
                    new_string="a + b",
                ),
                finish("tool_calls"),
            ],
            # 4. 再次执行检查证明成功 (Green: 2 + 3 == 5, exit code 0)
            [tool("c5", "repo.check", recipe_id="calc-repro"), finish("tool_calls")],
            # 5. 记录 diff 证据
            [tool("c6", "repo.diff"), finish("tool_calls")],
            # 6. 给出最终答案
            [
                text("I reproduced the bug, fixed calc.py, and verified calc-repro is now green."),
                finish(),
            ],
        ]
        # 初始完全没有任何配方
        h = Harness(repo, turns, recipes={})
        outcome = await h.service.run("Fix the addition bug using TDD reproducer")

        assert outcome.status is RunStatus.SUCCEEDED
        assert outcome.stop_reason is StopReason.EVIDENCE_SATISFIED
        assert outcome.gate_reason == "evidence_satisfied"

        tool_events = completed(h)
        assert len(tool_events) == 6

        # 检查 c2: recipe.propose 成功注册
        propose_event = tool_events[1]
        assert propose_event.tool_name == "recipe.propose"
        assert propose_event.status == "ok"
        assert "calc-repro" in propose_event.summary

        # 检查 c3: 第一次 check (Red) 失败，退出码为 1
        first_check = tool_events[2]
        assert first_check.tool_name == "repo.check"
        assert first_check.status == "ok"
        assert '"exit_code": 1' in first_check.summary

        # 检查 c5: 第二次 check (Green) 成功，退出码为 0
        second_check = tool_events[4]
        assert second_check.tool_name == "repo.check"
        assert second_check.status == "ok"
        assert '"exit_code": 0' in second_check.summary
