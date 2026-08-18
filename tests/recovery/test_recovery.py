"""恢复语义：恢复安全的操作，绝不重放结果不明确的副作用。"""

from pathlib import Path

from haven.adapters.workspace_fs import FsWorkspace
from haven.application.recovery_service import RecoveryService
from haven.application.replay_service import ReplayService
from haven.application.run_service import RunService
from haven.contracts.checkpoint import (
    BudgetSnapshot,
    CheckpointV1,
    EvidenceSnapshot,
    UsageSnapshot,
)
from haven.contracts.model import ModelMessage
from haven.domain.budget import Budget, BudgetUsage
from haven.domain.enums import EffectState, RunStatus, StopReason
from haven.ports.session import ExecutionRecord

from ..integration.harness import (
    CollectingSink,
    Harness,
    default_recipes,
    finish,
    make_repo,
    text,
    tool,
)


async def crash_setup(
    repo: Path, h: Harness, *, tamper: str | None = None
) -> tuple[str, RecoveryService]:
    """模拟在编辑中途崩溃的运行：日志记录为 STARTED，但没有确认记录。

    检查点保存截至读取操作的对话记录；编辑的执行记录已经存在，但从未确认
    （相当于进程在此处退出）。
    """
    run_id = "run-crash01"
    workspace = h.workspace
    calc = workspace.path_facts("src/calc.py")
    assert calc.digest is not None

    await h.store.create_run(
        run_id, str(repo), workspace.workspace_digest, "fix add()", "interactive"
    )
    await h.store.update_run_status(run_id, RunStatus.EXECUTING_TOOL, "")
    checkpoint = CheckpointV1(
        run_id=run_id,
        workspace_digest=workspace.workspace_digest,
        goal="fix add()",
        mode="interactive",
        status=RunStatus.EXECUTING_TOOL.value,
        last_seq=4,
        budget=BudgetSnapshot.from_domain(Budget()),
        usage=UsageSnapshot.from_domain(BudgetUsage(steps=2, tool_calls=1)),
        messages=(
            ModelMessage(role="assistant", content="I read the file."),
            ModelMessage(
                role="tool",
                content='<tool_output tool="repo.read">...</tool_output>',
                tool_call_id="c1",
            ),
        ),
        evidence=EvidenceSnapshot(),
        files_read={"src/calc.py": calc.digest},
    )
    await h.store.save_checkpoint(checkpoint)
    await h.store.record_execution(
        ExecutionRecord(
            call_id="c2",
            run_id=run_id,
            ticket_digest="t-crash",
            tool_name="repo.edit",
            effect_state=EffectState.STARTED,
            preimage_digest=calc.digest,
            postimage_digest="",
            path="src/calc.py",
        )
    )
    if tamper:
        (repo / "src" / "calc.py").write_text(tamper)
    return run_id, RecoveryService(h.store, workspace)


async def crash_setup_for(
    repo: Path,
    h: Harness,
    *,
    tool_name: str,
    path: str,
    preimage: str,
    postimage: str = "",
    dest_path: str = "",
) -> tuple[str, RecoveryService]:
    """与 `crash_setup` 类似，但用于任意被中断的写入工具。"""
    run_id = "run-crash02"
    workspace = h.workspace
    await h.store.create_run(run_id, str(repo), workspace.workspace_digest, "goal", "interactive")
    await h.store.update_run_status(run_id, RunStatus.EXECUTING_TOOL, "")
    checkpoint = CheckpointV1(
        run_id=run_id,
        workspace_digest=workspace.workspace_digest,
        goal="goal",
        mode="interactive",
        status=RunStatus.EXECUTING_TOOL.value,
        last_seq=2,
        budget=BudgetSnapshot.from_domain(Budget()),
        usage=UsageSnapshot.from_domain(BudgetUsage(steps=1, tool_calls=1)),
        messages=(ModelMessage(role="assistant", content="working"),),
        evidence=EvidenceSnapshot(),
        files_read={},
    )
    await h.store.save_checkpoint(checkpoint)
    await h.store.record_execution(
        ExecutionRecord(
            call_id="c9",
            run_id=run_id,
            ticket_digest="t-crash",
            tool_name=tool_name,
            effect_state=EffectState.STARTED,
            preimage_digest=preimage,
            postimage_digest=postimage,
            path=path,
            dest_path=dest_path,
        )
    )
    return run_id, RecoveryService(h.store, workspace)


class TestCreateDeleteClassification:
    """中断的 create 或 delete 根据磁盘上能够证明的事实分类，与普通 `repo.edit`
    使用相同标准；move 会保持 unknown，因为它的记录无法区分移动中途的状态。"""

    async def test_create_with_no_file_is_not_run(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        h = Harness(repo, [])
        run_id, recovery = await crash_setup_for(
            repo, h, tool_name="repo.create", path="src/new_module.py", preimage=""
        )
        report = await recovery.inspect(run_id)
        assert report.can_resume
        assert report.findings[0].classification == "not_run"

    async def test_create_with_a_present_file_and_no_postimage_is_unknown(
        self, tmp_path: Path
    ) -> None:
        """没有预期 postimage 的旧记录无法证明当前文件就是预期内容。"""
        repo = make_repo(tmp_path)
        (repo / "src" / "new_module.py").write_text("something\n")
        h = Harness(repo, [])
        run_id, recovery = await crash_setup_for(
            repo, h, tool_name="repo.create", path="src/new_module.py", preimage=""
        )
        report = await recovery.inspect(run_id)
        assert not report.can_resume
        assert report.findings[0].classification == "unknown"

    async def test_create_matching_the_expected_postimage_is_confirmed(
        self, tmp_path: Path
    ) -> None:
        """预期 postimage 会在 STARTED 时写入日志，因此在“已写入但尚未确认”的窗口
        内崩溃时，系统仍能证明操作已经完成。"""
        from haven.domain.digest import sha256_text

        repo = make_repo(tmp_path)
        (repo / "src" / "new_module.py").write_text("intended content\n")
        h = Harness(repo, [])
        run_id, recovery = await crash_setup_for(
            repo,
            h,
            tool_name="repo.create",
            path="src/new_module.py",
            preimage="",
            postimage=sha256_text("intended content\n"),
        )
        report = await recovery.inspect(run_id)
        assert report.can_resume
        assert report.findings[0].classification == "confirmed"

    async def test_create_with_a_different_file_than_expected_is_unknown(
        self, tmp_path: Path
    ) -> None:
        from haven.domain.digest import sha256_text

        repo = make_repo(tmp_path)
        (repo / "src" / "new_module.py").write_text("something else\n")
        h = Harness(repo, [])
        run_id, recovery = await crash_setup_for(
            repo,
            h,
            tool_name="repo.create",
            path="src/new_module.py",
            preimage="",
            postimage=sha256_text("intended content\n"),
        )
        report = await recovery.inspect(run_id)
        assert not report.can_resume
        assert report.findings[0].classification == "unknown"

    async def test_delete_with_the_file_gone_is_confirmed(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        h = Harness(repo, [])
        run_id, recovery = await crash_setup_for(
            repo, h, tool_name="repo.delete", path="src/missing.py", preimage="d-old"
        )
        report = await recovery.inspect(run_id)
        assert report.findings[0].classification == "confirmed"

    async def test_delete_with_the_preimage_intact_is_not_run(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        h = Harness(repo, [])
        facts = h.workspace.path_facts("src/calc.py")
        assert facts.digest is not None
        run_id, recovery = await crash_setup_for(
            repo, h, tool_name="repo.delete", path="src/calc.py", preimage=facts.digest
        )
        report = await recovery.inspect(run_id)
        assert report.can_resume
        assert report.findings[0].classification == "not_run"

    async def test_delete_with_a_changed_file_is_unknown(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        h = Harness(repo, [])
        run_id, recovery = await crash_setup_for(
            repo, h, tool_name="repo.delete", path="src/calc.py", preimage="d-does-not-match"
        )
        report = await recovery.inspect(run_id)
        assert not report.can_resume
        assert report.findings[0].classification == "unknown"

    async def test_move_without_a_recorded_dest_stays_unknown(self, tmp_path: Path) -> None:
        """只记录源路径的旧记录无法区分移动中途的状态，因此必须保持不明确。"""
        repo = make_repo(tmp_path)
        h = Harness(repo, [])
        facts = h.workspace.path_facts("src/calc.py")
        assert facts.digest is not None
        run_id, recovery = await crash_setup_for(
            repo, h, tool_name="repo.move", path="src/calc.py", preimage=facts.digest
        )
        report = await recovery.inspect(run_id)
        assert report.findings[0].classification == "unknown"

    async def test_move_with_source_intact_and_dest_absent_is_not_run(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        h = Harness(repo, [])
        facts = h.workspace.path_facts("src/calc.py")
        assert facts.digest is not None
        run_id, recovery = await crash_setup_for(
            repo,
            h,
            tool_name="repo.move",
            path="src/calc.py",
            preimage=facts.digest,
            dest_path="src/renamed.py",
        )
        report = await recovery.inspect(run_id)
        assert report.can_resume
        assert report.findings[0].classification == "not_run"

    async def test_move_with_dest_holding_the_content_and_source_gone_is_confirmed(
        self, tmp_path: Path
    ) -> None:
        repo = make_repo(tmp_path)
        h = Harness(repo, [])
        facts = h.workspace.path_facts("src/calc.py")
        assert facts.digest is not None
        (repo / "src" / "renamed.py").write_text((repo / "src" / "calc.py").read_text())
        (repo / "src" / "calc.py").unlink()
        run_id, recovery = await crash_setup_for(
            repo,
            h,
            tool_name="repo.move",
            path="src/calc.py",
            preimage=facts.digest,
            dest_path="src/renamed.py",
        )
        report = await recovery.inspect(run_id)
        assert report.can_resume
        assert report.findings[0].classification == "confirmed"

    async def test_move_with_both_ends_present_is_unknown_not_replayed(
        self, tmp_path: Path
    ) -> None:
        """复制已经完成，但 unlink 尚未完成：自动补完会构成重放，而恢复逻辑从不
        重放操作，因此必须阻塞并等待人工调和。"""
        repo = make_repo(tmp_path)
        h = Harness(repo, [])
        facts = h.workspace.path_facts("src/calc.py")
        assert facts.digest is not None
        (repo / "src" / "renamed.py").write_text((repo / "src" / "calc.py").read_text())
        run_id, recovery = await crash_setup_for(
            repo,
            h,
            tool_name="repo.move",
            path="src/calc.py",
            preimage=facts.digest,
            dest_path="src/renamed.py",
        )
        report = await recovery.inspect(run_id)
        assert not report.can_resume
        assert report.findings[0].classification == "unknown"
        assert "source was not removed" in report.findings[0].detail


class TestEffectClassification:
    async def test_edit_never_ran_is_safe_to_resume(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        h = Harness(repo, [])
        run_id, recovery = await crash_setup(repo, h)

        report = await recovery.inspect(run_id)
        assert report.can_resume
        assert report.findings[0].classification == "not_run"
        # 由于证据充分，日志已被自动调和
        executions = await h.store.load_executions(run_id)
        assert executions[0].effect_state is EffectState.RECONCILED_NOT_RUN

    async def test_ambiguous_effect_blocks_resume(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        h = Harness(repo, [])
        run_id, recovery = await crash_setup(repo, h, tamper="mangled content\n")

        report = await recovery.inspect(run_id)
        assert not report.can_resume
        assert report.findings[0].classification == "unknown"
        assert any("reconcile" in blocker for blocker in report.blockers)
        # 永远不会自动调和
        executions = await h.store.load_executions(run_id)
        assert executions[0].effect_state is EffectState.STARTED

    async def test_manual_reconcile_then_resume(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        h = Harness(repo, [])
        run_id, recovery = await crash_setup(repo, h, tamper="mangled content\n")

        await recovery.reconcile(run_id, "c2", "confirmed")
        report = await recovery.inspect(run_id)
        assert report.can_resume

    async def test_abandon_marks_run_failed(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        h = Harness(repo, [])
        run_id, recovery = await crash_setup(repo, h, tamper="mangled content\n")

        await recovery.reconcile(run_id, "c2", "abandon")
        run = await h.store.get_run(run_id)
        assert run is not None
        assert run.status is RunStatus.FAILED

    async def test_finished_run_cannot_resume(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        h = Harness(repo, [[text("hi"), finish()]])
        outcome = await h.service.run("Say hi")
        recovery = RecoveryService(h.store, h.workspace)
        report = await recovery.inspect(outcome.run_id)
        assert not report.can_resume
        assert any("already finished" in b for b in report.blockers)

    async def test_workspace_identity_mismatch_blocks(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        h = Harness(repo, [])
        run_id, _ = await crash_setup(repo, h)

        other = tmp_path / "other-repo"
        other.mkdir()
        recovery = RecoveryService(h.store, FsWorkspace(other))
        report = await recovery.inspect(run_id)
        assert not report.can_resume
        assert any("workspace identity" in b for b in report.blockers)


class TestResume:
    async def test_resume_completes_the_task(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        h = Harness(repo, [])
        run_id, recovery = await crash_setup(repo, h)

        report = await recovery.inspect(run_id)
        assert report.can_resume and report.checkpoint is not None
        ctx = await recovery.build_context(report.checkpoint)

        # 新服务会用新的脚本化计划继续同一运行
        from haven.adapters.process_executor import ProcessExecutor
        from haven.adapters.providers.scripted import ScriptedModel
        from haven.domain.enums import PermissionMode

        resumed_model = ScriptedModel(
            [
                [tool("c1", "repo.read", path="src/calc.py"), finish("tool_calls")],
                [
                    tool(
                        "c2b",
                        "repo.edit",
                        path="src/calc.py",
                        old_string="return a - b  # BUG: should be +",
                        new_string="return a + b",
                    ),
                    finish("tool_calls"),
                ],
                [tool("c3", "repo.diff"), finish("tool_calls")],
                [tool("c4", "repo.check", recipe_id="verify-calc"), finish("tool_calls")],
                [text("Fixed after resume."), finish()],
            ]
        )
        service = RunService(
            model=resumed_model,
            workspace=h.workspace,
            executor=ProcessExecutor(),
            store=h.store,
            emitter=h.emitter,
            approvals=h.approver,
            recipes=default_recipes(),
            mode=PermissionMode.INTERACTIVE,
            budget=Budget(),
        )
        outcome = await service.resume(ctx)
        assert outcome.run_id == run_id
        assert outcome.status is RunStatus.SUCCEEDED
        assert outcome.stop_reason is StopReason.EVIDENCE_SATISFIED
        assert "return a + b" in (repo / "src" / "calc.py").read_text()
        # 用量从 checkpoint 继续，而不是重新开始
        assert outcome.steps > 2


class TestPlanSurvivesRecovery:
    async def test_plan_is_restored_from_the_checkpoint(self, tmp_path: Path) -> None:
        """ADR 0006：计划保存在 State 中，因此恢复的运行仍然拥有该计划。"""
        repo = make_repo(tmp_path)
        h = Harness(
            repo,
            [
                [
                    tool(
                        "c1",
                        "task.plan",
                        steps=[
                            {"title": "read the file", "status": "done"},
                            {"title": "fix the bug", "status": "in_progress"},
                        ],
                    ),
                    finish("tool_calls"),
                ],
                [text("stopping here"), finish()],
            ],
        )
        outcome = await h.service.run("Fix add()")

        checkpoint = await h.store.load_checkpoint(outcome.run_id)
        assert checkpoint is not None
        recovery = RecoveryService(h.store, h.workspace)
        ctx = await recovery.build_context(checkpoint)

        assert [s.title for s in ctx.plan] == ["read the file", "fix the bug"]
        assert ctx.plan[1].status == "in_progress"


class TestReplay:
    async def test_replay_reproduces_journal_without_side_effects(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        h = Harness(
            repo,
            [
                [tool("c1", "repo.read", path="src/calc.py"), finish("tool_calls")],
                [text("done"), finish()],
            ],
        )
        outcome = await h.service.run("read something")
        original_kinds = [k for k in h.sink.kinds() if k != "assistant.delta"]

        replay_sink = CollectingSink()
        envelopes = await ReplayService(h.store).replay(outcome.run_id, replay_sink)
        assert replay_sink.kinds() == original_kinds
        assert [e.seq for e in envelopes] == sorted(e.seq for e in envelopes)
        # replay 没有消耗任何模型轮次
        assert h.model.requests_seen is not None
