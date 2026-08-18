"""根据项目自身文件提出验证配方。

全新的仓库没有 `.haven.toml`，因此 Evidence Gate 没有可要求的检查，每次编辑
都会以 `verification_unavailable` 结束。本模块读取人类通常会识别的项目文件——
`pyproject.toml`、`tox.ini`、`setup.cfg`、`package.json`、`Makefile`、`Cargo.toml`、
`go.mod`——以及浅层目录列表，并据此建议相应的检查命令。

本模块是纯逻辑，不会运行任何命令。模型不会提供命令；检测由程序驱动，用户通过
注册配方来授权哪些内容真正生效。建议采用保守策略：只有检测到信号才提出命令，
因此输出是人类可以信任的短列表，而不是猜测。

pytest 建议根据五个真实仓库进行了调校（`docs/EVAL_LIVE.md`）：

- 始终使用 `python -m pytest`，不要直接使用 `pytest`：`python -m` 会将检出目录
  加入 `sys.path`，而裸二进制会悄悄测试同一库的*已安装*副本（idna 是本项目的
  传递依赖，一个行为差异就导致了一个失败测试）。
- 没有任何 pytest 配置的项目通常仍有包含 `test_*.py` 文件的 `tests/` 或 `test/`
  目录（jmespath 只有 `setup.py`）；这个结构本身就是信号，建议会限定到发现
  它的目录。
- `src/<package>/` 布局无法从裸检出目录导入；pytest 自身的 `pythonpath` 覆盖可以
  解决此问题，而不需要生成 shim（tomli）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RecipeCandidate:
    id: str
    argv: tuple[str, ...]
    #: 提出此建议的原因，会展示给用户，以便审计这份提案。
    rationale: str


@dataclass(frozen=True, slots=True)
class _TreeFacts:
    """从浅层路径列表中提取的结构信息。"""

    tests_dir: str | None
    src_layout: bool


def _tree_facts(paths: Iterable[str]) -> _TreeFacts:
    path_set = set(paths)
    tests_dir = None
    for candidate in ("tests", "test"):
        if any(re.fullmatch(rf"{candidate}/test_[^/]+\.py", p) for p in path_set):
            tests_dir = candidate
            break
    src_layout = any(re.fullmatch(r"src/[^/]+/__init__\.py", p) for p in path_set)
    return _TreeFacts(tests_dir=tests_dir, src_layout=src_layout)


def _plugin_warning(files: dict[str, str]) -> str:
    """当项目自身的 pytest 配置要求插件时发出警告。

    在 wcwidth 上测得：它的 tox.ini addopts 要求 pytest-cov，因此在没有该插件的
    环境中，建议命令会因用法错误而失败。建议仍然尊重项目配置——覆盖 addopts
    可能破坏项目依赖的选项——但理由必须说明环境需要提供什么。
    """
    config_text = "\n".join(
        files.get(name, "") for name in ("pyproject.toml", "tox.ini", "setup.cfg")
    )
    plugins = []
    if re.search(r"--cov\b|--cov[-=]", config_text):
        plugins.append("pytest-cov")
    if re.search(r"^\s*(addopts|opts)\s*=.*(-n\s|--numprocesses)", config_text, re.MULTILINE):
        plugins.append("pytest-xdist")
    if not plugins:
        return ""
    return f"; note: the project's pytest options require {' and '.join(plugins)} installed"


def _pytest_candidate(files: dict[str, str], tree: _TreeFacts) -> RecipeCandidate | None:
    """根据最强的现有信号提出一个 pytest 建议。

    自身配置了 pytest 的项目会得到不限定范围的运行方式——其配置（testpaths、
    addopts、pythonpath）是权威，覆盖它们可能破坏项目依赖的选项。只有在没有可
    尊重的配置时，结构化回退方案才会限定运行范围并修复 src 布局。
    """
    warning = _plugin_warning(files)
    pyproject = files.get("pyproject.toml", "")
    if "[tool.pytest" in pyproject:
        return RecipeCandidate(
            "pytest",
            ("python", "-m", "pytest", "-q"),
            "pyproject.toml configures pytest" + warning,
        )
    if re.search(r"['\"]pytest\b", pyproject):
        return RecipeCandidate(
            "pytest",
            ("python", "-m", "pytest", "-q"),
            "pyproject.toml depends on pytest" + warning,
        )
    if re.search(r"^\[pytest\]", files.get("tox.ini", ""), re.MULTILINE):
        return RecipeCandidate(
            "pytest",
            ("python", "-m", "pytest", "-q"),
            "tox.ini has a [pytest] section" + warning,
        )
    if re.search(r"^\[tool:pytest\]", files.get("setup.cfg", ""), re.MULTILINE):
        return RecipeCandidate(
            "pytest",
            ("python", "-m", "pytest", "-q"),
            "setup.cfg has a [tool:pytest] section" + warning,
        )
    if tree.tests_dir is not None:
        argv: tuple[str, ...] = ("python", "-m", "pytest", "-q")
        why = f"{tree.tests_dir}/ contains test_*.py files"
        if tree.src_layout:
            argv += ("-o", "pythonpath=src")
            why += " (src layout, so the checkout is put on the import path)"
        return RecipeCandidate("pytest", (*argv, tree.tests_dir), why + warning)
    return None


def _node(content: str) -> RecipeCandidate | None:
    try:
        parsed = json.loads(content)
    except (ValueError, TypeError):
        return None
    scripts = parsed.get("scripts") if isinstance(parsed, dict) else None
    if isinstance(scripts, dict) and "test" in scripts:
        return RecipeCandidate("npm-test", ("npm", "test"), "package.json defines a test script")
    return None


def _makefile(content: str) -> RecipeCandidate | None:
    # 行首的 `test:` 目标，这是 Make 中的常见约定。
    if re.search(r"^test:", content, re.MULTILINE):
        return RecipeCandidate("make-test", ("make", "test"), "Makefile has a test target")
    return None


def _cargo(_content: str) -> RecipeCandidate | None:
    return RecipeCandidate("cargo-test", ("cargo", "test"), "Cargo.toml present")


def _go(_content: str) -> RecipeCandidate | None:
    return RecipeCandidate("go-test", ("go", "test", "./..."), "go.mod present")


#: 单文件生态中的 filename -> detector 映射。按顺序排列以保证输出确定；
#: pytest 候选项会综合多个文件和目录树判断，因此始终排在最前面。
_DETECTORS = (
    ("package.json", _node),
    ("Makefile", _makefile),
    ("Cargo.toml", _cargo),
    ("go.mod", _go),
)

#: 调用方应读取并传入的文件。将其公开是为了让 CLI 和评估测试框架与各个
#: 检测器保持同步。
KNOWN_FILES = (
    "pyproject.toml",
    "tox.ini",
    "setup.cfg",
    "package.json",
    "Makefile",
    "Cargo.toml",
    "go.mod",
)


def discover_recipes(files: dict[str, str], paths: Iterable[str] = ()) -> list[RecipeCandidate]:
    """根据项目文件内容和浅层目录列表提出检查配方。

    `files` 将存在的 `KNOWN_FILES` 映射到其内容；`paths` 是相对路径列表（顶层
    目录以及 `tests`/`test`/`src` 目录就足够）。这是纯函数，并且对所有输入都有
    定义。
    """
    candidates: list[RecipeCandidate] = []
    pytest_candidate = _pytest_candidate(files, _tree_facts(paths))
    if pytest_candidate is not None:
        candidates.append(pytest_candidate)
    for name, detector in _DETECTORS:
        if name in files:
            candidate = detector(files[name])
            if candidate is not None:
                candidates.append(candidate)
    return candidates
