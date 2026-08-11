"""Structural checks on the Mermaid diagrams in the docs.

Broken diagrams render as a raw error box on GitHub, which is exactly where a
portfolio reader sees them first. These checks are not a full Mermaid parser;
they catch the mistakes that actually happen: unbalanced fences, unbalanced
brackets/quotes in node labels, and unknown diagram types.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOC_FILES = sorted(REPO_ROOT.glob("docs/**/*.md")) + [REPO_ROOT / "README.md"]

KNOWN_HEADERS = ("flowchart", "graph", "stateDiagram-v2", "sequenceDiagram", "classDiagram")


def mermaid_blocks() -> list[tuple[Path, str]]:
    blocks: list[tuple[Path, str]] = []
    for path in DOC_FILES:
        if not path.is_file():
            continue
        for match in re.finditer(r"```mermaid\n(.*?)```", path.read_text("utf-8"), re.DOTALL):
            blocks.append((path, match.group(1)))
    return blocks


def test_docs_contain_the_three_required_diagrams() -> None:
    architecture = (REPO_ROOT / "docs" / "ARCHITECTURE.md").read_text("utf-8")
    assert architecture.count("```mermaid") >= 3, "layering, channel, and state machine"
    assert "stateDiagram-v2" in architecture
    assert "flowchart" in architecture


@pytest.mark.parametrize("path,block", mermaid_blocks(), ids=lambda v: getattr(v, "name", ""))
def test_mermaid_block_is_structurally_sound(path: Path, block: str) -> None:
    lines = [line for line in block.splitlines() if line.strip()]
    assert lines, f"{path.name}: empty mermaid block"

    header = lines[0].strip()
    assert header.startswith(KNOWN_HEADERS), f"{path.name}: unknown diagram type {header!r}"

    for number, line in enumerate(lines, start=1):
        assert line.count('"') % 2 == 0, f"{path.name}:{number} unbalanced quote: {line!r}"
        # brackets inside quoted labels are text, so only check outside quotes
        outside = re.sub(r'"[^"]*"', "", line)
        for opener, closer in (("[", "]"), ("(", ")"), ("{", "}")):
            assert outside.count(opener) == outside.count(closer), (
                f"{path.name}:{number} unbalanced {opener}{closer}: {line!r}"
            )


@pytest.mark.parametrize("path,block", mermaid_blocks(), ids=lambda v: getattr(v, "name", ""))
def test_mermaid_subgraphs_are_closed(path: Path, block: str) -> None:
    opened = len(re.findall(r"^\s*subgraph\b", block, re.MULTILINE))
    closed = len(re.findall(r"^\s*end\s*$", block, re.MULTILINE))
    # stateDiagram notes also close with `end note`, which the regex above skips
    assert closed >= opened, f"{path.name}: {opened} subgraph(s) but only {closed} end(s)"


def test_state_diagram_transition_labels_are_single_line() -> None:
    """`<br/>` in stateDiagram transition labels renders inconsistently."""
    for path, block in mermaid_blocks():
        if not block.lstrip().startswith("stateDiagram"):
            continue
        for line in block.splitlines():
            if "-->" in line and ":" in line:
                label = line.split(":", 1)[1]
                assert "<br" not in label, f"{path.name}: multi-line transition label: {line!r}"
