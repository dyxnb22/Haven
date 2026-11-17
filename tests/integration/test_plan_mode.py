"""只读规划、显式确认、持久化与执行之间的边界。"""

import json
from pathlib import Path
from typing import Any

import pytest

from haven.application.run_service import build_run_context_from_checkpoint
from haven.application.runtime import HavenRuntime
from haven.contracts.checkpoint import CheckpointV1
from haven.contracts.events import PlanUpdated, RunCreated, ToolCompleted
from haven.contracts.mcp import MCPToolSchema
from haven.domain.budget import Budget
from haven.domain.enums import (
    AgentMode,
    ApprovalMode,
    PermissionMode,
    PlanStatus,
    PolicyDecision,
    RunStatus,
    StopReason,
)
from haven.domain.policy import ToolFacts, evaluate_policy
from tests.integration.harness import Harness, finish, make_repo, text, tool


def planning_turns() -> list[list[Any]]:
    return [
        [tool("read", "repo.read", path="src/calc.py"), finish("tool_calls")],
        [
            tool(
                "plan",
                "task.plan",
                steps=[
                    {"title": "Fix add()", "status": "pending"},
                    {"title": "Verify the change", "status": "pending"},
                ],
            ),
            finish("tool_calls"),
        ],
        [text("Replace subtraction in src/calc.py, then diff and run verify-calc."), finish()],
    ]


def edit_turn() -> list[Any]:
    return [
        tool(
            "edit",
            "repo.edit",
            path="src/calc.py",
            old_string="return a - b  # BUG: should be +",
            new_string="return a + b",
            summary="Fix add()",
        ),
        finish("tool_calls"),
    ]


def completed_plan_turn() -> list[Any]:
    return [
        tool(
            "done",
            "task.plan",
            steps=[
                {"title": "Fix add()", "status": "done"},
                {"title": "Verify the change", "status": "done"},
            ],
        ),
        finish("tool_calls"),
    ]


@pytest.mark.parametrize(
    "name",
    [
        "repo.edit",
        "repo.create",
        "repo.delete",
        "repo.move",
        "repo.apply_patch",
        "repo.exec",
        "repo.check",
        "recipe.propose",
        "task.start",
        "task.stop",
    ],
)
@pytest.mark.parametrize("permission", list(PermissionMode))
def test_plan_policy_denies_effects_even_with_interactive_permissions(
    name: str, permission: PermissionMode
) -> None:
    outcome = evaluate_policy(
        permission,
        ToolFacts(
            tool_name=name,
            sandbox_available=True,
            recipe_registered=True,
            agent_mode=AgentMode.PLAN,
        ),
    )
    assert outcome.decision is PolicyDecision.DENY
    assert outcome.reason_code == "plan_mode"


@pytest.mark.parametrize("read_only", [False, True])
def test_plan_policy_checks_mcp_read_only_declarations(read_only: bool) -> None:
    outcome = evaluate_policy(
        PermissionMode.INTERACTIVE,
        ToolFacts(tool_name="mcp.docs.lookup", mcp_read_only=read_only, agent_mode=AgentMode.PLAN),
    )
    assert outcome.decision is (PolicyDecision.ALLOW if read_only else PolicyDecision.DENY)


@pytest.mark.parametrize("approval_mode", list(ApprovalMode))
async def test_plan_hides_effect_tools_and_denies_invented_calls(
    tmp_path: Path, approval_mode: ApprovalMode
) -> None:
    repo = make_repo(tmp_path)
    turns = planning_turns()
    turns.insert(1, edit_turn())
    h = Harness(repo, turns, approval_mode=approval_mode)
    outcome = await h.service.run("Plan the fix", agent_mode=AgentMode.PLAN)

    assert outcome.status is RunStatus.SUCCEEDED
    assert "return a - b" in (repo / "src/calc.py").read_text()
    assert not h.sink.events_of("approval.requested")
    for request in h.model.requests_seen:
        names = {schema.name for schema in request.tools}
        assert {"repo.read", "repo.search", "task.plan"} <= names
        assert not names & {"repo.edit", "repo.exec", "repo.check", "task.start", "task.stop"}
        assert "PLAN MODE" in request.messages[0].content
    completed = [e for e in h.sink.events_of("tool.completed") if isinstance(e, ToolCompleted)]
    assert next(e for e in completed if e.call_id == "edit").error_code == "denied"
    checkpoint = await h.store.load_checkpoint(outcome.run_id)
    assert checkpoint is not None
    assert checkpoint.agent_mode is AgentMode.PLAN
    assert checkpoint.mode == "read_only"
    assert checkpoint.plan_status is PlanStatus.READY


async def test_ordinary_follow_up_does_not_confirm_a_plan(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    h = Harness(repo, planning_turns() + [edit_turn(), [text("Plan unchanged."), finish()]])
    first = await h.service.run("Plan the fix", agent_mode=AgentMode.PLAN)
    second = await h.service.continue_run(
        first.run_id, "Start implementing now", approval_mode=ApprovalMode.FULL
    )
    checkpoint = await h.store.load_checkpoint(second.run_id)
    assert checkpoint is not None
    assert checkpoint.agent_mode is AgentMode.PLAN
    assert checkpoint.plan_status is PlanStatus.READY
    assert "return a - b" in (repo / "src/calc.py").read_text()


async def test_planning_only_advertises_mcp_tools_declared_read_only(tmp_path: Path) -> None:
    h = Harness(make_repo(tmp_path), planning_turns())
    tools = tuple(
        MCPToolSchema.create(
            server_name="docs",
            tool_name=name,
            description=name,
            parameters={},
            is_read_only=read_only,
        )
        for name, read_only in (("lookup", True), ("publish", False))
    )
    h.service._mcp_manager._tools = {t.namespaced_name: t for t in tools}
    await h.service.run("Plan the fix", agent_mode=AgentMode.PLAN)
    for request in h.model.requests_seen:
        names = {schema.name for schema in request.tools}
        assert "mcp.docs.lookup" in names
        assert "mcp.docs.publish" not in names


@pytest.mark.parametrize(
    ("changed", "error"),
    [
        ({"workspace_digest": "other-workspace"}, "workspace identity changed"),
        ({"catalog_digest": "other-catalog"}, "capability catalog changed"),
    ],
)
async def test_confirmation_rejects_a_changed_workspace_or_capability_catalog(
    tmp_path: Path, changed: dict[str, str], error: str
) -> None:
    h = Harness(make_repo(tmp_path), planning_turns())
    planned = await h.service.run("Plan the fix", agent_mode=AgentMode.PLAN)
    checkpoint = await h.store.load_checkpoint(planned.run_id)
    assert checkpoint is not None
    await h.store.save_checkpoint(checkpoint.model_copy(update=changed))
    with pytest.raises(ValueError, match=error):
        await h.service.implement_plan(planned.run_id)
    assert len(h.model.requests_seen) == len(planning_turns())


async def test_explicit_confirmation_executes_the_saved_plan_with_fresh_evidence(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    turns = planning_turns() + [
        edit_turn(),
        [tool("diff", "repo.diff"), finish("tool_calls")],
        [tool("check", "repo.check", recipe_id="verify-calc"), finish("tool_calls")],
        completed_plan_turn(),
        [text("Fixed and verified."), finish()],
    ]
    h = Harness(repo, turns)
    runtime = HavenRuntime(
        run_service=h.service, store=h.store, emitter=h.emitter, approvals=h.approver
    )
    planned = await runtime.start_run("Fix add()", agent_mode=AgentMode.PLAN)
    planned_checkpoint = await runtime.get_checkpoint(planned.run_id)
    outcome = await runtime.implement_plan(planned.run_id)

    assert outcome.status is RunStatus.SUCCEEDED
    assert outcome.stop_reason is StopReason.EVIDENCE_SATISFIED
    assert outcome.run_id != planned.run_id
    assert "return a + b" in (repo / "src/calc.py").read_text()
    checkpoint = await runtime.get_checkpoint(outcome.run_id)
    assert checkpoint is not None
    assert checkpoint.agent_mode is AgentMode.AGENT
    assert checkpoint.plan_status is PlanStatus.COMPLETED
    assert checkpoint.evidence.edits
    assert await runtime.get_checkpoint(planned.run_id) == planned_checkpoint
    created = [e for e in h.sink.events_of("run.created") if isinstance(e, RunCreated)]
    assert created[-1].parent_run_id == planned.run_id
    assert created[-1].agent_mode == "agent"
    inherited = [
        e
        for e in h.sink.events_of("plan.updated")
        if isinstance(e, PlanUpdated) and e.run_id == outcome.run_id
    ]
    assert inherited[0].plan_status == "executing"
    assert all(step.status == "pending" for step in inherited[0].steps)
    execution_request = h.model.requests_seen[len(planning_turns())]
    assert "repo.edit" in {schema.name for schema in execution_request.tools}
    assert any("Fix add()" in message.content for message in execution_request.messages)


async def test_completed_steps_do_not_bypass_the_evidence_gate(tmp_path: Path) -> None:
    h = Harness(
        make_repo(tmp_path),
        planning_turns() + [edit_turn(), completed_plan_turn(), [text("Done."), finish()]],
        repeat_last=True,
    )
    first = await h.service.run("Plan the fix", agent_mode=AgentMode.PLAN)
    outcome = await h.service.implement_plan(first.run_id)
    assert outcome.status is RunStatus.STOPPED
    assert outcome.stop_reason is StopReason.EVIDENCE_MISSING
    checkpoint = await h.store.load_checkpoint(outcome.run_id)
    assert checkpoint is not None
    assert checkpoint.plan_status is PlanStatus.EXECUTING


async def test_planning_without_structured_steps_stops_after_bounded_retries(
    tmp_path: Path,
) -> None:
    h = Harness(make_repo(tmp_path), [[text("I would fix add()."), finish()]], repeat_last=True)
    outcome = await h.service.run("Plan the fix", agent_mode=AgentMode.PLAN)
    assert outcome.status is RunStatus.STOPPED
    assert outcome.gate_reason == "plan_missing"
    assert len(h.model.requests_seen) == 3
    with pytest.raises(ValueError, match="no ready plan"):
        await h.service.implement_plan(outcome.run_id)


async def test_an_interrupted_draft_cannot_be_implemented(tmp_path: Path) -> None:
    h = Harness(
        make_repo(tmp_path),
        [planning_turns()[1]],
        budget=Budget(max_steps=1),
    )
    outcome = await h.service.run("Plan the fix", agent_mode=AgentMode.PLAN)
    checkpoint = await h.store.load_checkpoint(outcome.run_id)
    assert checkpoint is not None
    assert checkpoint.plan_status is PlanStatus.DRAFTING
    with pytest.raises(ValueError, match="no ready plan"):
        await h.service.implement_plan(outcome.run_id)


async def test_read_only_sessions_cannot_implement_a_ready_plan(tmp_path: Path) -> None:
    h = Harness(make_repo(tmp_path), planning_turns(), mode=PermissionMode.READ_ONLY)
    outcome = await h.service.run("Plan the fix", agent_mode=AgentMode.PLAN)
    with pytest.raises(ValueError, match="writable session"):
        await h.service.implement_plan(outcome.run_id)


async def test_resumed_planning_remains_read_only_and_legacy_checkpoints_load(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    h = Harness(repo, planning_turns() + [edit_turn(), [text("Still planning."), finish()]])
    first = await h.service.run("Plan the fix", agent_mode=AgentMode.PLAN)
    checkpoint = await h.store.load_checkpoint(first.run_id)
    assert checkpoint is not None
    restored = build_run_context_from_checkpoint(checkpoint)
    assert restored.agent_mode is AgentMode.PLAN
    await h.service.resume(restored)
    assert "return a - b" in (repo / "src/calc.py").read_text()

    legacy = checkpoint.model_dump(mode="json")
    legacy.pop("agent_mode")
    legacy.pop("plan_status")
    loaded = CheckpointV1.model_validate_json(json.dumps(legacy))
    assert loaded.agent_mode is AgentMode.AGENT
    assert loaded.plan_status is None
