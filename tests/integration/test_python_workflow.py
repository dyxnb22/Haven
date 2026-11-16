"""一个已准备好 pytest/Ruff 环境的 Python 工程：发现到固定验证与显式审阅。"""

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from haven import bootstrap
from haven.adapters.providers.scripted import ScriptedModel
from haven.adapters.sqlite_session import SqliteSessionStore
from haven.interfaces.cli import app
from haven.ports.sandbox import default_readable_roots
from tests.integration.fakes import RecordingLauncher
from tests.integration.harness import finish, text, tool

BROKEN = "import json\n\n\ndef add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"


def python_project(tmp_path: Path) -> Path:
    repo = tmp_path / "project"
    (repo / "src/demo_calc").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src/demo_calc/__init__.py").write_text(BROKEN)
    (repo / "tests/test_calc.py").write_text(
        "import os\n\nfrom demo_calc import add\n\n\ndef test_add():\n"
        "    assert add(2, 3) == 5\n\n\ndef test_environment():\n"
        "    assert os.environ.get('HAVEN_WORKFLOW_SECRET') is None\n"
    )
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'haven-python-demo'\nversion = '0.1.0'\n"
        "requires-python = '>=3.12'\n[tool.pytest.ini_options]\n"
        "testpaths = ['tests']\npythonpath = ['src']\n"
        "[tool.ruff]\nline-length = 100\n"
    )
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n.ruff_cache/\n.haven/\n")
    return repo


def test_python_project_from_discovery_to_real_checks_and_explicit_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = python_project(tmp_path)
    launcher = RecordingLauncher()
    monkeypatch.setattr(bootstrap, "select_launcher", lambda: launcher)
    runner = CliRunner()
    preview = runner.invoke(app, ["init", "-w", str(repo)])
    assert preview.exit_code == 0, preview.output
    assert "[recipes.pytest]" in preview.stdout and "[recipes.ruff]" in preview.stdout
    assert not (repo / ".haven.toml").exists()
    accepted = runner.invoke(app, ["init", "-w", str(repo), "--accept"])
    assert accepted.exit_code == 0, accepted.output
    config = repo / ".haven.toml"
    config.write_text(
        config.read_text() + "\n[verification_groups]\nquality = ['pytest', 'ruff']\n"
    )
    existing = config.read_text()
    repeated = runner.invoke(app, ["discover", "-w", str(repo), "--accept"])
    assert repeated.exit_code == 0 and config.read_text() == existing

    for argv in [
        ["git", "init", "-q"],
        ["git", "add", "."],
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "fixture",
        ],
    ]:
        subprocess.run(argv, cwd=repo, check=True, capture_output=True)

    # 初始化、检查与发现均不启动项目命令或解释器，也不安装依赖。
    with monkeypatch.context() as patch:

        def forbidden(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("inspection must not execute commands")

        patch.setattr(subprocess, "run", forbidden)
        patch.setattr(asyncio, "create_subprocess_exec", forbidden)
        doctor = runner.invoke(app, ["doctor", "-w", str(repo)])
        assert doctor.exit_code == 0, doctor.output
        assert "ruff module found" in doctor.stdout and "pytest module found" in doctor.stdout
        discover = runner.invoke(app, ["discover", "-w", str(repo)])
        assert discover.exit_code == 0, discover.output
        inspected = runner.invoke(app, ["config", "explain", "-w", str(repo)])
        assert inspected.exit_code == 0
        assert str(config) in inspected.stdout
        assert "execution.cwd" in inspected.stdout and "execution.env.forwarded" in inspected.stdout
        assert "recipes.ruff.network" in inspected.stdout and "denied" in inspected.stdout
        assert "recipes.ruff.readable_roots" in inspected.stdout
    model = ScriptedModel(
        [
            [tool("test-before", "repo.check", recipe_id="pytest"), finish("tool_calls")],
            [tool("lint-before", "repo.check", recipe_id="ruff"), finish("tool_calls")],
            [tool("read", "repo.read", path="src/demo_calc/__init__.py"), finish("tool_calls")],
            [
                tool(
                    "fix",
                    "repo.edit",
                    path="src/demo_calc/__init__.py",
                    old_string=BROKEN,
                    new_string=FIXED,
                ),
                finish("tool_calls"),
            ],
            [tool("diff", "repo.diff"), finish("tool_calls")],
            [tool("test-after", "repo.check", recipe_id="pytest"), finish("tool_calls")],
            [tool("lint-after", "repo.check", recipe_id="ruff"), finish("tool_calls")],
            [text("Fixed and verified with pytest and Ruff."), finish()],
        ]
    )
    database = tmp_path / "sessions.db"
    build = bootstrap.build_services
    builds = []

    async def injected(workspace: Path, **kwargs: Any) -> bootstrap.AppServices:
        builds.append(workspace)
        return await build(workspace, model=model, store_path=database, **kwargs)

    async def open_store() -> SqliteSessionStore:
        return await SqliteSessionStore.open(database, bootstrap.artifacts_dir())

    monkeypatch.setattr(bootstrap, "build_services", injected)
    monkeypatch.setattr(bootstrap, "open_store", open_store)
    monkeypatch.setenv("HAVEN_WORKFLOW_SECRET", "private-workflow-marker")
    journal = tmp_path / "events.jsonl"
    run = runner.invoke(
        app,
        [
            "run",
            "Fix add and remove the unused import",
            "-w",
            str(repo),
            "--write",
            "--approval-policy",
            "all",
            "--worktree",
            "--review",
            "--check-group",
            "quality",
            "--json",
            "--events",
            str(journal),
        ],
    )
    assert run.exit_code == 0, run.output
    outcome = json.loads(run.stdout)
    assert outcome["verification"] == {
        "group": "quality",
        "checks": [
            {"recipe_id": "pytest", "status": "passed"},
            {"recipe_id": "ruff", "status": "passed"},
        ],
    }
    assert outcome["review_required"]
    events = [json.loads(line)["event"] for line in journal.read_text().splitlines()]
    root = Path(next(event["workspace"] for event in events if event["kind"] == "run.created"))
    checks = [
        event["check"]
        for event in events
        if event["kind"] == "evidence.recorded" and event["evidence_kind"] == "check"
    ]
    for recipe_id in ("pytest", "ruff"):
        assert [check["exit_code"] for check in checks if check["recipe_id"] == recipe_id] == [1, 0]
    assert root != repo and (root / "src/demo_calc/__init__.py").read_text() == FIXED
    assert (repo / "src/demo_calc/__init__.py").read_text() == BROKEN
    assert len(launcher.calls) == 4
    for argv, sandbox in launcher.calls:
        assert argv[:3] in (("python", "-m", "pytest"), ("python", "-m", "ruff"))
        assert sandbox.workspace_root == root and sandbox.allow_network is False
        assert sandbox.extra_readable_roots == default_readable_roots()
        assert not sandbox.scratch_dir.is_relative_to(root)
    before_test = (repo / "tests/test_calc.py").read_bytes()
    viewed = runner.invoke(app, ["review", outcome["run_id"], "-w", str(repo)])
    assert viewed.exit_code == 0 and "src/demo_calc/__init__.py" in viewed.stdout
    assert (repo / "src/demo_calc/__init__.py").read_text() == BROKEN
    applied = runner.invoke(
        app,
        [
            "review",
            outcome["run_id"],
            "-w",
            str(repo),
            "--apply",
            "--binding",
            outcome["review_binding"],
        ],
    )
    assert applied.exit_code == 0, applied.output
    assert (repo / "src/demo_calc/__init__.py").read_text() == FIXED
    assert (repo / "tests/test_calc.py").read_bytes() == before_test
    assert len(builds) == 1
