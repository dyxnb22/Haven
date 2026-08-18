"""配置分层和失败即关闭的解析。"""

from pathlib import Path

import pytest

from haven.config import ConfigError, explain, load_config
from haven.domain.budget import Budget


def test_defaults_when_no_files(tmp_path: Path) -> None:
    config = load_config(tmp_path)
    assert config.budget == Budget()
    assert config.provider.model  # 存在默认模型名称
    assert config.recipes == {}


def test_project_budget_tightens_only(tmp_path: Path) -> None:
    (tmp_path / ".haven.toml").write_text("[budget]\nmax_steps = 3\nmax_tool_calls = 999\n")
    config = load_config(tmp_path)
    assert config.budget.max_steps == 3  # 已降低
    # 不能提高到内置默认值以上
    assert config.budget.max_tool_calls == Budget().max_tool_calls


def test_project_registers_recipes(tmp_path: Path) -> None:
    (tmp_path / ".haven.toml").write_text(
        '[recipes.tests]\nargv = ["pytest", "-q"]\ntimeout_seconds = 60\n'
    )
    config = load_config(tmp_path)
    assert "tests" in config.recipes
    assert config.recipes["tests"].argv == ("pytest", "-q")


def test_a_tier_selects_its_budget(tmp_path: Path) -> None:
    assert load_config(tmp_path, "deep").budget.max_steps == 80
    assert load_config(tmp_path, "quick").budget.max_steps == 8


def test_no_tier_keeps_the_standard_default(tmp_path: Path) -> None:
    assert load_config(tmp_path).budget == Budget()


def test_unknown_tier_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown budget tier"):
        load_config(tmp_path, "unlimited")


def test_a_project_can_tighten_a_tier_but_not_widen_it(tmp_path: Path) -> None:
    """档位是用户选择，可以提高预算；仓库仍然不能提高预算。"""
    (tmp_path / ".haven.toml").write_text("[budget]\nmax_steps = 5\nmax_tool_calls = 9999\n")
    budget = load_config(tmp_path, "deep").budget
    assert budget.max_steps == 5
    assert budget.max_tool_calls == 160


def test_tier_is_reported_as_the_budget_source(tmp_path: Path) -> None:
    rows = dict((key, source) for key, _, source in explain(load_config(tmp_path, "deep")))
    assert rows["budget.max_steps"] == "tier:deep"


def test_recipes_deny_network_by_default(tmp_path: Path) -> None:
    (tmp_path / ".haven.toml").write_text('[recipes.tests]\nargv = ["pytest", "-q"]\n')
    assert load_config(tmp_path).recipes["tests"].allow_network is False


def test_a_recipe_may_opt_into_network(tmp_path: Path) -> None:
    """确实需要网络的检查可以声明这一点；模型不能声明。"""
    (tmp_path / ".haven.toml").write_text(
        '[recipes.itest]\nargv = ["pytest", "-m", "integration"]\nallow_network = true\n'
    )
    assert load_config(tmp_path).recipes["itest"].allow_network is True


def test_a_recipe_may_declare_a_readable_toolchain_root(tmp_path: Path) -> None:
    """Maven 或 Gradle 检查需要读取 $HOME 下的依赖缓存，这与解释器前缀已经获得的
    例外相同（ports/sandbox.py）。"""
    (tmp_path / ".haven.toml").write_text(
        '[recipes.mvn]\nargv = ["mvn", "-o", "test"]\nreadable_roots = ["~/.m2"]\n',
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    assert config.recipes["mvn"].readable_roots == ("~/.m2",)


def test_a_recipe_declares_no_readable_roots_by_default(tmp_path: Path) -> None:
    """缺省情况必须保持为空，否则每个已有配方都会悄悄获得它从未请求的授权。"""
    (tmp_path / ".haven.toml").write_text('[recipes.tests]\nargv = ["pytest", "-q"]\n')
    assert load_config(tmp_path).recipes["tests"].readable_roots == ()


def test_project_cannot_weaken_the_sandbox(tmp_path: Path) -> None:
    """项目文件不得关闭限制，即使只是声明一个表也不行。"""
    (tmp_path / ".haven.toml").write_text('[sandbox]\nbackend = "none"\n')
    with pytest.raises(ConfigError, match="may only contain"):
        load_config(tmp_path)


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
