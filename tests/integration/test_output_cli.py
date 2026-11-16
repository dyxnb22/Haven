"""从重开的存储回看日志；默认输出有界且无终端控制字符。"""

import asyncio
from pathlib import Path

import pytest
from typer.testing import CliRunner

from haven import bootstrap
from haven.adapters.sqlite_session import SqliteSessionStore
from haven.application.process_outputs import read_process_output
from haven.interfaces.cli import app
from tests.unit.test_process_outputs import completed_output


def test_output_lists_results_pages_safely_and_exports_only_an_explicit_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, artifacts = tmp_path / "sessions.db", tmp_path / "artifacts"

    async def open_store() -> SqliteSessionStore:
        return await SqliteSessionStore.open(database, artifacts)

    async def seed() -> str:
        store = await open_store()
        try:
            await completed_output(store, "run")
            return await read_process_output(store, "run", "check")
        finally:
            await store.close()

    original = asyncio.run(seed())
    monkeypatch.setattr(bootstrap, "open_store", open_store)
    runner = CliRunner()
    index = runner.invoke(app, ["output", "run"])
    assert index.exit_code == 0, index.output
    assert "check: repo.exec exit=1" in index.stdout and "haven output run check" in index.stdout
    page = runner.invoke(app, ["output", "run", "check", "--max-lines", "2"])
    assert page.exit_code == 0 and "--start-line 3" in page.stdout
    later = runner.invoke(app, ["output", "run", "check", "--start-line", "3"])
    assert "EARLY" in later.stdout
    target = tmp_path / "full.log"
    saved = runner.invoke(app, ["output", "run", "check", "--save", str(target)])
    assert saved.exit_code == 0 and target.read_text() == original
    duplicate = runner.invoke(app, ["output", "run", "check", "--save", str(target)])
    assert duplicate.exit_code == 2 and target.read_text() == original
    for arguments in (
        ["output", "other-run", "check"],
        ["output", "run", "../file"],
        ["output", "run", "--save", str(target)],
    ):
        missing = runner.invoke(app, arguments)
        assert missing.exit_code == 2


def test_console_sink_escapes_controls_and_bounds_untrusted_process_diagnostics(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from haven.contracts.events import EventEnvelope, ToolCompleted
    from haven.contracts.process_output import ProcessReport
    from haven.interfaces.cli_support.sinks import ConsoleSink

    report = ProcessReport(
        exit_code=1,
        cwd="/workspace",
        output_digest="a" * 64,
        diagnostics=("src/a.py:12: F401 \x1b[2J\x07[b]error[/b]",) * 25,
    )
    event = ToolCompleted(
        run_id="run", call_id="check", tool_name="repo.check", status="ok", process=report
    )
    asyncio.run(ConsoleSink().emit(EventEnvelope(seq=1, at="t", event=event)))
    output = capsys.readouterr().out
    assert "src/a.py:12" in output and "\x1b" not in output and "\x07" not in output
    assert output.count("F401") == 3 and len(output) < 4000


def test_output_display_bounds_a_single_long_line_and_preserves_exported_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from haven.application.process_outputs import save_process_output
    from haven.contracts.events import ToolCompleted
    from haven.ports.executor import CapturedOutput, ExecOutcome
    from tests.unit.test_maintenance import _seed_run

    async def open_store() -> SqliteSessionStore:
        return await SqliteSessionStore.open(tmp_path / "sessions.db", tmp_path / "artifacts")

    async def seed() -> None:
        store = await open_store()
        try:
            await _seed_run(store, "run")
            result = ExecOutcome(
                1, 1, "", "", False, False, CapturedOutput("\x1b[2J" + "x" * 40000, "")
            )
            report = await save_process_output(store, result, tmp_path)
            await store.append_event(
                "run",
                ToolCompleted(
                    run_id="run", call_id="log", tool_name="repo.exec", status="ok", process=report
                ),
            )
        finally:
            await store.close()

    asyncio.run(seed())
    monkeypatch.setattr(bootstrap, "open_store", open_store)
    runner = CliRunner()
    page = runner.invoke(app, ["output", "run", "log"])
    assert page.exit_code == 0 and len(page.stdout) < 21_000 and "\x1b" not in page.stdout
    assert "Line clipped" in page.stdout
    target = tmp_path / "retained.log"
    exported = runner.invoke(app, ["output", "run", "log", "--save", str(target)])
    assert exported.exit_code == 0 and "\x1b[2J" + "x" * 40000 in target.read_text()
