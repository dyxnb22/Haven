"""Evidence 显示早期错误，日志窗口只读分页，关闭后可继续操作。"""

from pathlib import Path

import pytest
from textual.widgets import Static, TabbedContent

from haven import bootstrap
from haven.adapters.providers.scripted import ScriptedModel
from haven.contracts.events import EventEnvelope, EvidenceRecorded, RunCreated, ToolCompleted
from haven.contracts.process_output import ProcessReport
from haven.interfaces.tui.app import HavenApp
from haven.interfaces.tui.presenter import PresenterState, reduce
from haven.interfaces.tui.process_output import PAGE_CHARS, ProcessOutputScreen
from haven.interfaces.tui.rendering import panel_text
from tests.integration.fakes import RecordingLauncher
from tests.integration.harness import finish, make_repo, text, tool
from tests.tui.test_tui_journey import _approve_pending, _settle, _submit, _wait_ready
from tests.tui.test_verification_groups import builder


def test_evidence_overview_bounds_history_and_escapes_untrusted_diagnostics() -> None:
    state = PresenterState()
    report = ProcessReport(
        cwd="/workspace",
        exit_code=1,
        output_digest="a" * 64,
        diagnostics=("src/a.py:1:1 [b]error[/b]\x1b\x07",) * 25,
    )
    for index in range(100):
        state = reduce(
            state,
            EventEnvelope(
                seq=index,
                at="t",
                event=ToolCompleted(
                    run_id="run",
                    call_id=str(index),
                    tool_name="repo.exec",
                    status="ok",
                    process=report,
                ),
            ),
        )
        state = reduce(
            state,
            EventEnvelope(
                seq=index,
                at="t",
                event=EvidenceRecorded(run_id="run", evidence_kind="check", summary=str(index)),
            ),
        )
    assert len(state.process_results) == 10 and len(state.evidence_rows) == 50
    rendered = panel_text(state).evidence
    assert len(rendered) < 30_000 and "\x1b" not in rendered and "\x07" not in rendered
    assert "\\[b]" in rendered
    state = reduce(
        state,
        EventEnvelope(
            seq=101,
            at="t",
            event=RunCreated(
                run_id="next",
                workspace="/workspace",
                workspace_digest="d",
                goal="next",
                mode="interactive",
                model_name="m",
            ),
        ),
    )
    assert state.process_results == ()


async def test_process_output_is_locatable_paginated_and_never_runs_a_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bootstrap, "select_launcher", lambda: RecordingLauncher())
    repo = make_repo(tmp_path)
    (repo / ".haven.toml").write_text(
        "[recipes.lint]\nargv = ['python', '-c', \"print('F401 unused import'); "
        "print(' --> src/calc.py:1:8'); print('noise' * 20000); raise SystemExit(1)\"]\n"
    )
    model = ScriptedModel(
        [
            [tool("lint", "repo.check", recipe_id="lint"), finish("tool_calls")],
            [text("Inspected"), finish()],
        ]
    )
    app = HavenApp(workspace=repo, services_builder=builder(tmp_path / "sessions.db", model))
    async with app.run_test(size=(110, 40)) as pilot:
        await _wait_ready(app, pilot)
        await _submit(app, pilot, "Inspect lint output")
        await _approve_pending(app, pilot, "a", 1)
        await _settle(pilot, 15)
        evidence = panel_text(app._state).evidence
        assert "src/calc.py:1:8" in evidence and "/output CALL_ID" in evidence
        count = len(model.requests_seen)
        await _submit(app, pilot, "/output")
        assert app.query_one("#tabs", TabbedContent).active == "tab-evidence"
        await _submit(app, pilot, "/output lint")
        await _settle(pilot, 4)
        assert isinstance(app.screen, ProcessOutputScreen)
        assert "src/calc.py:1:8" in str(app.screen.query_one("#output-text", Static).content)
        assert len(str(app.screen.query_one("#output-text", Static).content)) <= PAGE_CHARS + 100
        await pilot.click("#output-next")
        assert app.screen._page == 1
        await pilot.click("#output-prev")
        assert app.screen._page == 0
        await pilot.press("escape")
        await _submit(app, pilot, "/output missing")
        await _settle(pilot, 3)
        assert "output unavailable" in app._state.timeline[-1].text
        assert len(model.requests_seen) == count
        await _submit(app, pilot, "/new")
        assert app._state.process_results == ()
