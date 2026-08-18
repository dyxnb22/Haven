import inspect

from haven.domain import StuckLoopDetector, call_fingerprint


def test_identical_observations_trigger_stuck() -> None:
    detector = StuckLoopDetector(threshold=3)
    fp = call_fingerprint("repo.search", '{"pattern":"x"}', "result1")
    assert detector.observe(fp) is False
    assert detector.observe(fp) is False
    assert detector.observe(fp) is True


def test_the_run_loop_uses_this_fingerprint_and_not_its_own() -> None:
    """只有循环共享该定义时，这些测试才有价值。

    二者曾经发生过漂移（循环内联构造了 dict-vs-list 摘要），因此这里固定的行为
    并不是实际发布的行为。现在直接固定调用点本身。
    """
    from haven.application.run_service import RunService

    source = inspect.getsource(RunService._handle_tool_calls)  # noqa: SLF001
    assert "call_fingerprint(" in source
    assert "digest_of(" not in source, "the loop must not compose its own fingerprint"


def test_different_results_reset_counter() -> None:
    detector = StuckLoopDetector(threshold=3)
    fp1 = call_fingerprint("repo.search", '{"pattern":"x"}', "result1")
    fp2 = call_fingerprint("repo.search", '{"pattern":"x"}', "result2")
    assert detector.observe(fp1) is False
    assert detector.observe(fp1) is False
    assert detector.observe(fp2) is False  # 结果发生变化 -> 有进展
    assert detector.observe(fp2) is False
    assert detector.observe(fp2) is True


def test_detection_requires_adjacency() -> None:
    """固定该检测器的实测边界，避免将其误认为更宽泛的检测：交替出现 A、B、A 永远
    不会触发它。42 次实时运行的追踪研究发现，不收敛表现为*多样的*无效工作，而
    不是重复，因此建立在此之上的警告等级被移除（`docs/notes/rejected/0002`）。"""
    detector = StuckLoopDetector(threshold=3)
    a = call_fingerprint("repo.read", '{"path":"a"}', "A")
    b = call_fingerprint("repo.read", '{"path":"b"}', "B")
    assert [detector.observe(fp) for fp in (a, b, a, b, a, b)] == [False] * 6
