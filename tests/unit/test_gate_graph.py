"""质量门禁只声明一次，并按模式选择。

CI 和本地命令列表过去是同一序列的两份手工维护副本，会悄悄漂移：某处新增门禁后，
另一处会一直缺失，直到发布损坏内容。将它们连同依赖声明一次，使“CI 运行什么”和
“我在这里能运行什么”成为同一个对象。
"""

import pytest

from scripts.gates import Gate, cycle_in, order_for, select, validate


def _gates() -> list[Gate]:
    return [
        Gate("fmt", "ruff format --check .", modes=("fast", "full")),
        Gate("lint", "ruff check .", modes=("fast", "full")),
        Gate("tests", "pytest", modes=("full",)),
        Gate("cov", "check-coverage", modes=("full",), needs=("tests",)),
    ]


class TestSelection:
    def test_a_mode_selects_only_its_gates(self) -> None:
        assert [g.id for g in select(_gates(), "fast")] == ["fmt", "lint"]

    def test_selection_pulls_in_dependencies(self) -> None:
        """选择一个依赖属于其他模式的门禁时，不得在没有该依赖的情况下运行；依赖会
        一并加入。"""
        gates = [
            Gate("tests", "pytest", modes=("full",)),
            Gate("cov", "check-coverage", modes=("quick",), needs=("tests",)),
        ]
        assert [g.id for g in select(gates, "quick")] == ["tests", "cov"]

    def test_dependencies_run_before_dependents(self) -> None:
        assert order_for(_gates(), ["cov", "tests"]) == ["tests", "cov"]


class TestValidation:
    def test_duplicate_ids_are_rejected(self) -> None:
        dupes = [Gate("a", "x"), Gate("a", "y")]
        with pytest.raises(ValueError, match="duplicate"):
            validate(dupes)

    def test_an_unknown_dependency_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown"):
            validate([Gate("a", "x", needs=("ghost",))])

    def test_a_dependency_cycle_is_detected(self) -> None:
        looped = [Gate("a", "x", needs=("b",)), Gate("b", "y", needs=("a",))]
        assert cycle_in(looped) is True
        with pytest.raises(ValueError, match="cycle"):
            validate(looped)

    def test_the_real_gate_graph_is_valid(self) -> None:
        """发布的声明本身必须通过所有结构检查。"""
        from scripts.gates import GATES

        validate(GATES)
        assert any(g.modes for g in GATES)
