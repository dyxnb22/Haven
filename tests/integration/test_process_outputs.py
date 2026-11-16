"""长日志的早期错误可定位与回看；进程报告不会创造验收证据。"""

import sys
from pathlib import Path

import pytest

from haven.application.maintenance import collect_garbage
from haven.application.process_outputs import read_process_output
from haven.contracts.events import ToolCompleted
from haven.contracts.tools import RecipeSpec
from haven.domain.budget import Budget
from haven.domain.enums import RunStatus
from haven.interfaces.process_output import process_overview
from tests.integration.harness import Harness, finish, make_repo, text, tool


async def test_long_failed_check_surfaces_early_error_and_keeps_complete_output(
    tmp_path: Path,
) -> None:
    command = (
        "import sys; print('F401 [*] unused import'); "
        "print(' --> src/calc.py:7:8'); print('noise\\n' * 40000); "
        "print('LAST_OUTPUT'); sys.exit(1)"
    )
    recipes = {
        name: RecipeSpec(id=name, argv=(sys.executable, "-c", command)) for name in ("lint", "test")
    }
    h = Harness(
        make_repo(tmp_path),
        [
            [tool("check", "repo.check", recipe_id="lint"), finish("tool_calls")],
            [text("Done"), finish()],
        ],
        recipes=recipes,
        verification_groups={"quality": ("lint", "test")},
        budget=Budget(max_steps=2),
    )
    outcome = await h.service.run("Check the project", check_group="quality")
    assert outcome.status is not RunStatus.SUCCEEDED
    completed = [
        event for event in h.sink.events_of("tool.completed") if isinstance(event, ToolCompleted)
    ]
    report = completed[0].process
    assert report is not None and report.recipe_id == "lint"
    assert report.exit_code == 1 and report.output_truncated is False
    assert completed[0].truncated  # 模型原有的尾部观察仍有界。
    assert "src/calc.py:7:8" in report.diagnostics[0]
    original = await read_process_output(h.store, outcome.run_id, "check")
    assert "F401" in original and "LAST_OUTPUT" in original and original.count("noise") == 40000
    overview = await process_overview(h.store, outcome.run_id)
    assert "lint: failed" in overview and "test: not_run" in overview
    assert "src/calc.py:7:8" in overview and len(overview) < 4000
    await collect_garbage(h.store, keep=1, apply=True)
    assert await read_process_output(h.store, outcome.run_id, "check") == original
    with pytest.raises(ValueError, match="no saved process output"):
        await read_process_output(h.store, "another-run", "check")


@pytest.mark.parametrize("exit_code", [0, 1])
async def test_exec_output_and_diagnostics_never_count_as_a_required_check(
    tmp_path: Path, exit_code: int
) -> None:
    h = Harness(
        make_repo(tmp_path),
        [
            [
                tool(
                    "exec",
                    "repo.exec",
                    argv=[
                        sys.executable,
                        "-c",
                        f"print('ERROR: example'); raise SystemExit({exit_code})",
                    ],
                ),
                finish("tool_calls"),
            ],
            [text("Verified"), finish()],
        ],
        verification_groups={"quality": ("verify-calc",)},
        budget=Budget(max_steps=2),
    )
    outcome = await h.service.run("Inspect the project", check_group="quality")
    assert outcome.status is not RunStatus.SUCCEEDED
    checkpoint = await h.store.load_checkpoint(outcome.run_id)
    assert checkpoint is not None and checkpoint.evidence.checks == ()
    assert "verify-calc: not_run" in await process_overview(h.store, outcome.run_id)
    completed = next(
        event for event in h.sink.events_of("tool.completed") if isinstance(event, ToolCompleted)
    )
    assert completed.process is not None and completed.process.recipe_id == ""
    assert completed.process.exit_code == exit_code
    assert "ERROR: example" in await read_process_output(h.store, outcome.run_id, "exec")
