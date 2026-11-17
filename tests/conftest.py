"""全局测试夹具。

隔离 Haven 的用户数据目录与用户配置目录，使测试永远不会读取或写入真实的
`~/.local/share/haven` 数据库或构件，也不会读到开发者自己撰写的配置和技能。
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import platformdirs
import pytest

#: 任何可能让测试套件指向真实提供商的内容。只清除硬编码列表并不够：曾有
#: 开发者导出一次 DEEPSEEK_API_KEY，导致套件运行 200 秒并产生真实费用，因此
#: 规则是“移除所有符合凭据形状的变量”，而不是“移除我们想到的那些变量”。
_PROVIDER_ENV_SUFFIXES = ("_API_KEY", "_KEY", "_TOKEN", "_SECRET")
_PROVIDER_ENV_NAMES = ("HAVEN_API_KEY_ENV", "HAVEN_BASE_URL", "HAVEN_MODEL")
#: 指向 Haven 用户级目录的环境变量。隔离数据目录而不隔离配置目录，会让每个
#: 触达 build_services 的测试读到开发者真实的 config.toml 和技能目录。
_USER_DIR_ENV_NAMES = ("HAVEN_DATA_DIR", "HAVEN_CONFIG_DIR")


def _provider_env_names() -> list[str]:
    return [
        name
        for name in os.environ
        if name.upper().endswith(_PROVIDER_ENV_SUFFIXES) or name in _PROVIDER_ENV_NAMES
    ]


@pytest.fixture(autouse=True)
def hermetic_environment(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """使每个测试都离线运行，并与开发者环境隔离。"""
    data_dir = tmp_path_factory.mktemp("haven-data")
    config_dir = tmp_path_factory.mktemp("haven-config")
    saved = {name: os.environ[name] for name in _provider_env_names()}
    saved_user_dirs = {name: os.environ.get(name) for name in _USER_DIR_ENV_NAMES}

    for name in saved:
        del os.environ[name]
    os.environ["HAVEN_DATA_DIR"] = str(data_dir)
    os.environ["HAVEN_CONFIG_DIR"] = str(config_dir)
    try:
        yield data_dir
    finally:
        os.environ.update(saved)
        for name, value in saved_user_dirs.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_environment_is_hermetic() -> None:
    """保护环境隔离本身：此测试失败时，套件可能触达真实提供商。"""
    assert _provider_env_names() == []
    assert os.environ["HAVEN_CONFIG_DIR"] != platformdirs.user_config_dir("haven")


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    """一个带**缺陷** `calc.py` 和 `verify` 配方的 Git 仓库。

    任务形状的仓库：`add` 写成减法，配一个会失败的注册检查，因此跑起来的任务
    有东西可修、有证据可留。这是集成与 TUI 测试用的那一版。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@haven.dev"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Haven Test"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "README.md").write_text("# Demo\n", encoding="utf-8")
    verify_cmd = f'argv = ["{sys.executable}", "-c", "import calc; assert calc.add(2, 3) == 5"]'
    (repo / ".haven.toml").write_text(f"[recipes.verify]\n{verify_cmd}\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


@pytest.fixture()
def git_repo_clean(tmp_path: Path) -> Path:
    """一个内容**正确**、没有任何 Haven 配置的 Git 仓库。

    工作树生命周期单元测试用的那一版：断言的是"主仓库未被污染"和"改动被合流"，
    所以仓库本身必须本来就是对的一一缺陷版会让这些断言失去意义。两个夹具不是
    重复，而是同一形状的两种状态。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@haven.dev"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Haven Test"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo
