"""配方环境的只读诊断；不运行解释器、不导入项目代码或安装依赖。"""

import os
import shutil
import sys
import sysconfig
from importlib.machinery import PathFinder
from pathlib import Path

from haven.contracts.tools import RecipeSpec

_PYTHON_TOOLS = frozenset({"pytest", "ruff"})


def resolve_recipe_executable(command: str, workspace: Path) -> Path | None:
    if os.path.dirname(command):
        candidate = Path(command)
        if not candidate.is_absolute():
            candidate = workspace / candidate
        return candidate if candidate.is_file() and os.access(candidate, os.X_OK) else None
    # Popen 的 cwd 会影响 PATH 中的相对项，不能按 doctor 自身的目录解析。
    entries = []
    for item in os.environ.get("PATH", os.defpath).split(os.pathsep):
        path = Path(item)
        entries.append(str(path if path.is_absolute() else workspace / path))
    found = shutil.which(command, path=os.pathsep.join(entries))
    return Path(found) if found else None


def python_tool_dependency(recipe: RecipeSpec, executable: Path) -> tuple[bool | None, str] | None:
    if len(recipe.argv) < 3 or recipe.argv[1] != "-m" or recipe.argv[2] not in _PYTHON_TOOLS:
        return None
    module = recipe.argv[2]
    current = Path(sys.executable)
    # 同一个二进制也可能属于不同 venv；同时比较入口目录，避免误判依赖已存在。
    if (
        executable.resolve() != current.resolve()
        or executable.parent.resolve() != current.parent.resolve()
    ):
        return None, (
            f"{module}: availability unknown for {executable}; prepare and verify that "
            "interpreter's environment before running the recipe"
        )
    # 只检查当前环境的安装目录。PathFinder 不执行模块正文，也不会去 cwd
    # 中导入一个由仓库伪装的 pytest.py、ruff.py 或 sitecustomize.py。
    paths = list(
        dict.fromkeys(path for key in ("purelib", "platlib") if (path := sysconfig.get_path(key)))
    )
    spec = PathFinder.find_spec(module, paths)
    if spec is None:
        return False, (
            f"Python module {module!r} is missing from {executable}; "
            "prepare the project's dependencies in this interpreter, then rerun haven doctor"
        )
    return True, f"{module} module found in the environment of {executable} (metadata check)"
