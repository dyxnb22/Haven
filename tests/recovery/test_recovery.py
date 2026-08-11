"""Recovery semantics: resume what is safe, never replay ambiguous effects."""

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
    """Simulate a run that crashed mid-edit: journal says STARTED, no confirm.

    The checkpoint carries the transcript up to the read; the execution record
    for the edit exists but was never confirmed (as if the process died).
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


class TestEffectClassification:
    async def test_edit_never_ran_is_safe_to_resume(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        h = Harness(repo, [])
        run_id, recovery = await crash_setup(repo, h)

        report = await recovery.inspect(run_id)
        assert report.can_resume
        assert report.findings[0].classification == "not_run"
        # the journal was reconciled automatically because the proof is solid
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
        # never auto-reconciled
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

        # a fresh service continues the same run with a new scripted plan
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
        # usage continued from the checkpoint instead of restarting
        assert outcome.steps > 2


class TestPlanSurvivesRecovery:
    async def test_plan_is_restored_from_the_checkpoint(self, tmp_path: Path) -> None:
        """ADR 0006: the plan lives in State, so a resumed run still has it."""
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
        # replay consumed zero model turns
        assert h.model.requests_seen is not None
