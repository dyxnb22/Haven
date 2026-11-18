"""子 Agent 必须真实调用模型，且无法写入、越界或绕过父预算。"""

from pathlib import Path

import pytest

from haven.domain.enums import RunStatus
from tests.integration.harness import Harness, finish, make_repo, text, tool


@pytest.mark.parametrize("role", ["scout", "reviewer"])
async def test_child_has_its_own_model_context_and_parent_accounting(
    tmp_path: Path, role: str
) -> None:
    h = Harness(
        make_repo(tmp_path),
        [
            [
                tool(
                    "delegate",
                    "agent.delegate",
                    role=role,
                    task="Find the arithmetic defect",
                    file_scope=["src/calc.py"],
                ),
                finish("tool_calls"),
            ],
            [tool("read", "repo.read", path="src/calc.py"), finish("tool_calls")],
            [text("src/calc.py:2 subtracts b; add() must use a + b."), finish()],
            [text("The investigation found subtraction on line 2."), finish()],
        ],
    )
    outcome = await h.service.run("Investigate add() without changing it")
    assert outcome.status is RunStatus.SUCCEEDED
    assert len(h.model.requests_seen) == 4
    child_request = h.model.requests_seen[1]
    assert not any(message.tool_calls for message in child_request.messages)
    assert "agent.delegate" not in {t.name for t in child_request.tools}
    assert "repo.edit" not in {t.name for t in child_request.tools}
    child_started = h.sink.events_of("child.started")[0]
    child = await h.store.load_checkpoint(child_started.child_run_id)
    parent = await h.store.load_checkpoint(outcome.run_id)
    assert child is not None and parent is not None
    assert child.parent_run_id == outcome.run_id
    assert child.file_scope == ("src/calc.py",)
    assert child.original_artifacts == {}
    assert outcome.steps == 4
    assert outcome.tool_calls == 2
    assert parent.usage.input_tokens > child.usage.input_tokens > 0
    with pytest.raises(ValueError, match="delegated runs"):
        await h.service.continue_run(child.run_id, "Modify files now")


async def test_child_cannot_read_outside_scope_or_create_files(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    h = Harness(
        repo,
        [
            [
                tool(
                    "delegate",
                    "agent.delegate",
                    role="scout",
                    task="Investigate calc",
                    file_scope=["src/calc.py"],
                    max_steps=5,
                ),
                finish("tool_calls"),
            ],
            [tool("outside", "repo.read", path="README.md"), finish("tool_calls")],
            [tool("write", "repo.create", path="injected.py", content="bad"), finish("tool_calls")],
            [tool("read", "repo.read", path="src/calc.py"), finish("tool_calls")],
            [text("src/calc.py:2 contains the defect."), finish()],
            [text("Investigation complete."), finish()],
        ],
    )
    outcome = await h.service.run("Locate the bug")
    assert outcome.status is RunStatus.SUCCEEDED
    completed = h.sink.events_of("tool.completed")
    assert next(e for e in completed if e.call_id == "outside").error_code == "denied"
    assert next(e for e in completed if e.call_id == "write").error_code == "denied"
    assert not (repo / "injected.py").exists()
    assert outcome.steps == 6


async def test_child_budget_stop_is_reported_as_incomplete(tmp_path: Path) -> None:
    h = Harness(
        make_repo(tmp_path),
        [
            [
                tool(
                    "delegate", "agent.delegate", role="reviewer", task="Review calc", max_steps=1
                ),
                finish("tool_calls"),
            ],
            [tool("read", "repo.read", path="src/calc.py"), finish("tool_calls")],
            [text("The reviewer ran out of budget; no complete review is available."), finish()],
        ],
    )
    await h.service.run("Review calc")
    child = h.sink.events_of("child.finished")[0]
    assert child.status == "stopped"
    result = next(e for e in h.sink.events_of("tool.completed") if e.call_id == "delegate")
    assert result.status == "error"


async def test_scoped_diff_reports_truncation_of_the_current_side(tmp_path: Path) -> None:
    """子 Agent 的有作用域 diff 只读每个文件的前若干行来构造"现在这一侧"。

    丢掉 read.truncated 会让模型看到一份停在半途的差异，却被告知它是完整的——
    一份残缺的审计被当成了全貌。这条分支此前一行测试都没有。
    """
    from haven.application.tool_readers import SCOPED_DIFF_MAX_LINES

    repo = make_repo(tmp_path)
    target = repo / "src" / "calc.py"
    filler = "\n".join(f"# filler {i}" for i in range(SCOPED_DIFF_MAX_LINES + 20))
    target.write_text(f"def add(a, b):\n    return a - b\n{filler}\n", encoding="utf-8")

    h = Harness(
        repo,
        [
            [tool("r1", "repo.read", path="src/calc.py"), finish("tool_calls")],
            [
                tool(
                    "e1",
                    "repo.edit",
                    path="src/calc.py",
                    old_string="return a - b",
                    new_string="return a + b",
                ),
                finish("tool_calls"),
            ],
            [
                tool(
                    "d1",
                    "agent.delegate",
                    role="reviewer",
                    task="Review the diff",
                    file_scope=["src/calc.py"],
                ),
                finish("tool_calls"),
            ],
            # 子 Agent 调 repo.diff：文件 2022 行，超过 SCOPED_DIFF_MAX_LINES，
            # "现在这一侧"因此只读到前若干行。
            [tool("g1", "repo.diff"), finish("tool_calls")],
            [text("The diff is partial but flagged as such."), finish()],
            # 父级自己也要留下 diff 与 check 证据，否则 Evidence Gate 会拦住这次运行。
            [tool("g2", "repo.diff"), finish("tool_calls")],
            [tool("k1", "repo.check", recipe_id="verify-calc"), finish("tool_calls")],
            [text("Review complete."), finish()],
        ],
    )
    outcome = await h.service.run("Fix add() and have a reviewer check the diff")
    assert outcome.status is RunStatus.SUCCEEDED

    # 有作用域的那份结果载荷只有 files 与 diff（父级那份还带 insertions/deletions），
    # 据此把它从同名工具的多条结果里认出来。
    scoped = [
        m.content
        for req in h.model.requests_seen
        for m in req.messages
        if m.role == "tool" and '"repo.diff"' in m.content and '"insertions"' not in m.content
    ]
    assert scoped, "子 Agent 应该收到一份有作用域的 repo.diff 结果"
    assert '"truncated": true' in scoped[0], "文件超出作用域 diff 的行数上限，必须带截断标志"
    # 截断是"不完整"的声明，不是把内容藏起来：改动本身仍然呈现。
    assert "return a + b" in scoped[0]
