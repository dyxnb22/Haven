"""全局测试夹具。

隔离 Haven 的用户数据目录，使测试永远不会读取或写入真实的
`~/.local/share/haven` 数据库或构件。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

#: 任何可能让测试套件指向真实提供商的内容。只清除硬编码列表并不够：曾有
#: 开发者导出一次 DEEPSEEK_API_KEY，导致套件运行 200 秒并产生真实费用，因此
#: 规则是“移除所有符合凭据形状的变量”，而不是“移除我们想到的那些变量”。
_PROVIDER_ENV_SUFFIXES = ("_API_KEY", "_KEY", "_TOKEN", "_SECRET")
_PROVIDER_ENV_NAMES = ("HAVEN_API_KEY_ENV", "HAVEN_BASE_URL", "HAVEN_MODEL")


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
    saved = {name: os.environ[name] for name in _provider_env_names()}
    saved_data_dir = os.environ.get("HAVEN_DATA_DIR")

    for name in saved:
        del os.environ[name]
    os.environ["HAVEN_DATA_DIR"] = str(data_dir)
    try:
        yield data_dir
    finally:
        os.environ.update(saved)
        if saved_data_dir is None:
            os.environ.pop("HAVEN_DATA_DIR", None)
        else:
            os.environ["HAVEN_DATA_DIR"] = saved_data_dir


def test_environment_is_hermetic() -> None:
    """保护环境隔离本身：此测试失败时，套件可能触达真实提供商。"""
    assert _provider_env_names() == []
