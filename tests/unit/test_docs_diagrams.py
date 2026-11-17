"""文档中 Mermaid 图的结构检查。

损坏的图在 GitHub 上会渲染为原始错误框，而作品集读者恰恰会先在那里看到它们。
这些检查不是完整的 Mermaid 解析器；它们捕获实际会发生的错误：围栏不平衡、节点
标签中的括号/引号不平衡，以及未知图类型。
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
        # 引号内标签中的括号属于文本，因此只检查引号外的部分
        outside = re.sub(r'"[^"]*"', "", line)
        for opener, closer in (("[", "]"), ("(", ")"), ("{", "}")):
            assert outside.count(opener) == outside.count(closer), (
                f"{path.name}:{number} unbalanced {opener}{closer}: {line!r}"
            )


@pytest.mark.parametrize("path,block", mermaid_blocks(), ids=lambda v: getattr(v, "name", ""))
def test_mermaid_subgraphs_are_closed(path: Path, block: str) -> None:
    opened = len(re.findall(r"^\s*subgraph\b", block, re.MULTILINE))
    closed = len(re.findall(r"^\s*end\s*$", block, re.MULTILINE))
    # stateDiagram 注释也用 `end note` 结束，上面的正则会跳过它
    assert closed >= opened, f"{path.name}: {opened} subgraph(s) but only {closed} end(s)"


def test_state_diagram_transition_labels_are_single_line() -> None:
    """stateDiagram 转换标签中的 `<br/>` 会产生不一致的渲染结果。"""
    for path, block in mermaid_blocks():
        if not block.lstrip().startswith("stateDiagram"):
            continue
        for line in block.splitlines():
            if "-->" in line and ":" in line:
                label = line.split(":", 1)[1]
                assert "<br" not in label, f"{path.name}: multi-line transition label: {line!r}"
