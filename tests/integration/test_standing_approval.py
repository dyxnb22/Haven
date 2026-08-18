"""运行作用域内对相同 repo.check 调用的持续审批（ADR 0025）。

一次审批覆盖同一运行中逐字节相同的重新检查——这正是修复/验证循环的常见形态。
其他情况仍然会再次询问：不同配方、写入工具以及下一次运行。
"""

from pathlib import Path

from haven.application.approvals import ApprovalResponder
from haven.domain.approval import ApprovalRequest
from haven.domain.enums import ApprovalDecision, RunStatus
from tests.integration.harness import Harness, finish, make_repo, text, tool


class CountingApprover(ApprovalResponder):
    """批准（或拒绝）所有内容，并记录曾被询问的内容。"""

    def __init__(self, decision: ApprovalDecision = ApprovalDecision.APPROVED) -> None:
        self._decision = decision
        self.asked: list[str] = []

    async def respond(self, request: ApprovalRequest) -> ApprovalDecision:
        self.asked.append(request.summary)
        return self._decision


async def test_identical_check_reruns_ask_only_once(tmp_path: Path) -> None:
    # 检查之间的 repo.diff 模拟真实的修复/验证循环，也是确定性所需的：
    # 三次连续、调用和结果都相同的调用正是 no-progress 条件，因此没有
    # 中间调用时，卡住检测器（阈值 3）会停止运行。此前它之所以通过，
    # 只是因为检查结果带有 duration_ms，而毫秒数通常不同；这种抖动就是
    # 2026-08-13 记录的 1/8 概率 flaky。插入调用会重置计数器，因此现在
    # 测试的是持续授权，而不是时钟。
    repo = make_repo(tmp_path)
    turns = [
        [tool("c1", "repo.check", recipe_id="always-pass"), finish("tool_calls")],
        [tool("d1", "repo.diff"), finish("tool_calls")],
        [tool("c2", "repo.check", recipe_id="always-pass"), finish("tool_calls")],
        [tool("d2", "repo.diff"), finish("tool_calls")],
        [tool("c3", "repo.check", recipe_id="always-pass"), finish("tool_calls")],
        [text("Ran the check three times."), finish()],
    ]
    approver = CountingApprover()
    h = Harness(repo, turns, approver=approver)
    outcome = await h.service.run("run the check repeatedly")

    assert outcome.status is RunStatus.SUCCEEDED, outcome
    check_asks = [s for s in approver.asked if "check recipe" in s]
    assert len(check_asks) == 1, f"identical re-runs must not re-ask; asked: {approver.asked}"
    # 卡片在用户同意时披露持续授权的范围。
    assert "identical re-runs" in check_asks[0]

    # 日志仍为每次执行保留一条审批记录……
    decided = h.sink.events_of("approval.decided")
    assert len(decided) == 3, f"approval.decided events: {decided}"
    # ……并且每次跳过询问时都会宣布持续授权。
    all_notices = [getattr(e, "message", "") for e in h.sink.events_of("notice")]
    notices = [m for m in all_notices if "standing approval" in m]
    assert len(notices) == 2, f"notices seen: {all_notices}"


async def test_a_different_recipe_asks_again(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    turns = [
        [tool("c1", "repo.check", recipe_id="always-pass"), finish("tool_calls")],
        [tool("c2", "repo.check", recipe_id="verify-calc"), finish("tool_calls")],
        [text("Two different checks."), finish()],
    ]
    approver = CountingApprover()
    h = Harness(repo, turns, approver=approver)
    await h.service.run("run two different checks")

    check_asks = [s for s in approver.asked if "check recipe" in s]
    assert len(check_asks) == 2, "a different recipe is a different consent"


async def test_rejection_never_arms_the_grant(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    turns = [
        [tool("c1", "repo.check", recipe_id="always-pass"), finish("tool_calls")],
        [tool("c2", "repo.check", recipe_id="always-pass"), finish("tool_calls")],
        [text("Tried twice."), finish()],
    ]
    approver = CountingApprover(ApprovalDecision.REJECTED)
    h = Harness(repo, turns, approver=approver)
    await h.service.run("try the check")

    check_asks = [s for s in approver.asked if "check recipe" in s]
    assert len(check_asks) == 2, "a rejected check must ask again, not inherit a grant"
    assert not h.sink.events_of("execution.started"), "nothing was ever authorized"


async def test_write_tools_always_re_ask(tmp_path: Path) -> None:
    """对同一文件进行两次形状相同的编辑仍然会询问两次：持续授权只适用于
    repo.check，绝不适用于任何写入操作。"""
    repo = make_repo(tmp_path)
    turns = [
        [tool("c1", "repo.read", path="src/calc.py"), finish("tool_calls")],
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
        [tool("c3", "repo.read", path="src/calc.py"), finish("tool_calls")],
        [
            tool(
                "c4",
                "repo.edit",
                path="src/calc.py",
                old_string="return a + b",
                new_string="return a + b  # verified",
            ),
            finish("tool_calls"),
        ],
        [tool("c5", "repo.diff"), finish("tool_calls")],
        [tool("c6", "repo.check", recipe_id="verify-calc"), finish("tool_calls")],
        [text("Edited twice."), finish()],
    ]
    approver = CountingApprover()
    h = Harness(repo, turns, approver=approver)
    outcome = await h.service.run("fix and annotate add()")

    assert outcome.status is RunStatus.SUCCEEDED
    edit_asks = [s for s in approver.asked if s.startswith("edit ")]
    assert len(edit_asks) == 2
