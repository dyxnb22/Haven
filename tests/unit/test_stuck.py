import inspect

import pytest

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

    调用点是 `RunActor.handle_tool_calls`：`RunService._drive` 总是先创建并注册
    Actor，再委托 `actor.drive()`，所以 Actor 的循环是唯一真正执行的循环。
    """
    from haven.application.run_actor import RunActor

    source = inspect.getsource(RunActor.handle_tool_calls)
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


def test_invalid_threshold_raises() -> None:
    with pytest.raises(ValueError, match="threshold must be at least 1"):
        StuckLoopDetector(threshold=0)
    with pytest.raises(ValueError, match="threshold must be at least 1"):
        StuckLoopDetector(threshold=-1)


def test_the_fingerprint_binds_the_tool_and_its_arguments() -> None:
    """指纹必须绑定全部三个输入，否则检测器会误判。

    此前它实际上只按 result 区分：把 `tool` 或 `args` 从摘要里删掉，1581 条测试
    依然全绿（变异测试实测）。那样一来，两个不同的调用只要返回相同文本就会被
    当成同一次观测——`repo.read a.py` 连着 `repo.read b.py` 读到同样的内容，
    运行会被判成卡死而提前停止。
    """
    base = call_fingerprint("repo.read", '{"path":"a"}', "same text")
    assert call_fingerprint("repo.edit", '{"path":"a"}', "same text") != base, (
        "fingerprint does not bind the tool"
    )
    assert call_fingerprint("repo.read", '{"path":"b"}', "same text") != base, (
        "fingerprint does not bind the arguments"
    )
    assert call_fingerprint("repo.read", '{"path":"a"}', "other text") != base

    # 因此两次不同调用不会被误判成循环。
    detector = StuckLoopDetector(threshold=3)
    assert [
        detector.observe(fp)
        for fp in (base, call_fingerprint("repo.read", '{"path":"b"}', "same text"))
    ] == [False, False]
