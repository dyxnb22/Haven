"""TUI journey tests using Textual's Pilot with a ScriptedModel (offline)."""

from pathlib import Path
from typing import Any

import pytest

from haven.adapters.memory_session import MemorySessionStore
from haven.adapters.process_executor import ProcessExecutor
from haven.adapters.providers.scripted import ScriptedModel
from haven.adapters.workspace_fs import FsWorkspace
from haven.application.emitter import EventEmitter
from haven.application.recovery_service import RecoveryService
from haven.application.replay_service import ReplayService
from haven.application.run_service import RunService
from haven.domain.budget import Budget
from haven.domain.enums import PermissionMode
from haven.interfaces.tui.app import HavenApp
from tests.integration.fakes import RecordingLauncher
from tests.integration.harness import (
    BUGGY_CALC,
    default_recipes,
    finish,
    make_repo,
    text,
    tool,
)


class _Services:
    def __init__(self, run_service: RunService, store: MemorySessionStore) -> None:
        self.run_service = run_service
        self.store = store
        self.recovery = RecoveryService(store, run_service._workspace)  # noqa: SLF001
        self.replay = ReplayService(store)
        self.git_branch = "main"
        self.model_name = "scripted"
        self.config = type("Cfg", (), {"budget": Budget()})()

    async def close(self) -> None:
        return None


def make_builder(repo: Path, turns: list[list[Any]]):  # type: ignore[no-untyped-def]
    async def _builder(*, workspace: Path, approvals: Any, sinks: list[Any]) -> _Services:
        ws = FsWorkspace(repo)
        store = MemorySessionStore()
        # The same launcher the headless harness uses, so the "TUI and headless
        # produce the same trace" invariant compares like with like. In
        # production both go through build_services and share one backend.
        launcher = RecordingLauncher()
        service = RunService(
            model=ScriptedModel(turns),
            workspace=ws,
            executor=ProcessExecutor(launcher=launcher),
            store=store,
            emitter=EventEmitter(store, sinks),
            approvals=approvals,
            recipes=default_recipes(),
            mode=PermissionMode.INTERACTIVE,
            budget=Budget(),
            launcher=launcher,
        )
        return _Services(service, store)

    return _builder


async def _settle(pilot: Any, tries: int = 60) -> None:
    for _ in range(tries):
        await pilot.pause()


async def _wait_ready(app: HavenApp, pilot: Any, tries: int = 100) -> None:
    for _ in range(tries):
        await pilot.pause()
        if app._services is not None:  # noqa: SLF001
            return
    raise AssertionError("services never finished bootstrapping")


async def _submit(app: HavenApp, pilot: Any, value: str) -> None:
    prompt = app.query_one("#prompt")
    prompt.focus()  # type: ignore[attr-defined]
    prompt.value = value  # type: ignore[attr-defined]
    await pilot.pause()
    await pilot.press("enter")


async def _approve_pending(app: HavenApp, pilot: Any, decision: str, count: int) -> None:
    approved = 0
    for _ in range(200):
        await pilot.pause()
        if app.screen.__class__.__name__ == "ApprovalScreen":
            await pilot.press(decision)
            approved += 1
            if approved >= count:
                return


@pytest.mark.timeout(30)
async def test_edit_journey_with_approval(tmp_path: Path) -> None:
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
                summary="fix add()",
            ),
            finish("tool_calls"),
        ],
        [tool("c3", "repo.diff"), finish("tool_calls")],
        [tool("c4", "repo.check", recipe_id="verify-calc"), finish("tool_calls")],
        [text("Fixed and verified."), finish()],
    ]
    app = HavenApp(workspace=repo, services_builder=make_builder(repo, turns))

    async with app.run_test() as pilot:
        await _wait_ready(app, pilot)
        await _submit(app, pilot, "Fix the bug in add()")
        # approve the two side-effecting actions (edit, then check)
        await _approve_pending(app, pilot, "a", count=2)
        await _settle(pilot, 60)

        assert app._state.status == "succeeded"  # noqa: SLF001
        assert app._state.stop_reason == "evidence_satisfied"  # noqa: SLF001

    assert "return a + b" in (repo / "src" / "calc.py").read_text()


@pytest.mark.timeout(30)
async def test_follow_up_continues_the_same_session(tmp_path: Path) -> None:
    """A second prompt after a finished run continues it, so the model keeps
    the first turn's context instead of starting from a blank run (Phase 2)."""
    from haven.contracts.events import RunCreated

    repo = make_repo(tmp_path)
    turns = [
        [text("First answer."), finish()],
        [text("Second answer, building on the first."), finish()],
    ]
    app = HavenApp(workspace=repo, services_builder=make_builder(repo, turns))

    async with app.run_test() as pilot:
        await _wait_ready(app, pilot)
        await _submit(app, pilot, "Explain calc.py")
        await _settle(pilot, 40)
        first_run_id = app._state.run_id  # noqa: SLF001
        assert app._state.status == "succeeded"  # noqa: SLF001

        await _submit(app, pilot, "Now suggest a fix")
        await _settle(pilot, 40)
        second_run_id = app._state.run_id  # noqa: SLF001

        assert second_run_id and second_run_id != first_run_id
        events = await app._services.store.load_events(second_run_id)  # noqa: SLF001
        created = [e.event for e in events if isinstance(e.event, RunCreated)]
        assert created and created[0].parent_run_id == first_run_id


@pytest.mark.timeout(30)
async def test_reject_journey_leaves_file_untouched(tmp_path: Path) -> None:
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
        [text("Rejected; no changes."), finish()],
    ]
    app = HavenApp(workspace=repo, services_builder=make_builder(repo, turns))

    async with app.run_test() as pilot:
        await _wait_ready(app, pilot)
        await _submit(app, pilot, "Fix add()")
        await _approve_pending(app, pilot, "r", count=1)
        await _settle(pilot, 60)
        assert app._state.status == "succeeded"  # noqa: SLF001

    assert (repo / "src" / "calc.py").read_text() == BUGGY_CALC


@pytest.mark.timeout(30)
async def test_help_command_writes_to_timeline(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    app = HavenApp(workspace=repo, services_builder=make_builder(repo, []))
    async with app.run_test() as pilot:
        await _wait_ready(app, pilot)
        await _submit(app, pilot, "/help")
        await _settle(pilot, 5)
        assert any("Commands:" in entry.text for entry in app._state.timeline)  # noqa: SLF001


@pytest.mark.timeout(30)
async def test_tab_switching(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    app = HavenApp(workspace=repo, services_builder=make_builder(repo, []))
    async with app.run_test() as pilot:
        await _wait_ready(app, pilot)
        await pilot.press("f2")
        await pilot.pause()
        from textual.widgets import TabbedContent

        assert app.query_one("#tabs", TabbedContent).active == "tab-diff"
        await pilot.press("f4")
        await pilot.pause()
        assert app.query_one("#tabs", TabbedContent).active == "tab-trace"
