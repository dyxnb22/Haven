"""A single project-wide coverage percentage can hide a bare critical file.

89% overall says nothing about whether `domain/policy.py` is at 99% or 20% —
one well-covered large module subsidises a thin one. The layers that decide
permission, evidence, and budgets therefore carry a per-file floor, so a future
module cannot land in them essentially untested while the headline number holds.
"""

from scripts.check_coverage_floor import CORE_FLOOR, floor_for, violations


class TestWhichFilesAreGated:
    def test_the_decision_making_layers_are_gated(self) -> None:
        assert floor_for("src/haven/domain/policy.py") == CORE_FLOOR
        assert floor_for("src/haven/application/tool_pipeline.py") == CORE_FLOOR
        assert floor_for("src/haven/contracts/events.py") == CORE_FLOOR
        assert floor_for("src/haven/ports/model.py") == CORE_FLOOR

    def test_platform_and_ui_surfaces_are_not_gated_here(self) -> None:
        """They are covered by the overall figure and by platform-specific
        suites; a per-file floor would encode a number no platform can meet."""
        assert floor_for("src/haven/sandbox/landlock_launcher.py") is None
        assert floor_for("src/haven/interfaces/cli.py") is None


class TestViolations:
    def test_a_gated_file_below_the_floor_is_reported(self) -> None:
        found = violations({"src/haven/domain/policy.py": 40.0})
        assert found == [("src/haven/domain/policy.py", 40.0, CORE_FLOOR)]

    def test_a_gated_file_at_the_floor_passes(self) -> None:
        assert violations({"src/haven/domain/policy.py": float(CORE_FLOOR)}) == []

    def test_an_ungated_file_is_ignored_however_low(self) -> None:
        assert violations({"src/haven/interfaces/cli.py": 1.0}) == []

    def test_every_violation_is_reported_not_just_the_first(self) -> None:
        found = violations(
            {
                "src/haven/domain/policy.py": 10.0,
                "src/haven/application/registry.py": 20.0,
                "src/haven/domain/budget.py": 99.0,
            }
        )
        assert len(found) == 2
