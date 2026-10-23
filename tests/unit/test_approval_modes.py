"""分级审批模式（ADR 0033）的单元与行为测试。

覆盖三级模式：
- ask（严格逐笔审查）
- smart（分级智能审批：低/中风险自动放行，高风险弹窗）
- full（工作区全速自主）
同时断言：策略层硬性 DENY（只读模式、越界、.git 保护）在任何审批模式下都绝对不可被绕过。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from haven.application.approvals import AutoApprover
from haven.contracts.events import PolicyDecided
from haven.domain import (
    ApprovalMode,
    PermissionMode,
    PolicyDecision,
    RiskLevel,
    RunStatus,
    ToolFacts,
    evaluate_policy,
)
from haven.interfaces.tui.commands import CommandAction, route_command
from haven.interfaces.tui.presenter import PresenterState
from tests.integration.harness import (
    BUGGY_CALC,
    Harness,
    finish,
    make_repo,
    text,
    tool,
    usage,
)


def test_approval_mode_enum_values() -> None:
    """枚举值必须为稳定的小写标识符。"""
    assert ApprovalMode.ASK.value == "ask"
    assert ApprovalMode.SMART.value == "smart"
    assert ApprovalMode.FULL.value == "full"


def test_policy_layer_unconditionally_denies_regardless_of_approval_mode() -> None:
    """策略层的纯函数不受任何审批模式影响：只读模式下写入一律 DENY，越界一律 DENY。"""
    facts_outside = ToolFacts(tool_name="repo.edit", within_workspace=False)
    outcome_outside = evaluate_policy(PermissionMode.INTERACTIVE, facts_outside)
    assert outcome_outside.decision == PolicyDecision.DENY

    facts_git = ToolFacts(tool_name="repo.edit", within_workspace=True, touches_protected_path=True)
    assert evaluate_policy(PermissionMode.INTERACTIVE, facts_git).decision == PolicyDecision.DENY

    facts_edit = ToolFacts(tool_name="repo.edit", within_workspace=True)
    assert evaluate_policy(PermissionMode.READ_ONLY, facts_edit).decision == PolicyDecision.DENY

    # 高风险工具在策略层标为 HIGH
    facts_del = ToolFacts(tool_name="repo.delete", within_workspace=True)
    assert evaluate_policy(PermissionMode.INTERACTIVE, facts_del).risk == RiskLevel.HIGH

    facts_move = ToolFacts(tool_name="repo.move", within_workspace=True)
    assert evaluate_policy(PermissionMode.INTERACTIVE, facts_move).risk == RiskLevel.HIGH

    facts_patch = ToolFacts(tool_name="repo.apply_patch", within_workspace=True)
    assert evaluate_policy(PermissionMode.INTERACTIVE, facts_patch).risk == RiskLevel.HIGH


@pytest.mark.asyncio
async def test_ask_mode_requires_approval_and_respects_rejection(tmp_path: Path) -> None:
    """在 ask 模式下，任何写入操作都必须经过人工审批；如果审批人拒绝，则操作被拒绝。"""
    repo = make_repo(tmp_path)
    rejecting_approver = AutoApprover(policy="reject_all")
    turns = [
        [
            tool("c1", "repo.read", path="src/calc.py"),
            finish("tool_calls"),
        ],
        [
            tool(
                "c2",
                "repo.edit",
                path="src/calc.py",
                old_string="return a - b  # BUG: should be +",
                new_string="return a + b",
            ),
            finish("tool_calls"),
        ],
        [text("stopped after rejection"), finish("stop"), usage()],
    ]
    h = Harness(
        repo,
        turns,
        approval_mode=ApprovalMode.ASK,
        approver=rejecting_approver,
    )
    outcome = await h.service.run("fix calc")
    assert outcome.stop_reason.value == "final_answer"
    assert len(rejecting_approver.seen) == 1
    assert rejecting_approver.seen[0].tool_name == "repo.edit"
    # 审批人明确拒绝，且文件未被修改
    approval_events = h.sink.events_of("approval.decided")
    assert len(approval_events) == 1
    assert getattr(approval_events[0], "decision", "") == "rejected"
    assert (repo / "src" / "calc.py").read_text() == BUGGY_CALC


@pytest.mark.asyncio
async def test_smart_mode_auto_approves_medium_risk_edits(tmp_path: Path) -> None:
    """在 smart 模式下，repo.edit（中风险）无需人工确认直接自动放行。"""
    repo = make_repo(tmp_path)
    rejecting_approver = AutoApprover(policy="reject_all")
    turns = [
        [
            tool("c1", "repo.read", path="src/calc.py"),
            finish("tool_calls"),
        ],
        [
            tool(
                "c2",
                "repo.edit",
                path="src/calc.py",
                old_string="return a - b  # BUG: should be +",
                new_string="return a + b",
            ),
            finish("tool_calls"),
        ],
        [tool("c3", "repo.diff"), finish("tool_calls")],
        [tool("c4", "repo.check", recipe_id="verify-calc"), finish("tool_calls")],
        [text("Fixed and verified!"), finish("stop"), usage()],
    ]
    h = Harness(
        repo,
        turns,
        approval_mode=ApprovalMode.SMART,
        approver=rejecting_approver,
    )
    outcome = await h.service.run("fix calc")
    # 审批人从未被询问，因为全部在 smart 规则下免审自动放行
    assert len(rejecting_approver.seen) == 0
    assert outcome.status == RunStatus.SUCCEEDED
    assert "return a + b" in (repo / "src" / "calc.py").read_text()


@pytest.mark.asyncio
async def test_smart_mode_still_prompts_for_high_risk_actions(tmp_path: Path) -> None:
    """在 smart 模式下，repo.delete（高风险）必须停下询问人工；reject_all 会阻止该操作。"""
    repo = make_repo(tmp_path)
    rejecting_approver = AutoApprover(policy="reject_all")
    turns = [
        [
            tool("c1", "repo.delete", path="README.md"),
            finish("tool_calls"),
        ],
        [text("cannot delete"), finish("stop"), usage()],
    ]
    h = Harness(
        repo,
        turns,
        approval_mode=ApprovalMode.SMART,
        approver=rejecting_approver,
    )
    await h.service.run("clean up readme")
    # 高风险操作必须询问人工！
    assert len(rejecting_approver.seen) == 1
    assert rejecting_approver.seen[0].tool_name == "repo.delete"
    assert rejecting_approver.seen[0].risk == RiskLevel.HIGH
    # 文件依然存在
    assert (repo / "README.md").exists()


@pytest.mark.asyncio
async def test_full_mode_auto_approves_even_high_risk_mutations(tmp_path: Path) -> None:
    """在 full 模式下，工作区内的所有合法操作（包括 repo.delete）均自动放行。"""
    repo = make_repo(tmp_path)
    rejecting_approver = AutoApprover(policy="reject_all")
    turns = [
        [
            tool("c1", "repo.delete", path="README.md"),
            finish("tool_calls"),
        ],
        [text("Deleted!"), finish("stop"), usage()],
    ]
    h = Harness(
        repo,
        turns,
        approval_mode=ApprovalMode.FULL,
        approver=rejecting_approver,
    )
    await h.service.run("delete readme")
    # full 模式下直接放行，不打扰人工
    assert len(rejecting_approver.seen) == 0
    assert not (repo / "README.md").exists()


@pytest.mark.asyncio
async def test_full_mode_cannot_bypass_read_only_policy(tmp_path: Path) -> None:
    """即使在 full 模式下，若会话处于 read_only 模式，策略层硬性 DENY，写操作仍然失败且被记录。"""
    repo = make_repo(tmp_path)
    turns = [
        [
            tool("c1", "repo.read", path="src/calc.py"),
            finish("tool_calls"),
        ],
        [
            tool(
                "c2",
                "repo.edit",
                path="src/calc.py",
                old_string="return a - b  # BUG: should be +",
                new_string="return a + b",
            ),
            finish("tool_calls"),
        ],
        [text("stopped"), finish("stop"), usage()],
    ]
    h = Harness(
        repo,
        turns,
        mode=PermissionMode.READ_ONLY,
        approval_mode=ApprovalMode.FULL,
    )
    await h.service.run("try writing in read only")
    # 策略层对 c2 明确 DENY
    policy_events = [e for e in h.sink.events_of("policy.decided") if isinstance(e, PolicyDecided)]
    c2_policy = [e for e in policy_events if e.call_id == "c2"]
    assert len(c2_policy) == 1
    assert c2_policy[0].decision == PolicyDecision.DENY.value
    assert c2_policy[0].reason_code == "read_only_mode"
    # 文件未被修改
    assert (repo / "src" / "calc.py").read_text() == BUGGY_CALC


def test_tui_mode_command_routing() -> None:
    """TUI 中的 /mode 命令路由行为。"""
    state = PresenterState(approval_mode="smart")
    # 查看当前模式
    action_query = route_command("/mode", state)
    assert action_query.kind == "log"
    assert "approval mode: smart" in action_query.value

    # 切换到 full
    action_full = route_command("/mode full", state)
    assert action_full == CommandAction("mode", "full")

    # 切换到 ask
    action_ask = route_command("/mode ask", state)
    assert action_ask == CommandAction("mode", "ask")

    # 非法输入
    action_bad = route_command("/mode super_admin", state)
    assert action_bad.kind == "log"
    assert "unknown approval mode 'super_admin'" in action_bad.value
