"""Config layering and fail-closed parsing."""

from pathlib import Path

import pytest

from haven.config import ConfigError, load_config
from haven.domain.budget import Budget


def test_defaults_when_no_files(tmp_path: Path) -> None:
    config = load_config(tmp_path)
    assert config.budget == Budget()
    assert config.provider.model  # a default model name exists
    assert config.recipes == {}


def test_project_budget_tightens_only(tmp_path: Path) -> None:
    (tmp_path / ".haven.toml").write_text("[budget]\nmax_steps = 3\nmax_tool_calls = 999\n")
    config = load_config(tmp_path)
    assert config.budget.max_steps == 3  # lowered
    # cannot raise above the built-in default
    assert config.budget.max_tool_calls == Budget().max_tool_calls


def test_project_registers_recipes(tmp_path: Path) -> None:
    (tmp_path / ".haven.toml").write_text(
        '[recipes.tests]\nargv = ["pytest", "-q"]\ntimeout_seconds = 60\n'
    )
    config = load_config(tmp_path)
    assert "tests" in config.recipes
    assert config.recipes["tests"].argv == ("pytest", "-q")


def test_project_cannot_set_provider(tmp_path: Path) -> None:
    (tmp_path / ".haven.toml").write_text('[provider]\nbase_url = "http://evil"\n')
    with pytest.raises(ConfigError, match="may only contain"):
        load_config(tmp_path)


def test_invalid_recipe_rejected(tmp_path: Path) -> None:
    (tmp_path / ".haven.toml").write_text("[recipes.bad]\nargv = []\n")
    with pytest.raises(ConfigError, match="non-empty"):
        load_config(tmp_path)


def test_bad_budget_type_rejected(tmp_path: Path) -> None:
    (tmp_path / ".haven.toml").write_text('[budget]\nmax_steps = "lots"\n')
    with pytest.raises(ConfigError, match="integer"):
        load_config(tmp_path)


def test_env_can_rename_the_api_key_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HAVEN_API_KEY_ENV", "DEEPSEEK_API_KEY")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-value")
    config = load_config(tmp_path)
    assert config.provider.api_key_env == "DEEPSEEK_API_KEY"
    assert config.provider.api_key() == "sk-test-value"
    assert config.sources["provider.api_key_env"] == "env"


def test_env_overrides_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HAVEN_MODEL", "custom-model")
    config = load_config(tmp_path)
    assert config.provider.model == "custom-model"
    assert config.sources["provider.model"] == "env"
