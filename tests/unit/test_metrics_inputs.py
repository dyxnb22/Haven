"""指标刷新使用指定测量输入，保留历史成绩时不访问受限目录。"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import refresh_metrics as metrics


@pytest.mark.parametrize(
    ("language", "old_label", "new_label"),
    [
        ("en", "Live real-repo suite (model)", "Historical live real-repo suite (model)"),
        ("zh", "实时真实仓库套件（model）", "历史实时真实仓库套件（model）"),
    ],
)
def test_explicit_inputs_and_preserved_history_skip_restricted_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    language: str,
    old_label: str,
    new_label: str,
) -> None:
    target = tmp_path / "README.md"
    original = (
        "# Project\n\n"
        + metrics.BEGIN
        + f"\n\n| {old_label} | 7/9 (historical) |\n\n"
        + metrics.END
        + "\n\nKeep this paragraph.\n"
    )
    target.write_text(original)
    report = tmp_path / "new-offline.json"
    report.write_text(json.dumps({"passed": 40, "total": 40, "security_violations": 0}))
    coverage_data = tmp_path / "new-coverage"
    coverage_data.touch()
    monkeypatch.setattr(metrics, "ROOT", tmp_path)
    monkeypatch.setattr(metrics, "TARGETS", {"README.md": language})
    monkeypatch.setattr(metrics, "_count_tests", lambda: 8)

    def coverage(path: Path | None) -> int:
        assert path == coverage_data
        return 90

    monkeypatch.setattr(metrics, "_coverage_pct", coverage)
    forbidden = {tmp_path / "eval_report/report.json", tmp_path / "evals/real/results.json"}
    read_text, is_file = Path.read_text, Path.is_file

    def guarded_read(path: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        assert path not in forbidden
        return read_text(path, *args, **kwargs)

    def guarded_exists(path: Path) -> bool:
        assert path not in forbidden
        return is_file(path)

    monkeypatch.setattr(Path, "read_text", guarded_read)
    monkeypatch.setattr(Path, "is_file", guarded_exists)
    argv = [
        "refresh_metrics.py",
        "--coverage-data",
        str(coverage_data),
        "--offline-report",
        str(report),
        "--preserve-historical-live",
    ]
    monkeypatch.setattr(sys, "argv", argv)
    assert metrics.main() == 0
    updated = target.read_text()
    assert f"| {new_label} | 7/9 (historical) |" in updated
    assert "90%" in updated and "40/40" in updated
    assert updated.endswith("Keep this paragraph.\n")
    monkeypatch.setattr(sys, "argv", [*argv, "--check"])
    assert metrics.main() == 0
    assert target.read_text() == updated
    report.write_text(json.dumps({"passed": 39, "total": 40, "security_violations": 0}))
    assert metrics.main() == 1
    assert target.read_text() == updated


def test_invalid_explicit_report_cannot_silently_fall_back(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="could not read offline report"):
        metrics._eval_summary(tmp_path / "missing.json")
    report = tmp_path / "invalid.json"
    report.write_text('{"passed": 100, "total": 2, "security_violations": 0}')
    with pytest.raises(SystemExit, match="invalid offline report"):
        metrics._eval_summary(report)


def test_coverage_override_is_used_for_both_freshness_and_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "fresh-coverage"
    data.touch()
    monkeypatch.setattr(metrics, "ROOT", tmp_path)
    monkeypatch.setattr(metrics, "_src_files", lambda: [])
    monkeypatch.setattr(metrics, "_test_files", lambda: [])

    def run(argv, **kwargs):  # type: ignore[no-untyped-def]
        assert argv[argv.index("--data-file") + 1] == str(data)
        return subprocess.CompletedProcess(argv, 0, stdout="TOTAL 10 1 90%\n", stderr="")

    monkeypatch.setattr(metrics.subprocess, "run", run)
    assert metrics._coverage_pct(data) == 90
