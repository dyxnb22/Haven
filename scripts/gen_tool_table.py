"""根据策略本身生成 ARCHITECTURE.md 中的工具/策略表。

该表回答整个项目存在所要回答的问题——“为什么允许这个精确的副作用？”——因此
与 `evaluate_policy` 漂移的手抄副本还不如没有表。下面的每个决定都通过调用真实
策略函数生成；只有约束列是文字，并且与生成器放在一起，同时有完整性检查，避免
工具加入代码后没有文档。

    uv run python scripts/gen_tool_table.py            # 重写区块
    uv run python scripts/gen_tool_table.py --check    # 漂移时失败
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from haven.domain.enums import PermissionMode  # noqa: E402
from haven.domain.exec_policy import ExecClass  # noqa: E402
from haven.domain.policy import (  # noqa: E402
    EFFECT_TOOLS,
    EXEC_TOOLS,
    KNOWN_TOOLS,
    READ_ONLY_TOOLS,
    STATE_TOOLS,
    ToolFacts,
    evaluate_policy,
)

TARGET = ROOT / "docs" / "ARCHITECTURE.md"
BEGIN = "<!-- BEGIN GENERATED TOOL TABLE (scripts/gen_tool_table.py; do not edit by hand) -->"
END = "<!-- END GENERATED TOOL TABLE -->"

#: 代码无法推导的唯一列：为什么提供每个工具本身是安全的。
#: 每项只保留审查者需要的约束，不要写成普通描述。
CONSTRAINTS: dict[str, str] = {
    "repo.list": "workspace-confined, entry cap",
    "repo.search": (
        "ripgrep when available (honours `.gitignore`), Python fallback; "
        "result, line, and byte caps"
    ),
    "repo.read": (
        "regular UTF-8 files, line and byte caps; records the digest that later binds an edit"
    ),
    "repo.diff": "shows only what *this run* changed, including created files",
    "repo.edit": (
        "existing files only, preimage-bound, unique match unless `occurrence` or "
        "`replace_all` is set"
    ),
    "repo.create": "new paths only — fails on anything that exists, so it can never blank a file",
    "repo.delete": "existing files only, content pinned at approval so a concurrent change fails",
    "repo.move": "rename/move; fails if the destination exists, so it never silently overwrites",
    "repo.apply_patch": (
        "multi-file transaction: simulated first, one approval binds every file's preimage, "
        "applied atomically with journaled rollback (ADR 0019)"
    ),
    "repo.check": (
        "registered recipe ids only, fixed argv, scrubbed env, timeout, bounded output; "
        "workspace-writable and user config may opt into network"
    ),
    "repo.exec": (
        "argv array only (no shell string), OS sandbox with the workspace read-only, no network, "
        "`$HOME` unreadable; output is never evidence"
    ),
    "task.plan": "touches only run state; no path, no external effect",
}

#: 工具所属的集合，用于 class 列。
_CLASSES = (
    ("read-only", READ_ONLY_TOOLS),
    ("effect", EFFECT_TOOLS),
    ("exec", EXEC_TOOLS),
    ("state", STATE_TOOLS),
)


@dataclass(frozen=True, slots=True)
class Row:
    """生成的工具/策略表中的一行。"""

    #: 工具注册名称。
    tool: str
    #: 工具所属的副作用类别。
    tool_class: str
    #: 交互模式下的策略决定。
    interactive: str
    #: 只读模式下的策略决定。
    read_only: str
    #: 需要审查者关注的核心约束。
    constraints: str


def _class_of(tool: str) -> str:
    for name, members in _CLASSES:
        if tool in members:
            return name
    return "unclassified"


def _decide(tool: str, mode: PermissionMode) -> str:
    """真实策略对一个有代表性、格式正确的提议所作的决定。

    事实采用良性情况（位于工作区内、不触碰受保护路径、已注册配方且存在沙箱），
    因此表格报告的是每个工具的*基线*摩擦，而不是任何工具都可能获得的硬拒绝。
    """
    facts = ToolFacts(
        tool_name=tool,
        within_workspace=True,
        touches_protected_path=False,
        recipe_registered=True,
        sandbox_available=True,
        exec_class=ExecClass.OTHER.value,
    )
    return str(evaluate_policy(mode, facts).decision.value)


def rows_for_tools(tools: set[str] | None = None) -> list[Row]:
    """每个工具一行，决定由真实策略计算。"""
    selected = sorted(KNOWN_TOOLS if tools is None else tools)
    missing = [tool for tool in selected if tool not in CONSTRAINTS]
    if missing:
        raise SystemExit(
            "every tool needs a constraints entry in scripts/gen_tool_table.py; missing: "
            + ", ".join(missing)
        )
    return [
        Row(
            tool=tool,
            tool_class=_class_of(tool),
            interactive=_decide(tool, PermissionMode.INTERACTIVE),
            read_only=_decide(tool, PermissionMode.READ_ONLY),
            constraints=CONSTRAINTS[tool],
        )
        for tool in selected
    ]


def render_table() -> str:
    """将当前策略计算结果渲染为 Markdown 表格。"""
    header = "| Tool | Class | Interactive | Read-only | Key constraints |\n|---|---|---|---|---|\n"
    body = "".join(
        f"| `{row.tool}` | {row.tool_class} | {row.interactive} | "
        f"{row.read_only} | {row.constraints} |\n"
        for row in rows_for_tools()
    )
    return header + body


def _block() -> str:
    note = (
        "\n*Decisions are computed by calling `evaluate_policy` itself, for a benign, "
        "well-formed proposal; a hard deny (outside the workspace, a protected path, no "
        "sandbox) overrides them in every mode.*\n"
    )
    return f"{BEGIN}\n\n{render_table()}{note}\n{END}"


def main(argv: list[str] | None = None) -> int:
    """生成或校验架构文档中的工具策略表，并返回退出码。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the block is stale")
    args = parser.parse_args(argv)

    text = TARGET.read_text(encoding="utf-8")
    pattern = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.DOTALL)
    if not pattern.search(text):
        raise SystemExit(f"{TARGET} has no generated tool-table block; add the marker comments")
    updated = pattern.sub(lambda _: _block(), text)

    if args.check:
        if updated != text:
            print(f"{TARGET} tool table is stale; run `uv run python {Path(__file__).name}`")
            return 1
        print("tool table matches the policy code")
        return 0
    if updated != text:
        TARGET.write_text(updated, encoding="utf-8")
        print(f"updated {TARGET.relative_to(ROOT)}")
    else:
        print("tool table already current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
