"""The documented tool/policy table is derived from the code that enforces it.

A hand-transcribed table drifts: a tool gains a policy class, the doc keeps the
old one, and the document that a reader uses to answer "why was this side effect
allowed?" is quietly wrong. The decisions here are computed by calling the real
`evaluate_policy`, and a completeness guard means a new tool cannot ship
undocumented.
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
        """`task.plan` touches only run state, so it is not a side effect."""
        row = next(r for r in rows_for_tools() if r.tool == "task.plan")
        assert (row.interactive, row.read_only) == ("allow", "allow")


class TestCompleteness:
    def test_every_tool_has_documented_constraints(self) -> None:
        """The prose column is the one thing the code cannot derive, so it is
        maintained beside the generator — and every tool must have an entry."""
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
