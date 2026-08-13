import inspect

from haven.domain import StuckLoopDetector, call_fingerprint


def test_identical_observations_escalate_from_nudge_to_stuck() -> None:
    """Repetition is warned about before it is fatal: the second identical
    observation earns a nudge, the third stops the run."""
    detector = StuckLoopDetector(threshold=3)
    fp = call_fingerprint("repo.search", '{"pattern":"x"}', "result1")
    assert detector.observe(fp) == "ok"
    assert detector.observe(fp) == "nudge"
    assert detector.observe(fp) == "stuck"


def test_the_nudge_fires_once_per_episode() -> None:
    """A second nudge would just spend context restating the first."""
    detector = StuckLoopDetector(threshold=4)
    fp = call_fingerprint("repo.search", '{"pattern":"x"}', "result1")
    verdicts = [detector.observe(fp) for _ in range(4)]
    assert verdicts == ["ok", "nudge", "ok", "stuck"]


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
    assert detector.observe(fp1) == "ok"
    assert detector.observe(fp1) == "nudge"
    assert detector.observe(fp2) == "ok"  # result changed -> progress
    assert detector.observe(fp2) == "nudge"
    assert detector.observe(fp2) == "stuck"


def test_a_new_episode_can_be_nudged_again() -> None:
    """Progress resets the counter, so a later repetition is a fresh episode
    and deserves its own warning rather than silence."""
    detector = StuckLoopDetector(threshold=3)
    fp1 = call_fingerprint("repo.read", '{"path":"a"}', "same")
    fp2 = call_fingerprint("repo.read", '{"path":"b"}', "other")
    assert [detector.observe(fp1), detector.observe(fp1)] == ["ok", "nudge"]
    assert detector.observe(fp2) == "ok"
    assert [detector.observe(fp1), detector.observe(fp1)] == ["ok", "nudge"]
