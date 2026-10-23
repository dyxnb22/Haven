"""全局测试夹具。

隔离 Haven 的用户数据目录与用户配置目录，使测试永远不会读取或写入真实的
`~/.local/share/haven` 数据库或构件，也不会读到开发者自己撰写的配置和技能。
"""

from __future__ import annotations

import os
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
