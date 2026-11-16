"""运行效率汇总读取真实持久化记录，缺失的旧字段不会被视为零。"""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from haven import bootstrap
from haven.adapters.memory_session import MemorySessionStore
from haven.adapters.providers.scripted import ScriptedModel
from haven.application.approvals import AutoApprover
from haven.contracts.events import ContextBuilt, ContextSegment, ModelCompleted, StepStarted
from haven.domain.enums import PermissionMode, RunStatus
from haven.interfaces.cli import app
from haven.interfaces.run_stats import run_stats_text
from tests.integration.fakes import RecordingLauncher
from tests.integration.harness import finish, make_repo, text, tool, usage


def test_stats_explains_a_slow_local_check_and_distinguishes_read_ranges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path)
    argv = json.dumps([sys.executable, "-c", "import time; time.sleep(0.05)"])
    (repo / ".haven.toml").write_text(f"[recipes.verify]\nargv = {argv}\n")
    monkeypatch.setattr(bootstrap, "select_launcher", lambda: RecordingLauncher())
    model = ScriptedModel(
        [
            [tool("a", "repo.read", path="src/calc.py"), usage(), finish("tool_calls")],
            [
                tool("b", "repo.read", path="src/calc.py", start_line=1),
                usage(),
                finish("tool_calls"),
            ],
            [
                tool("c", "repo.read", path="src/calc.py", start_line=2),
                usage(),
                finish("tool_calls"),
            ],
            [tool("verify", "repo.check", recipe_id="verify"), usage(), finish("tool_calls")],
            [text("Inspected and checked."), finish()],
        ]
    )

    async def seed() -> str:
        services = await bootstrap.build_services(
            repo,
            mode=PermissionMode.INTERACTIVE,
            model=model,
            approvals=AutoApprover("approve_all"),
            sinks=[],
            worktree=False,
        )
        try:
            outcome = await services.runtime.start_run("Inspect and verify")
            assert outcome.status is RunStatus.SUCCEEDED
            return outcome.run_id
        finally:
            await services.close()

    run_id = asyncio.run(seed())
    runner = CliRunner()
    report = runner.invoke(app, ["sessions", "stats", run_id])
    assert report.exit_code == 0, report.output
    assert "5 completed / 5 started steps" in report.stdout
    assert "4 timed / 4 completed / 4 proposed" in report.stdout
    tool_rows = report.stdout.split("Tool pipeline ", 1)[1].splitlines()
    assert "repo.check (ok)" in tool_rows[1]
    assert "Reads 3 successful, 3 comparable, 1 repeated" in report.stdout
    assert "'src/calc.py' from line 1, up to 400 lines, 2 reads" in report.stdout
    assert "Context 5 builds" in report.stdout
    assert "Recorded active time" in report.stdout
    assert "includes estimates" in report.stdout and "Cost unknown" in report.stdout
    assert len(model.requests_seen) == 5
    missing = runner.invoke(app, ["sessions", "stats", "does-not-exist"])
    assert missing.exit_code == 2 and "run not found" in missing.stdout


async def test_stats_labels_partial_history_and_counts_observed_compaction() -> None:
    store = MemorySessionStore()
    run_id = "incomplete"
    await store.create_run(run_id, "/workspace", "digest", "Inspect files", "read_only")
    await store.update_run_status(run_id, RunStatus.RUNNING_MODEL, "")
    await store.append_event(run_id, StepStarted(run_id=run_id, step=1))
    await store.append_event(
        run_id,
        ModelCompleted(
            run_id=run_id,
            step=1,
            text="partial",
            tool_call_count=0,
            input_tokens=0,
            output_tokens=0,
            usage_estimated=False,
            ttft_ms=10,
            duration_ms=20,
            finish_reason="stop",
        ),
    )
    await store.append_event(run_id, StepStarted(run_id=run_id, step=2))
    await store.append_event(
        run_id,
        ContextBuilt(
            run_id=run_id,
            step=2,
            total_bytes=1000,
            segments=(
                ContextSegment(
                    source="run_digest", trust="trusted", size_bytes=30, reason="compacted"
                ),
            ),
        ),
    )

    report = await run_stats_text(store, run_id)
    assert "20 ms, 1 completed / 2 started steps" in report
    assert "Context 1 builds, 1 with a digest or budget truncation" in report
    assert "Largest 1,000 bytes, event 4, step 2" in report
    assert "Usage missing on 1 completed call(s)" in report and "Cost unknown" in report
