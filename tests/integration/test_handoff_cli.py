"""CLI 明确准备、导出和修订笔记；不建模型服务，不覆盖并发修订。"""

import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from haven import bootstrap
from haven.adapters.sqlite_session import SqliteSessionStore
from haven.application.handoff import view_handoff
from haven.contracts.events import StepStarted
from haven.contracts.tools import PlanStep
from haven.domain.enums import RunStatus
from haven.interfaces.cli import app
from tests.contract.test_session_store import checkpoint


def test_prepare_export_revise_and_clear_preserve_facts_and_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def open_store() -> SqliteSessionStore:
        return await SqliteSessionStore.open(tmp_path / "sessions.db", tmp_path / "artifacts")

    async def seed() -> None:
        store = await open_store()
        try:
            await store.create_run("run-1", str(tmp_path), "ws", "task", "interactive")
            await store.append_event("run-1", StepStarted(run_id="run-1", step=1))
            await store.save_checkpoint(
                checkpoint(last_seq=1).model_copy(
                    update={
                        "plan": (
                            PlanStep(title="Preserve API", status="done"),
                            PlanStep(title="Run lint", status="pending"),
                        )
                    }
                )
            )
            await store.update_run_status("run-1", RunStatus.STOPPED, "stopped")
        finally:
            await store.close()

    asyncio.run(seed())
    monkeypatch.setattr(bootstrap, "open_store", open_store)

    def forbidden(*args, **kwargs):
        raise AssertionError("handoff commands must not build a model service")

    monkeypatch.setattr(bootstrap, "build_services", forbidden)
    runner = CliRunner()
    prepared = runner.invoke(app, ["handoff", "run-1", "--prepare"])
    assert prepared.exit_code == 0, prepared.output
    assert "plan_draft" in prepared.stdout and "UNTRUSTED" in prepared.stdout
    target = tmp_path / "notes.json"
    exported = runner.invoke(app, ["handoff", "run-1", "--export", str(target)])
    assert exported.exit_code == 0
    notes = json.loads(target.read_text())
    assert notes["completed"] == "Preserve API" and notes["next_steps"] == "Run lint"
    notes["decisions"] = "Keep aliases because callers use them"
    target.write_text(json.dumps(notes))
    missing_seq = runner.invoke(app, ["handoff", "run-1", "--set", str(target)])
    assert missing_seq.exit_code == 2
    saved = runner.invoke(app, ["handoff", "run-1", "--set", str(target), "--expected-seq", "2"])
    assert saved.exit_code == 0 and "callers use them" in saved.stdout
    stale = runner.invoke(app, ["handoff", "run-1", "--set", str(target), "--expected-seq", "2"])
    assert stale.exit_code == 2 and "reopen" in stale.stdout
    history = runner.invoke(app, ["handoff", "run-1", "--history"])
    assert history.exit_code == 0 and "Event 2" in history.stdout and "Event 3" in history.stdout
    duplicate_export = runner.invoke(app, ["handoff", "run-1", "--export", str(target)])
    assert duplicate_export.exit_code == 2
    cleared = runner.invoke(app, ["handoff", "run-1", "--clear"])
    assert cleared.exit_code == 0 and "no saved handoff" in cleared.stdout

    async def inspect() -> None:
        store = await open_store()
        try:
            view = await view_handoff(store, "run-1")
            assert view.snapshot is None and view.checkpoint.last_seq == 1
            assert view.checkpoint.evidence.checks == ()
            assert len(await store.load_events("run-1")) == 4
        finally:
            await store.close()

    asyncio.run(inspect())
