"""在真实执行通道验证选定检查：漏验、筛选、替换、写入和恢复。"""

import json
import sys
from pathlib import Path

import pytest

from haven import bootstrap
from haven.adapters.providers.scripted import ScriptedModel
from haven.application.approvals import AutoApprover
from haven.application.verification import recipe_invocation_digest
from haven.contracts.model import ModelEvent
from haven.contracts.tools import RecipeSpec
from haven.contracts.user_input import UserInputResponse
from haven.domain.enums import AgentMode, PermissionMode, RunStatus, StopReason
from tests.integration.fakes import RecordingLauncher
from tests.integration.harness import Harness, finish, make_repo, text, tool
from tests.integration.test_user_input import ask_turn


def recipes(*, fail_lint: bool = False) -> dict[str, RecipeSpec]:
    return {
        name: RecipeSpec(
            id=name,
            argv=(sys.executable, "-c", f"raise SystemExit({int(name == 'lint' and fail_lint)})"),
        )
        for name in ("test", "lint")
    }


def check_turn(recipe: str, call_id: str = "check", **kwargs: object) -> list[ModelEvent]:
    return [tool(call_id, "repo.check", recipe_id=recipe, **kwargs), finish("tool_calls")]


@pytest.mark.parametrize("case", ["missing", "failed", "filtered", "stale"])
async def test_incomplete_or_changed_checks_do_not_finish_the_run(
    tmp_path: Path, case: str
) -> None:
    turns = [check_turn("test", args=["--only-one"]) if case == "filtered" else check_turn("test")]
    if case != "missing":
        turns.append(check_turn("lint", "lint"))
    if case == "stale":
        turns.extend(
            [
                [
                    tool("write", "repo.create", path="new.txt", content="a new write"),
                    finish("tool_calls"),
                ],
                [tool("diff", "repo.diff"), finish("tool_calls")],
            ]
        )
    turns.append([text("Done."), finish()])
    h = Harness(
        make_repo(tmp_path),
        turns,
        repeat_last=True,
        recipes=recipes(fail_lint=case == "failed"),
        verification_groups={"quality": ("test", "lint")},
    )
    try:
        outcome = await h.service.run("Inspect and verify", check_group="quality")
        assert outcome.status is RunStatus.STOPPED
        assert outcome.stop_reason is StopReason.EVIDENCE_MISSING
        expected = {
            "missing": "not_run",
            "failed": "failed",
            "filtered": "mismatch",
            "stale": "stale",
        }
        assert outcome.gate_reason == f"required_check_{expected[case]}"
        checkpoint = await h.store.load_checkpoint(outcome.run_id)
        assert checkpoint is not None and checkpoint.verification is not None
        assert [r.recipe_id for r in checkpoint.verification.required] == ["test", "lint"]
    finally:
        await h.service.close()


async def test_all_checks_pass_and_continuations_inherit_until_explicitly_cleared(
    tmp_path: Path,
) -> None:
    h = Harness(
        make_repo(tmp_path),
        [
            check_turn("test"),
            check_turn("lint", "lint"),
            [text("Verified."), finish()],
            [text("Follow-up without rechecking."), finish()],
            [text("Follow-up without rechecking."), finish()],
            [text("Follow-up without rechecking."), finish()],
            [text("Explicit read-only analysis."), finish()],
        ],
        recipes=recipes(),
        verification_groups={"quality": ("test", "lint")},
    )
    try:
        first = await h.service.run("Verify the project", check_group="quality")
        assert first.status is RunStatus.SUCCEEDED
        assert first.stop_reason is StopReason.EVIDENCE_SATISFIED
        saved = await h.store.load_checkpoint(first.run_id)
        assert saved is not None
        assert saved.evidence.checks[0].recipe_digest == recipe_invocation_digest(recipes()["test"])
        second = await h.service.continue_run(first.run_id, "Explain another detail")
        assert second.status is RunStatus.STOPPED
        assert second.gate_reason == "required_check_not_run"
        third = await h.service.continue_run(second.run_id, "Only explain now", check_group="")
        assert third.status is RunStatus.SUCCEEDED
        cleared = await h.store.load_checkpoint(third.run_id)
        assert cleared is not None and cleared.verification is None
        assert await h.store.load_checkpoint(first.run_id) == saved
    finally:
        await h.service.close()


async def test_model_cannot_replace_a_required_recipe_even_with_approval(tmp_path: Path) -> None:
    h = Harness(
        make_repo(tmp_path),
        [
            [
                tool(
                    "replace",
                    "recipe.propose",
                    recipe_id="lint",
                    argv=[sys.executable, "-c", "pass"],
                ),
                finish("tool_calls"),
            ],
            check_turn("test"),
            check_turn("lint", "lint"),
            [text("Done."), finish()],
        ],
        repeat_last=True,
        recipes=recipes(fail_lint=True),
        verification_groups={"quality": ("test", "lint")},
    )
    try:
        outcome = await h.service.run("Verify the project", check_group="quality")
        assert outcome.status is RunStatus.STOPPED
        assert outcome.gate_reason == "required_check_failed"
        decisions = h.sink.events_of("policy.decided")
        assert any(
            getattr(event, "reason_code", "") == "required_recipe_frozen" for event in decisions
        )
        assert h.service._recipes["lint"].ephemeral is False
    finally:
        await h.service.close()


async def test_plan_preserves_checks_without_running_them_before_confirmation(
    tmp_path: Path,
) -> None:
    h = Harness(
        make_repo(tmp_path),
        [
            [
                tool(
                    "plan",
                    "task.plan",
                    steps=[{"title": "Verify the project", "status": "pending"}],
                ),
                finish("tool_calls"),
            ],
            [text("Plan ready."), finish()],
            check_turn("test"),
            [text("Implemented."), finish()],
        ],
        repeat_last=True,
        recipes=recipes(),
        verification_groups={"quality": ("test", "lint")},
    )
    try:
        planned = await h.service.run(
            "Plan the verification", agent_mode=AgentMode.PLAN, check_group="quality"
        )
        assert planned.status is RunStatus.SUCCEEDED
        checkpoint = await h.store.load_checkpoint(planned.run_id)
        assert checkpoint is not None and checkpoint.verification is not None
        assert checkpoint.evidence.checks == ()
        implemented = await h.service.implement_plan(planned.run_id)
        assert implemented.status is RunStatus.STOPPED
        assert implemented.gate_reason == "required_check_not_run"
    finally:
        await h.service.close()


async def test_restart_pins_requirements_and_rejects_drift_before_consuming_an_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path)
    config = repo / ".haven.toml"
    body = "[verification_groups]\nquality = ['test', 'lint']\n" + "".join(
        f"[recipes.{name}]\nargv = {json.dumps(list(spec.argv))}\ntimeout_seconds = 30\n"
        for name, spec in recipes().items()
    )
    config.write_text(body)
    monkeypatch.setattr(bootstrap, "select_launcher", lambda: RecordingLauncher())
    database = tmp_path / "sessions.db"

    async def build(model: ScriptedModel):  # type: ignore[no-untyped-def]
        return await bootstrap.build_services(
            repo,
            model=model,
            store_path=database,
            worktree=False,
            approvals=AutoApprover("approve_all"),
            sinks=[],
            mode=PermissionMode.INTERACTIVE,
        )

    first = await build(ScriptedModel([check_turn("test"), ask_turn()]))
    try:
        waiting = await first.run_service.run("Verify with a question", check_group="quality")
        assert waiting.pending_input is not None
        frozen = await first.store.load_checkpoint(waiting.run_id)
    finally:
        await first.close()
    config.write_text(body.replace("timeout_seconds = 30", "timeout_seconds = 60"))
    drift_model = ScriptedModel([])
    changed = await build(drift_model)
    response = UserInputResponse(
        request_id=waiting.pending_input.request_id, answers={"scope": "CLI"}
    )
    try:
        with pytest.raises(ValueError, match="missing or changed"):
            await changed.run_service.answer_run(waiting.run_id, response)
        assert drift_model.requests_seen == []
        assert await changed.store.load_checkpoint(waiting.run_id) == frozen
    finally:
        await changed.close()
    # 删除组名也不能降低旧任务的要求；固定命令仍一致时，旧检查继续有效。
    config.write_text("[recipes.test]" + body.split("[recipes.test]", 1)[1])
    final_model = ScriptedModel(
        [check_turn("lint", "lint-after-resume"), [text("Verified."), finish()]]
    )
    restored = await build(final_model)
    try:
        assert restored.config.verification_groups == {}
        outcome = await restored.run_service.answer_run(waiting.run_id, response)
        assert outcome.status is RunStatus.SUCCEEDED
        saved = await restored.store.load_checkpoint(waiting.run_id)
        assert saved is not None and frozen is not None
        assert saved.verification == frozen.verification
        assert {check.recipe_id for check in saved.evidence.checks} == {"test", "lint"}
    finally:
        await restored.close()
