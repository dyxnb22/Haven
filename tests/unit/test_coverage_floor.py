"""单个项目级覆盖率百分比可能掩盖一个几乎没有覆盖的关键文件。

总体 89% 无法说明 `domain/policy.py` 是 99% 还是 20%——一个覆盖良好的大型模块
可以补贴薄弱模块。因此，决定权限、证据和预算的层采用逐文件下限，避免未来模块
在总体数字保持不变时，几乎未经测试就进入这些层。
"""

from scripts.check_coverage_floor import BASE_FLOOR, CORE_FLOOR, floor_for, violations


class TestWhichFilesAreGated:
    def test_high_risk_paths_keep_the_strict_floor(self) -> None:
        assert floor_for("src/haven/domain/policy.py") == CORE_FLOOR
        assert floor_for("src/haven/application/tool_pipeline.py") == CORE_FLOOR

    def test_other_core_files_use_the_collapse_floor(self) -> None:
        assert floor_for("src/haven/contracts/events.py") == BASE_FLOOR
        assert floor_for("src/haven/ports/model.py") == BASE_FLOOR
        assert floor_for("src/haven/application/registry.py") == BASE_FLOOR
        assert floor_for("src/haven/application/tool_mutations.py") == BASE_FLOOR

    def test_windows_paths_are_normalized_before_risk_classification(self) -> None:
        assert floor_for(r"src\haven\domain\policy.py") == CORE_FLOOR

    def test_platform_and_ui_surfaces_are_not_gated_here(self) -> None:
        """它们由总体数字和平台专用套件覆盖；逐文件下限会编码一个没有任何平台都能
        达到的数字。"""
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

    def test_each_violation_reports_its_own_risk_floor(self) -> None:
        found = violations(
            {
                "src/haven/domain/policy.py": 69.0,
                "src/haven/application/registry.py": 69.0,
            }
        )
        assert found == [
            ("src/haven/application/registry.py", 69.0, BASE_FLOOR),
            ("src/haven/domain/policy.py", 69.0, CORE_FLOOR),
        ]
