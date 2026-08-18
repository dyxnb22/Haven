"""使用 Textual Pilot 和 ScriptedModel 的 TUI 流程测试（离线）。"""

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
        self.lease_warning = ""
        self.config = type("Cfg", (), {"budget": Budget()})()

    async def close(self) -> None:
        return None


def make_builder(repo: Path, turns: list[list[Any]]):  # type: ignore[no-untyped-def]
    async def _builder(*, workspace: Path, approvals: Any, sinks: list[Any]) -> _Services:
        ws = FsWorkspace(repo)
        store = MemorySessionStore()
        # 使用无头 harness 相同的 launcher，使“TUI 和无头模式产生相同 trace”
        # 这一不变量能够进行同类比较。生产环境中两者都会经过 build_services
        # 并共享同一个后端。
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
        # 批准两个有副作用的操作（先 edit，再 check）
        await _approve_pending(app, pilot, "a", count=2)
        await _settle(pilot, 60)

        assert app._state.status == "succeeded"  # noqa: SLF001
        assert app._state.stop_reason == "evidence_satisfied"  # noqa: SLF001

    assert "return a + b" in (repo / "src" / "calc.py").read_text()


@pytest.mark.timeout(30)
async def test_rewind_command_restores_the_runs_edit(tmp_path: Path) -> None:
    """`/rewind` 分两步执行（先记录意图，再确认），并恢复已完成运行编辑过的文件；
    这是 ADR 0020 在 TUI 中体现的、默认安全失败的用户级撤销。"""
    repo = make_repo(tmp_path)
    buggy = (repo / "src" / "calc.py").read_text()
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
        await _approve_pending(app, pilot, "a", count=2)
        await _settle(pilot, 60)
        assert app._state.status == "succeeded"  # noqa: SLF001
        assert "return a + b" in (repo / "src" / "calc.py").read_text()

        # 第 1 步：没有确认时不发生任何变化，只说明意图。
        await _submit(app, pilot, "/rewind")
        await _settle(pilot, 10)
        assert "return a + b" in (repo / "src" / "calc.py").read_text()
        assert any(
            "rewind restores" in entry.text
            for entry in app._state.timeline  # noqa: SLF001
        )

        # 第 2 步：确认后，撤销本次运行的 edit。
        await _submit(app, pilot, "/rewind yes")
        await _settle(pilot, 40)
        assert (repo / "src" / "calc.py").read_text() == buggy
        assert any(
            "rewind complete" in entry.text
            for entry in app._state.timeline  # noqa: SLF001
        )


@pytest.mark.timeout(30)
async def test_follow_up_continues_the_same_session(tmp_path: Path) -> None:
    """运行结束后的第二个提示会继续原运行，因此模型保留第一轮的上下文，而不是从
    空白运行开始（第 2 阶段）。"""
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
async def test_apply_patch_journey_through_the_approval_flow(tmp_path: Path) -> None:
    """多文件补丁在 TUI 中只需要一次审批（ADR 0019）。稳定性流程验证：整个变更
    展示为一张卡片，批准后两个文件都会落盘。"""
    repo = make_repo(tmp_path)
    turns = [
        [tool("c1", "repo.read", path="src/calc.py"), finish("tool_calls")],
        [
            tool(
                "c2",
                "repo.apply_patch",
                operations=[
                    {
                        "kind": "edit",
                        "path": "src/calc.py",
                        "old_string": "return a - b  # BUG: should be +",
                        "new_string": "return a + b",
                    },
                    {"kind": "create", "path": "src/helper.py", "content": "H = 1\n"},
                ],
                summary="fix add and add a helper",
            ),
            finish("tool_calls"),
        ],
        [tool("c3", "repo.diff"), finish("tool_calls")],
        [tool("c4", "repo.check", recipe_id="verify-calc"), finish("tool_calls")],
        [text("Patched both files."), finish()],
    ]
    app = HavenApp(workspace=repo, services_builder=make_builder(repo, turns))

    async with app.run_test() as pilot:
        await _wait_ready(app, pilot)
        await _submit(app, pilot, "Fix add and add a helper module")
        # 恰好两次审批：补丁（两个文件共一张卡片），然后是 check。
        await _approve_pending(app, pilot, "a", count=2)
        await _settle(pilot, 60)
        assert app._state.status == "succeeded"  # noqa: SLF001

    assert "return a + b" in (repo / "src" / "calc.py").read_text()
    assert (repo / "src" / "helper.py").read_text() == "H = 1\n"


@pytest.mark.timeout(30)
async def test_steering_while_running_queues_instead_of_starting_a_run(tmp_path: Path) -> None:
    """ADR 0020 的稳定性流程：运行活跃时输入的内容会进入 steer 队列（下一轮投递），
    不会被拒绝，也不会启动新运行。"""
    repo = make_repo(tmp_path)
    steered: list[str] = []

    app = HavenApp(workspace=repo, services_builder=make_builder(repo, []))
    async with app.run_test() as pilot:
        await _wait_ready(app, pilot)

        # 以确定性方式强制进入“运行活跃”分支，然后确认提交会路由到
        # run_service.steer，而不是创建新运行。
        from dataclasses import replace

        app._state = replace(app._state, running=True)  # noqa: SLF001

        async def _fake_steer(text: str) -> bool:
            steered.append(text)
            return True

        app._services.run_service.steer = _fake_steer  # type: ignore[assignment]  # noqa: SLF001
        await _submit(app, pilot, "also update the README while you are at it")
        await _settle(pilot, 20)

        assert steered == ["also update the README while you are at it"]
        assert any("queued" in entry.text.lower() for entry in app._state.timeline)  # noqa: SLF001


@pytest.mark.timeout(30)
async def test_sessions_and_fork_commands(tmp_path: Path) -> None:
    """`/sessions` 列出之前的运行；`/fork` 从其中一个运行分支出下一条消息。"""
    from haven.contracts.events import RunCreated

    repo = make_repo(tmp_path)
    turns = [
        [text("ALPHA answer."), finish()],
        [text("branched from alpha."), finish()],
    ]
    app = HavenApp(workspace=repo, services_builder=make_builder(repo, turns))
    async with app.run_test() as pilot:
        await _wait_ready(app, pilot)
        await _submit(app, pilot, "first question")
        await _settle(pilot, 40)
        first_run_id = app._state.run_id  # noqa: SLF001
        assert first_run_id

        await _submit(app, pilot, "/sessions")
        await _settle(pilot, 10)
        assert any(first_run_id in e.text for e in app._state.timeline)  # noqa: SLF001

        await _submit(app, pilot, f"/fork {first_run_id}")
        await _settle(pilot, 5)
        await _submit(app, pilot, "take a different direction")
        await _settle(pilot, 40)
        forked = app._state.run_id  # noqa: SLF001
        assert forked and forked != first_run_id
        events = await app._services.store.load_events(forked)  # noqa: SLF001
        created = [e.event for e in events if isinstance(e.event, RunCreated)]
        assert created and created[0].parent_run_id == first_run_id


@pytest.mark.timeout(30)
async def test_mention_expands_into_the_goal(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    app = HavenApp(workspace=repo, services_builder=make_builder(repo, [[text("ok"), finish()]]))
    async with app.run_test() as pilot:
        await _wait_ready(app, pilot)
        expanded = app._expand_mentions("please look at @src/calc.py now")  # noqa: SLF001
        assert "mentioned files" in expanded and "src/calc.py" in expanded
        # 不存在的路径保持原样。
        assert app._expand_mentions("see @nope.py") == "see @nope.py"  # noqa: SLF001


@pytest.mark.timeout(30)
async def test_diff_command_switches_tab(tmp_path: Path) -> None:
    from textual.widgets import TabbedContent

    repo = make_repo(tmp_path)
    app = HavenApp(workspace=repo, services_builder=make_builder(repo, []))
    async with app.run_test() as pilot:
        await _wait_ready(app, pilot)
        await _submit(app, pilot, "/diff")
        await _settle(pilot, 5)
        assert app.query_one("#tabs", TabbedContent).active == "tab-diff"


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
