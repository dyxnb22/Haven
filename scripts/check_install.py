"""从 wheel 验证干净安装、历史数据库升级、继续运行及备份回退。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import AsyncIterator
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = "v0.1.0"
BUILD_FILES = ("pyproject.toml", "uv.lock", "README.md", "LICENSE")
ORIGINAL = "value = 1\n"
EDITED = "value = 2\n"
RECIPE = '[recipes.verify]\nargv = ["/bin/sh", "-c", "grep -qx \'value = 2\' sample.py"]\n'


def _run(*argv: str | Path, cwd: Path, env: dict[str, str] | None = None) -> None:
    subprocess.run(argv, cwd=cwd, env=env, check=True)


def _prepare_repo(repo: Path) -> None:
    repo.mkdir()
    _run("git", "init", "-q", cwd=repo)
    (repo / "sample.py").write_text(ORIGINAL, encoding="utf-8")
    (repo / ".haven.toml").write_text(RECIPE, encoding="utf-8")


def _install(source: Path, target: Path, python_version: str) -> Path:
    _run("uv", "build", "--wheel", "--out-dir", target / "wheel", cwd=source)
    (wheel,) = (target / "wheel").glob("*.whl")
    env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(target / "venv")}
    env.pop("VIRTUAL_ENV", None)
    _run(
        "uv",
        "sync",
        "--locked",
        "--no-default-groups",
        "--no-install-project",
        "--python",
        python_version,
        cwd=source,
        env=env,
    )
    bin_dir = target / "venv" / "bin"
    python = bin_dir / "python"
    _run("uv", "pip", "install", "--python", python, "--no-deps", wheel, cwd=target)
    _run("uv", "pip", "check", "--python", python, cwd=target)
    print(
        f"{target.name} wheel {wheel.name} sha256={hashlib.sha256(wheel.read_bytes()).hexdigest()}",
        flush=True,
    )
    return python


def _settings(config: Path, data: Path, python: Path) -> dict[str, str]:
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("HAVEN_") and name not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
    }
    path = os.get_exec_path()
    if sys.prefix != sys.base_prefix:
        path = [part for part in path if Path(part) != Path(sys.executable).parent]
    return {
        **env,
        "HAVEN_CONFIG_DIR": str(config),
        "HAVEN_DATA_DIR": str(data),
        "PYTHONNOUSERSITE": "1",
        "PATH": os.pathsep.join([str(python.parent), *path]),
        "HAVEN_BELL": "0",
    }


def _db_snapshot(data: Path, run_id: str) -> tuple[int, list[tuple[int, str, str]]]:
    with closing(sqlite3.connect(data / "haven.db")) as db:
        (version,) = db.execute("SELECT version FROM schema_meta").fetchall()
        events = db.execute(
            "SELECT seq, payload_json, payload_digest FROM events WHERE run_id=? ORDER BY seq",
            (run_id,),
        ).fetchall()
        return int(version[0]), events


async def _exercise(repo: Path, marker: Path, *, seed: bool) -> None:
    # 在选定 wheel 自己的解释器里导入，同一夹具覆盖发行版和候选版本。
    from importlib.metadata import version

    import haven
    from haven.adapters.providers.scripted import ScriptedModel
    from haven.adapters.sqlite_session import DB_SCHEMA_VERSION
    from haven.application.approvals import AutoApprover
    from haven.bootstrap import build_services
    from haven.config import data_dir
    from haven.contracts.model import (
        ModelEvent,
        ModelRequest,
        StreamFinished,
        TextDelta,
        ToolCallProposal,
        ToolCallReady,
    )
    from haven.domain.enums import PermissionMode, RunStatus, StopReason

    assert haven.__file__ and Path(haven.__file__).resolve().is_relative_to(Path(sys.prefix))
    assert haven.__version__ == version("haven")
    assert (repo / ".haven.toml").read_text(encoding="utf-8") == RECIPE

    def turn(call_id: str, name: str, **args: str) -> list[ToolCallReady | StreamFinished]:
        return [
            ToolCallReady(
                call=ToolCallProposal(
                    call_id=call_id,
                    tool_name=name,
                    arguments_json=json.dumps(args),
                )
            ),
            StreamFinished(finish_reason="tool_calls"),
        ]

    class Interrupted(Exception):
        pass

    class LegacyModel(ScriptedModel):
        def generate_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
            if len(self.requests_seen) == 2:
                raise Interrupted
            return super().generate_stream(request)

    model: ScriptedModel
    if seed:
        model = LegacyModel(
            [
                turn("read", "repo.read", path="sample.py"),
                turn("edit", "repo.edit", path="sample.py", old_string=ORIGINAL, new_string=EDITED),
            ]
        )
    else:
        model = ScriptedModel(
            [
                turn("diff", "repo.diff"),
                turn("verify", "repo.check", recipe_id="verify"),
                [TextDelta(text="Verified the saved edit."), StreamFinished(finish_reason="stop")],
            ]
        )

    services = await build_services(
        repo,
        model=model,
        mode=PermissionMode.INTERACTIVE,
        approvals=AutoApprover("approve_all"),
        sinks=[],
    )
    try:
        if seed:
            try:
                await services.run_service.run("Change the value to 2")
            except Interrupted:
                pass
            else:
                raise AssertionError("legacy run did not reach the interruption point")
            (run,) = await services.store.list_runs(1)
            run_id = run.run_id
            marker.write_text(run_id, encoding="utf-8")
        else:
            run_id = marker.read_text(encoding="utf-8")

        report = await services.recovery.inspect(run_id)
        assert report.can_resume, report.blockers
        checkpoint = report.checkpoint
        assert checkpoint is not None and checkpoint.usage.steps == 2
        assert (repo / "sample.py").read_text(encoding="utf-8") == EDITED
        assert (
            await services.store.get_artifact(checkpoint.original_artifacts["sample.py"])
            == ORIGINAL.encode()
        )
        if not seed:
            ctx = await services.recovery.build_context(checkpoint)
            outcome = await services.run_service.resume(ctx)
            assert outcome.run_id == run_id and outcome.status is RunStatus.SUCCEEDED, outcome
            assert outcome.stop_reason is StopReason.EVIDENCE_SATISFIED
            executions = await services.store.load_executions(run_id)
            assert sum(item.tool_name == "repo.edit" for item in executions) == 1
    finally:
        await services.close()
    current_schema, _ = _db_snapshot(data_dir(), run_id)
    assert current_schema == DB_SCHEMA_VERSION
    print(
        f"{'legacy checkpoint' if seed else 'resumed run'} {run_id} on schema {current_schema}",
        flush=True,
    )


def _verify_install(tmp: Path, old_python: Path, new_python: Path, cases: Path) -> None:
    old_haven, new_haven = (python.with_name("haven") for python in (old_python, new_python))
    repo, config, data, backup = (tmp / name for name in ("repo", "config", "data", "backup"))
    marker = tmp / "run-id"
    _prepare_repo(repo)
    old_env = _settings(config, data, old_python)
    env = _settings(config, data, new_python)
    _run(new_haven, "--version", cwd=tmp, env=env)
    _run(new_haven, "doctor", "--workspace", repo, cwd=tmp, env=env)
    assert not data.exists(), "doctor created a data directory"
    _run(
        new_haven,
        "eval",
        "--offline",
        "--cases",
        cases,
        "--out",
        tmp / "offline",
        cwd=tmp,
        env=env,
    )

    _run(old_python, "-I", __file__, "_seed", repo, marker, cwd=tmp, env=old_env)
    run_id = marker.read_text(encoding="utf-8")
    old_version, old_events = _db_snapshot(data, run_id)
    assert old_events
    shutil.copytree(data, backup)
    _run(new_haven, "sessions", "show", run_id, cwd=tmp, env=env)
    _run(new_python, "-I", __file__, "_resume", repo, marker, cwd=tmp, env=env)
    new_version, new_events = _db_snapshot(data, run_id)
    assert new_version > old_version and new_events[: len(old_events)] == old_events
    assert len(new_events) > len(old_events)
    _run(new_haven, "sessions", "stats", run_id, cwd=tmp, env=env)

    rollback_env = _settings(config, backup, old_python)
    _run(old_haven, "sessions", "show", run_id, cwd=tmp, env=rollback_env)
    assert _db_snapshot(backup, run_id) == (old_version, old_events)
    print(
        f"install smoke passed, SQLite schema {old_version} -> {new_version}, "
        f"backup reopens on {BASELINE}"
    )


def _smoke(tmp: Path, python_version: str) -> None:
    baseline, candidate = tmp / "baseline", tmp / "candidate"
    baseline.mkdir()
    archive = tmp / "baseline.tar"
    _run("git", "archive", "--format=tar", "-o", archive, BASELINE, *BUILD_FILES, "src", cwd=ROOT)
    with tarfile.open(archive) as archive_file:
        archive_file.extractall(baseline, filter="data")
    candidate.mkdir()
    for name in BUILD_FILES:
        shutil.copy2(ROOT / name, candidate / name)
    shutil.copytree(ROOT / "src", candidate / "src", ignore=shutil.ignore_patterns("__pycache__"))

    old_python = _install(baseline, tmp / "old", python_version)
    new_python = _install(candidate, tmp / "new", python_version)
    _verify_install(tmp, old_python, new_python, ROOT / "evals/cases")


def main() -> None:
    if len(sys.argv) == 4 and sys.argv[1] in {"_seed", "_resume"}:
        asyncio.run(_exercise(Path(sys.argv[2]), Path(sys.argv[3]), seed=sys.argv[1] == "_seed"))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--python", default="3.12", help="Python version for the clean wheel installs"
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="haven-install-check-") as name:
        _smoke(Path(name).resolve(), args.python)


if __name__ == "__main__":
    main()
