"""隔离运行必须在真实 TUI 审阅确认后才能写回主工作区。"""

from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Select, Static

from haven import bootstrap
from haven.adapters.providers.scripted import ScriptedModel
from haven.domain.enums import ApprovalMode, PermissionMode
from haven.interfaces.tui.app import HavenApp
from haven.interfaces.tui.diff_review import DiffReviewScreen
from tests.integration.fakes import RecordingLauncher
from tests.integration.test_diff_review import edit_turns
from tests.tui.test_tui_journey import _settle, _submit, _wait_ready


async def test_tui_review_can_keep_inspect_and_apply(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bootstrap, "select_launcher", lambda: RecordingLauncher())

    async def builder(workspace: Path, **kwargs: Any) -> bootstrap.AppServices:
        return await bootstrap.build_services(
            workspace,
            model=ScriptedModel(edit_turns("calc.py", "verify")),
            store_path=tmp_path / "review.db",
            mode=PermissionMode.INTERACTIVE,
            worktree=True,
            **kwargs,
        )

    app = HavenApp(workspace=git_repo, services_builder=builder, approval_mode=ApprovalMode.FULL)
    async with app.run_test(size=(110, 45)) as pilot:
        await _wait_ready(app, pilot)
        await _submit(app, pilot, "fix add")
        for _ in range(100):
            await pilot.pause()
            if isinstance(app.screen, DiffReviewScreen):
                break
        assert isinstance(app.screen, DiffReviewScreen)
        assert "return a - b" in (git_repo / "calc.py").read_text()
        assert app.screen.query_one("#review-file", Select).value == "calc.py"
        app.screen.query_one("#review-hunk", Select).value = 0
        await _settle(pilot, 3)
        assert "+    return a + b" in str(app.screen.query_one("#review-diff", Static).render())
        await pilot.click("#review-keep")
        await _settle(pilot, 5)
        assert app._state.review_pending
        run_id = app._state.run_id
        await _submit(app, pilot, "/new")
        assert app._state.run_id == run_id
        await _submit(app, pilot, "/review")
        await _settle(pilot, 5)
        assert isinstance(app.screen, DiffReviewScreen)
        await pilot.click("#review-apply")
        await _settle(pilot, 10)
        assert "return a + b" in (git_repo / "calc.py").read_text()
        assert not app._state.review_pending
