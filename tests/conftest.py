"""Global test fixtures.

Isolate Haven's user data directory so tests never read or write the real
`~/.local/share/haven` database or artifacts.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

#: Anything that could point the test suite at a real provider. Stripping only a
#: hardcoded list is not enough: a developer with DEEPSEEK_API_KEY exported once
#: turned this suite into a 200-second run that spent real money, so the rule is
#: "remove every credential-shaped variable", not "remove the ones we thought of".
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
    """Make every test offline and isolated from the developer's environment."""
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
    """Guard the guard: if this fails, the suite can reach a real provider."""
    assert _provider_env_names() == []
