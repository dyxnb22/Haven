"""Scoped project guidance: root + nested AGENTS.md, bounded and untrusted."""

from pathlib import Path

from haven.adapters.workspace_fs import FsWorkspace
from haven.bootstrap import (
    _MAX_SCOPED_GUIDANCE,
    MAX_GUIDANCE_CHARS,
    _read_guidance,
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
    # Root guidance precedes the scoped one.
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
