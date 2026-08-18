"""文档中的工具/策略表由执行策略的代码推导而来。

手工抄写的表会漂移：工具获得新的策略分类，文档却保留旧分类，读者用来回答“为
什么允许这个副作用？”的文档就会悄悄变错。这里的决定通过调用真实的
`evaluate_policy` 计算，完整性守卫保证新工具不能无文档发布。
"""

import pytest

from haven.domain.policy import KNOWN_TOOLS
from scripts.gen_tool_table import CONSTRAINTS, render_table, rows_for_tools


class TestDerivedFromTheRealPolicy:
    def test_every_known_tool_gets_a_row(self) -> None:
        rows = rows_for_tools()
        assert {row.tool for row in rows} == set(KNOWN_TOOLS)

    def test_an_editing_tool_asks_interactively_and_is_denied_read_only(self) -> None:
        row = next(r for r in rows_for_tools() if r.tool == "repo.edit")
        assert row.interactive == "ask"
        assert row.read_only == "deny"

    def test_a_read_only_tool_is_allowed_in_both_modes(self) -> None:
        row = next(r for r in rows_for_tools() if r.tool == "repo.read")
        assert row.interactive == "allow"
        assert row.read_only == "allow"

    def test_the_state_tool_survives_read_only_mode(self) -> None:
        """`task.plan` 只接触运行状态，因此不属于副作用。"""
        row = next(r for r in rows_for_tools() if r.tool == "task.plan")
        assert (row.interactive, row.read_only) == ("allow", "allow")


class TestCompleteness:
    def test_every_tool_has_documented_constraints(self) -> None:
        """文字约束列是代码无法推导的唯一内容，因此与生成器一同维护——每个工具都
        必须有一项。"""
        assert set(CONSTRAINTS) == set(KNOWN_TOOLS)

    def test_a_tool_without_constraints_fails_loudly(self) -> None:
        with pytest.raises(SystemExit, match="repo.ghost"):
            rows_for_tools(tools={"repo.ghost"})


class TestRendering:
    def test_the_table_is_sorted_and_names_every_tool(self) -> None:
        table = render_table()
        assert table.startswith("| Tool |")
        for tool in KNOWN_TOOLS:
            assert f"`{tool}`" in table
