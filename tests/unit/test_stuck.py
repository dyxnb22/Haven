import inspect

from haven.domain import StuckLoopDetector, call_fingerprint


def test_identical_observations_trigger_stuck() -> None:
    detector = StuckLoopDetector(threshold=3)
    fp = call_fingerprint("repo.search", '{"pattern":"x"}', "result1")
    assert detector.observe(fp) is False
    assert detector.observe(fp) is False
    assert detector.observe(fp) is True


def test_the_run_loop_uses_this_fingerprint_and_not_its_own() -> None:
    """These tests are only worth anything if the loop shares the definition.

    The two drifted once (a dict-vs-list digest built inline in the loop), so
    what was pinned here was not what shipped. Pin the call site itself.
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
    assert detector.observe(fp2) is False  # result changed -> progress
    assert detector.observe(fp2) is False
    assert detector.observe(fp2) is True
