from haven.domain import StuckLoopDetector, call_fingerprint


def test_identical_observations_trigger_stuck() -> None:
    detector = StuckLoopDetector(threshold=3)
    fp = call_fingerprint("repo.search", '{"pattern":"x"}', "result1")
    assert detector.observe(fp) is False
    assert detector.observe(fp) is False
    assert detector.observe(fp) is True


def test_different_results_reset_counter() -> None:
    detector = StuckLoopDetector(threshold=3)
    fp1 = call_fingerprint("repo.search", '{"pattern":"x"}', "result1")
    fp2 = call_fingerprint("repo.search", '{"pattern":"x"}', "result2")
    assert detector.observe(fp1) is False
    assert detector.observe(fp1) is False
    assert detector.observe(fp2) is False  # result changed -> progress
    assert detector.observe(fp2) is False
    assert detector.observe(fp2) is True
