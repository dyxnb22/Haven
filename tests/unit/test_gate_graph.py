"""The quality gates are declared once and selected by mode.

CI and the local command list used to be two hand-maintained copies of the same
sequence, which drift silently: a gate added to one is simply absent from the
other until something ships broken. Declaring them once, with dependencies,
makes "what CI runs" and "what I can run here" the same object.
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
        """Selecting a gate whose dependency is in another mode must not run it
        without that dependency; the dependency comes along."""
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
        """The shipped declaration must itself pass every structural check."""
        from scripts.gates import GATES

        validate(GATES)
        assert any(g.modes for g in GATES)
