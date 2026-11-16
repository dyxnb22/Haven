"""配方静态依赖检查不执行仓库代码，不误认另一个虚拟环境。"""

import sys
from pathlib import Path

import pytest

from haven.contracts.tools import RecipeSpec
from haven.interfaces.cli_support import recipe_environment as environment


def test_relative_path_entries_and_commands_use_the_execution_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    program = tmp_path / "bin/check"
    program.parent.mkdir()
    program.write_text("#!/bin/sh\nexit 0\n")
    program.chmod(0o755)
    monkeypatch.setenv("PATH", "bin")
    assert environment.resolve_recipe_executable("check", tmp_path) == program
    assert environment.resolve_recipe_executable("bin/check", tmp_path) == program
    assert environment.resolve_recipe_executable("missing", tmp_path) is None
    program.chmod(0o644)
    assert environment.resolve_recipe_executable("bin/check", tmp_path) is None


def test_probe_ignores_project_shadow_modules_and_reports_missing_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "must-not-run"
    (tmp_path / "ruff.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    installed = tmp_path / "installed"
    installed.mkdir()
    monkeypatch.setattr(environment.sysconfig, "get_path", lambda name: str(installed))
    recipe = RecipeSpec(id="lint", argv=(sys.executable, "-m", "ruff", "check", "."))
    status = environment.python_tool_dependency(recipe, Path(sys.executable))
    assert status is not None and status[0] is False
    assert "missing" in status[1] and "prepare" in status[1]
    assert not marker.exists()
    # 找到已安装模块同样只读取元数据，不导入其正文。
    (installed / "ruff.py").write_text(f"raise RuntimeError({str(marker)!r})\n")
    status = environment.python_tool_dependency(recipe, Path(sys.executable))
    assert status is not None and status[0] is True
    assert not marker.exists()


def test_a_different_venv_is_unknown_even_when_it_links_to_the_same_python(tmp_path: Path) -> None:
    executable = tmp_path / "other-venv/bin/python"
    executable.parent.mkdir(parents=True)
    executable.symlink_to(sys.executable)
    recipe = RecipeSpec(id="test", argv=(str(executable), "-m", "pytest"))
    status = environment.python_tool_dependency(recipe, executable)
    assert status is not None and status[0] is None
    assert "unknown" in status[1]


def test_other_commands_are_not_imported_or_executed() -> None:
    recipe = RecipeSpec(id="custom", argv=(sys.executable, "-m", "project_command"))
    assert environment.python_tool_dependency(recipe, Path(sys.executable)) is None
