"""有作用域的项目指导：根目录 + 嵌套 AGENTS.md，有界且不可信。"""

import os
from pathlib import Path

import pytest

from haven.adapters.workspace_fs import FsWorkspace
from haven.bootstrap import (
    _MAX_SCOPED_GUIDANCE,
    MAX_GUIDANCE_CHARS,
    _read_guidance,
    _read_guidance_snapshot,
)


def ws(tmp_path: Path) -> FsWorkspace:
    (tmp_path / "src").mkdir()
    return FsWorkspace(tmp_path)


async def test_root_agents_md_is_read(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("prefer tabs")
    guidance = await _read_guidance(ws(tmp_path))
    assert "prefer tabs" in guidance
    assert "repository root" in guidance


async def test_claude_md_is_merged_for_compatibility(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("agents rule")
    (tmp_path / "CLAUDE.md").write_text("claude rule")
    guidance = await _read_guidance(ws(tmp_path))
    assert "agents rule" in guidance
    assert "claude rule" in guidance


async def test_nested_agents_md_is_scoped_and_labelled(tmp_path: Path) -> None:
    workspace = ws(tmp_path)
    (tmp_path / "AGENTS.md").write_text("root rule")
    (tmp_path / "src" / "AGENTS.md").write_text("src-specific rule")
    guidance = await _read_guidance(workspace)
    assert "root rule" in guidance
    assert "src-specific rule" in guidance
    assert "src/AGENTS.md" in guidance
    # 根目录指导位于作用域指导之前。
    assert guidance.index("root rule") < guidance.index("src-specific rule")


async def test_scoped_guidance_is_bounded_in_count(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("root")
    for i in range(_MAX_SCOPED_GUIDANCE + 4):
        sub = tmp_path / f"pkg{i}"
        sub.mkdir()
        (sub / "AGENTS.md").write_text(f"rule {i}")
    guidance = await _read_guidance(ws(tmp_path))
    scoped_count = guidance.count("scoped to")
    assert scoped_count == _MAX_SCOPED_GUIDANCE


async def test_noise_directories_are_skipped(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("root")
    for noise in (".git", "node_modules", ".venv"):
        d = tmp_path / noise
        d.mkdir()
        (d / "AGENTS.md").write_text("should not appear")
    guidance = await _read_guidance(ws(tmp_path))
    assert "should not appear" not in guidance


async def test_total_is_capped(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("r" * 100_000)
    guidance = await _read_guidance(ws(tmp_path))
    assert len(guidance) <= MAX_GUIDANCE_CHARS


async def test_no_guidance_is_empty(tmp_path: Path) -> None:
    assert await _read_guidance(ws(tmp_path)) == ""


async def test_snapshot_matches_context_text_and_reports_global_truncation(tmp_path: Path) -> None:
    workspace = ws(tmp_path)
    (tmp_path / "AGENTS.md").write_text("root " * 1000)
    (tmp_path / "CLAUDE.md").write_text("compat " * 1000)
    (tmp_path / "src/AGENTS.md").write_text("scoped instructions")
    snapshot = await _read_guidance_snapshot(workspace)
    assert snapshot.text == await _read_guidance(workspace)
    assert len(snapshot.text) == MAX_GUIDANCE_CHARS
    sources = {source.path: source for source in snapshot.sources}
    assert sources["AGENTS.md"].status == "loaded"
    assert sources["AGENTS.md"].truncated
    assert sources["CLAUDE.md"].truncated
    assert sources["src/AGENTS.md"].scope == "src"
    assert sources["src/AGENTS.md"].status == "omitted"
    assert sources["src/AGENTS.md"].reason == "total character budget"
    assert "scoped instructions" not in snapshot.text


async def test_snapshot_reports_line_limits_empty_files_and_blocked_sources(tmp_path: Path) -> None:
    workspace = ws(tmp_path)
    (tmp_path / "AGENTS.md").write_text("rule\n" * 250)
    (tmp_path / "src/AGENTS.md").write_text(" \n ")
    outside = tmp_path.parent / "outside-guidance.txt"
    outside.write_text("outside content must not enter guidance")
    (tmp_path / "CLAUDE.md").symlink_to(outside)
    snapshot = await _read_guidance_snapshot(workspace)
    sources = {source.path: source for source in snapshot.sources}
    assert sources["AGENTS.md"].truncated
    assert sources["AGENTS.md"].reason == "file character/line limit"
    assert sources["src/AGENTS.md"].status == "empty"
    assert sources["CLAUDE.md"].status == "unavailable"
    assert sources["CLAUDE.md"].reason
    assert "outside content" not in snapshot.text


async def test_guidance_walk_does_not_enter_evaluation_or_generated_trees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = ws(tmp_path)
    excluded = {"evals/real", "evals/headtohead", "eval_report"}
    for relative in excluded:
        path = tmp_path / relative
        path.mkdir(parents=True)
        (path / "AGENTS.md").write_text("excluded guidance")
    (tmp_path / "src/AGENTS.md").write_text("source guidance")
    walk = os.walk

    def guarded_walk(root: Path):
        for directory, names, files in walk(root):
            relative = Path(directory).relative_to(tmp_path).as_posix()
            assert not any(relative == path or relative.startswith(path + "/") for path in excluded)
            yield directory, names, files

    monkeypatch.setattr(os, "walk", guarded_walk)
    snapshot = await _read_guidance_snapshot(workspace)
    assert "source guidance" in snapshot.text
    assert "excluded guidance" not in snapshot.text
