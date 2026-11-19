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


def test_a_file_the_report_never_saw_is_a_failure_not_a_pass() -> None:
    """没被执行过的模块是 0% 覆盖，不是"没问题"。

    门禁原先只看覆盖率报告里出现了哪些文件，而报告只包含被执行过的模块：一个新增的、
    还没有任何代码路径 import 的源文件根本不在报告里，`floor_for` 永远不会被咨询。
    已实测：放一个没人 import 的 `src/haven/domain/_probe.py` 进去，报告里不含它，
    门禁却打印"每个被把关的文件都达标"并退出 0。
    """
    import scripts.check_coverage_floor as floor

    # 取一个真实存在且受门禁的文件
    real = "src/haven/domain/policy.py"
    assert floor.floor_for(real) == floor.CORE_FLOOR, "这个路径必须真的受门禁"
    assert real in floor.gated_paths_on_disk(), "磁盘集合不能是空的，否则测试在测空文本"

    # 报告里没有它 → 必须被报出来（缺席不等于达标）
    assert real in floor.unreported({})
    # 报告里有它 → 它自己不该再被报（其余文件仍缺席，那是预期的）
    assert real not in floor.unreported({real: 90.0})
    # 不受门禁的文件不在磁盘集合里
    assert "src/haven/interfaces/tui/app.py" not in floor.gated_paths_on_disk()


def test_main_fails_when_a_gated_file_is_absent_from_the_report() -> None:
    """`main()` 必须把"报告里没有这个文件"判成失败。

    只测 `unreported()` 是不够的：门禁可能算出了缺席集合却在 main 里把它丢掉——那正是
    "算得出却不报"的形状，而上一个测试抓不到。
    """
    import scripts.check_coverage_floor as floor

    real = "src/haven/domain/policy.py"
    # 报告里没有 real，但有别的内容，让 violations() 无事可做
    report = {"src/haven/application/registry.py": 90.0}
    original = floor._measured
    floor._measured = lambda: report  # type: ignore[assignment]
    try:
        assert floor.main() == 1, "缺席的文件是 0% 覆盖，不是'没问题'"
    finally:
        floor._measured = original  # type: ignore[assignment]
    assert real not in report


def test_main_passes_when_every_gated_file_is_in_the_report() -> None:
    """反方向：全都在报告里且达标时必须放行，否则这个门禁没法用。"""
    import scripts.check_coverage_floor as floor

    original = floor._measured
    floor._measured = lambda: {  # type: ignore[assignment]
        path: 100.0 for path in floor.gated_paths_on_disk()
    }
    try:
        assert floor.main() == 0
    finally:
        floor._measured = original  # type: ignore[assignment]
