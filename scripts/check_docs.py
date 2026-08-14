"""Check the documentation contracts that should survive the frozen baseline.

Counts already have dedicated generators. This gate covers the structural facts
that otherwise drift silently: local Markdown links, the public command surface,
the package version, and labels that keep historical plans from masquerading as
current documentation.

    uv run python scripts/check_docs.py
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path
from urllib.parse import unquote

from typer.main import get_command

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from haven import __version__  # noqa: E402
from haven.interfaces.cli import app  # noqa: E402
from haven.interfaces.tui.app import HELP_TEXT  # noqa: E402

DOC_PATHS = sorted(
    {
        ROOT / "README.md",
        ROOT / "Haven_TUI_Coding_Agent_项目计划.md",
        *ROOT.glob("docs/**/*.md"),
        *ROOT.glob("course/**/*.md"),
    }
)

HISTORICAL_DOCS = (
    "Haven_TUI_Coding_Agent_项目计划.md",
    "docs/superpowers/specs/2026-08-12-deepseek-harness-design.md",
    "docs/superpowers/specs/2026-08-12-long-horizon-design.md",
    "docs/superpowers/specs/2026-08-12-repo-exec-sandbox-design.md",
    "docs/superpowers/plans/2026-08-12-repo-exec-sandbox.md",
    "docs/superpowers/plans/2026-08-14-measure-what-was-built.md",
)

HISTORICAL_MARKER = "Historical record"
CLI_BEGIN = "<!-- BEGIN CLI COMMAND SURFACE -->"
CLI_END = "<!-- END CLI COMMAND SURFACE -->"


def _local_link_targets(path: Path, text: str) -> list[tuple[str, Path]]:
    """Return local Markdown link targets and their resolved filesystem paths."""
    targets: list[tuple[str, Path]] = []
    prose = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    prose = re.sub(r"`[^`\n]+`", "", prose)
    for match in re.finditer(r"!?\[[^\]]*\]\(([^)]+)\)", prose):
        raw = match.group(1).strip()
        if raw.startswith("<") and raw.endswith(">"):
            raw = raw[1:-1]
        # A Markdown title follows whitespace. Local paths in this repository
        # contain no unescaped spaces, so the first token is the target.
        target = raw.split(maxsplit=1)[0]
        if not target or target.startswith(("#", "http://", "https://", "mailto:")):
            continue
        target = unquote(target.split("#", 1)[0])
        if target:
            targets.append((target, (path.parent / target).resolve()))
    return targets


def _between(text: str, begin: str, end: str) -> str | None:
    start = text.find(begin)
    finish = text.find(end)
    if start < 0 or finish < 0 or finish <= start:
        return None
    return text[start + len(begin) : finish]


def collect_problems() -> list[str]:
    problems: list[str] = []

    for path in DOC_PATHS:
        text = path.read_text(encoding="utf-8")
        for raw, resolved in _local_link_targets(path, text):
            if not resolved.exists():
                rel = path.relative_to(ROOT).as_posix()
                problems.append(f"{rel}: broken local link {raw!r}")

    for rel in HISTORICAL_DOCS:
        path = ROOT / rel
        opening = "\n".join(path.read_text(encoding="utf-8").splitlines()[:16])
        if HISTORICAL_MARKER not in opening:
            problems.append(f"{rel}: missing an opening {HISTORICAL_MARKER!r} notice")

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project_version = str(project["project"]["version"])
    if project_version != __version__:
        problems.append(
            "version mismatch: "
            f"pyproject.toml={project_version!r}, haven.__version__={__version__!r}"
        )

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    command_block = _between(readme, CLI_BEGIN, CLI_END)
    if command_block is None:
        problems.append("README.md: missing CLI command-surface markers")
    else:
        click_app = get_command(app)
        root_commands = set(click_app.commands)
        documented = set(
            re.findall(r"^haven\s+([a-z][a-z-]*)(?:\s|$)", command_block, re.MULTILINE)
        )
        missing = sorted(root_commands - documented)
        extra = sorted(documented - root_commands)
        if missing:
            problems.append("README.md: undocumented root CLI command(s): " + ", ".join(missing))
        if extra:
            problems.append("README.md: unknown root CLI command(s): " + ", ".join(extra))

    tui_commands = set(re.findall(r"^\s+(\/[^\s]+)", HELP_TEXT, re.MULTILINE))
    missing_tui = sorted(command for command in tui_commands if f"`{command}`" not in readme)
    if missing_tui:
        problems.append("README.md: undocumented TUI command(s): " + ", ".join(missing_tui))

    return problems


def main() -> int:
    problems = collect_problems()
    if problems:
        print(f"documentation contracts: {len(problems)} problem(s)")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print(
        f"documentation contracts: {len(DOC_PATHS)} Markdown files, links, commands, "
        "history labels, and version agree"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
