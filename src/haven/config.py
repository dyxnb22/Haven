"""分层配置。

合并顺序固定，并采用失败即关闭的原则：
内置安全默认值 -> 用户配置 -> 提供商环境变量和 CLI 预算档位
-> 项目 `.haven.toml`（仅允许收紧）。

项目文件只能收紧预算和注册验证配方；不能提高限制、改变提供商或改变代理的
审批策略。配方是用户明确编写的进程授权，可以声明自身所需的网络权限和可读
根目录。秘密只存在于环境变量中，并且只报告“存在/缺失”。
"""

from __future__ import annotations

import math
import os
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import platformdirs

from haven.contracts.tools import RecipeSpec
from haven.domain.budget import BUDGET_TIERS, Budget
from haven.domain.pricing import Pricing

APP_NAME = "haven"

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_API_KEY_ENV = "HAVEN_API_KEY"


class ConfigError(Exception):
    """用户配置无效或无法读取时的错误。"""

    pass


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """单个模型提供商的连接和模型参数。"""

    #: OpenAI 兼容 API 的服务根地址。
    base_url: str = DEFAULT_BASE_URL
    #: 要请求的模型标识符。
    model: str = DEFAULT_MODEL
    #: 存放 API 密钥的环境变量名称；密钥本身不会进入配置对象。
    api_key_env: str = DEFAULT_API_KEY_ENV

    def api_key(self) -> str | None:
        """从配置指定的环境变量读取密钥；未设置或为空时返回 ``None``。"""
        return os.environ.get(self.api_key_env) or None


@dataclass(frozen=True, slots=True)
class ResolvedConfig:
    """合并用户配置、项目配置和环境变量后的最终配置。"""

    #: 最终的模型提供商连接设置。
    provider: ProviderConfig
    #: 所有只收紧合并完成后的最终硬资源预算。
    budget: Budget
    #: 用于估算该模型费用的费率卡。
    pricing: Pricing
    #: 按 recipe id 索引的、允许项目检查阶段执行的命令定义。
    recipes: dict[str, RecipeSpec] = field(default_factory=dict)
    #: config key -> 最终值的来源，用于诊断和可审计的配置报告。
    sources: dict[str, str] = field(default_factory=dict)


def user_config_path() -> Path:
    """返回当前用户级 Haven 配置文件路径。"""
    return Path(platformdirs.user_config_dir(APP_NAME)) / "config.toml"


def data_dir() -> Path:
    """运行数据库和构件的存放位置（位于所有工作区之外）。

    `HAVEN_DATA_DIR` 会覆盖平台默认路径；测试和沙箱运行会使用它，以免触碰
    用户的真实数据。
    """
    override = os.environ.get("HAVEN_DATA_DIR")
    if override:
        return Path(override)
    return Path(platformdirs.user_data_dir(APP_NAME))


def db_path() -> Path:
    """返回运行数据库路径。"""
    return data_dir() / "haven.db"


def artifacts_dir() -> Path:
    """返回内容寻址构件目录路径。"""
    return data_dir() / "artifacts"


def project_config_path(workspace: Path) -> Path:
    """返回工作区内项目配置文件 ``.haven.toml`` 的路径。"""
    return workspace / ".haven.toml"


def _read_toml(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"could not read config {path}: {exc}") from exc


def _validate_provider(provider: ProviderConfig) -> None:
    """验证用户文件和环境变量合并后的提供商连接参数。"""
    if (
        not provider.model.strip()
        or provider.model != provider.model.strip()
        or len(provider.model) > 200
        or any(ord(char) < 32 for char in provider.model)
    ):
        raise ConfigError("provider model must be a non-empty string of at most 200 characters")
    if provider.base_url != provider.base_url.strip() or len(provider.base_url) > 2048:
        raise ConfigError("provider base_url is too long")
    try:
        parsed = urlsplit(provider.base_url)
        hostname = parsed.hostname
    except ValueError as exc:
        raise ConfigError("provider base_url must be a valid absolute http(s) URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError("provider base_url must be an absolute http(s) URL")
    name = provider.api_key_env
    if (
        not name
        or len(name) > 200
        or not (name[0].isalpha() or name[0] == "_")
        or not all(char.isalnum() or char == "_" for char in name)
    ):
        raise ConfigError("provider api_key_env must be a valid environment variable name")


def _parse_budget(raw: object, origin: str) -> Budget:
    if not isinstance(raw, dict):
        raise ConfigError(f"[budget] in {origin} must be a table")
    known = {
        "max_steps",
        "max_tool_calls",
        "max_wall_time_seconds",
        "max_input_tokens",
        "max_output_tokens",
        "max_cost_usd",
    }
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"unknown budget keys in {origin}: {sorted(unknown)}")
    base = Budget()

    def _int(key: str, default: int) -> int:
        value = raw.get(key, default)
        if not isinstance(value, int) or isinstance(value, bool):
            raise ConfigError(f"budget key {key!r} in {origin} must be an integer")
        if value <= 0:
            raise ConfigError(f"budget key {key!r} in {origin} must be greater than zero")
        return value

    def _float(key: str, default: float) -> float:
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ConfigError(f"budget key {key!r} in {origin} must be a number")
        parsed = float(value)
        if not math.isfinite(parsed) or parsed <= 0:
            raise ConfigError(
                f"budget key {key!r} in {origin} must be finite and greater than zero"
            )
        return parsed

    return Budget(
        max_steps=_int("max_steps", base.max_steps),
        max_tool_calls=_int("max_tool_calls", base.max_tool_calls),
        max_wall_time_seconds=_float("max_wall_time_seconds", base.max_wall_time_seconds),
        max_input_tokens=_int("max_input_tokens", base.max_input_tokens),
        max_output_tokens=_int("max_output_tokens", base.max_output_tokens),
        max_cost_usd=_float("max_cost_usd", base.max_cost_usd),
    )


def _parse_recipes(raw: object, origin: str) -> dict[str, RecipeSpec]:
    if not isinstance(raw, dict):
        raise ConfigError(f"[recipes] in {origin} must be a table")
    recipes: dict[str, RecipeSpec] = {}
    for recipe_id, spec in raw.items():
        if not isinstance(spec, dict) or "argv" not in spec:
            raise ConfigError(f"recipe {recipe_id!r} in {origin} needs an 'argv' list")
        if not recipe_id or len(str(recipe_id)) > 100:
            raise ConfigError(f"recipe id {recipe_id!r} in {origin} must be 1-100 characters")
        unknown = set(spec) - {
            "argv",
            "timeout_seconds",
            "allow_network",
            "readable_roots",
        }
        if unknown:
            raise ConfigError(
                f"unknown keys for recipe {recipe_id!r} in {origin}: {sorted(unknown)}"
            )
        argv = spec["argv"]
        if (
            not isinstance(argv, list)
            or not argv
            or len(argv) > 64
            or not all(isinstance(item, str) and item and len(item) <= 4096 for item in argv)
        ):
            raise ConfigError(
                f"recipe {recipe_id!r} argv must contain 1-64 non-empty bounded strings"
            )
        raw_timeout = spec.get("timeout_seconds", 120.0)
        if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, int | float):
            raise ConfigError(f"recipe {recipe_id!r} timeout_seconds must be a number")
        timeout = float(raw_timeout)
        if not math.isfinite(timeout) or not 0.1 <= timeout <= 3600.0:
            raise ConfigError(f"recipe {recipe_id!r} timeout_seconds must be between 0.1 and 3600")
        allow_network = spec.get("allow_network", False)
        if not isinstance(allow_network, bool):
            raise ConfigError(f"recipe {recipe_id!r} allow_network must be a boolean")
        roots = spec.get("readable_roots", [])
        if (
            not isinstance(roots, list)
            or len(roots) > 16
            or not all(isinstance(root, str) and root and len(root) <= 4096 for root in roots)
        ):
            raise ConfigError(
                f"recipe {recipe_id!r} readable_roots must be a list of at most 16 paths"
            )
        recipes[str(recipe_id)] = RecipeSpec(
            id=str(recipe_id),
            argv=tuple(argv),
            timeout_seconds=timeout,
            allow_network=allow_network,
            readable_roots=tuple(roots),
        )
    return recipes


def load_config(workspace: Path | None = None, tier: str | None = None) -> ResolvedConfig:
    """按默认值、用户配置、环境变量、预算档位和项目收紧规则合并配置。"""
    provider = ProviderConfig()
    budget = Budget()
    pricing = Pricing()
    recipes: dict[str, RecipeSpec] = {}
    sources: dict[str, str] = {
        "provider.base_url": "default",
        "provider.model": "default",
        "provider.api_key_env": "default",
        "budget": "default",
        "pricing": "default",
    }

    # -- 用户级别 --------------------------------------------------------------
    user_path = user_config_path()
    if user_path.is_file():
        raw = _read_toml(user_path)
        unknown_top_level = set(raw) - {"provider", "budget", "pricing", "recipes"}
        if unknown_top_level:
            raise ConfigError(f"unknown top-level keys in {user_path}: {sorted(unknown_top_level)}")
        if (provider_raw := raw.get("provider")) is not None:
            if not isinstance(provider_raw, dict):
                raise ConfigError(f"[provider] in {user_path} must be a table")
            unknown_provider = set(provider_raw) - {"base_url", "model", "api_key_env"}
            if unknown_provider:
                raise ConfigError(
                    f"unknown provider keys in {user_path}: {sorted(unknown_provider)}"
                )
            provider_values = {
                key: provider_raw.get(key, getattr(provider, key))
                for key in ("base_url", "model", "api_key_env")
            }
            if not all(
                isinstance(value, str) and value.strip() for value in provider_values.values()
            ):
                raise ConfigError(f"[provider] values in {user_path} must be non-empty strings")
            provider = ProviderConfig(
                base_url=provider_values["base_url"],
                model=provider_values["model"],
                api_key_env=provider_values["api_key_env"],
            )
            for key in ("base_url", "model", "api_key_env"):
                if key in provider_raw:
                    sources[f"provider.{key}"] = "user"
        if budget_raw := raw.get("budget"):
            budget = _parse_budget(budget_raw, str(user_path))
            sources["budget"] = "user"
        if (pricing_raw := raw.get("pricing")) is not None:
            if not isinstance(pricing_raw, dict):
                raise ConfigError(f"[pricing] in {user_path} must be a table")
            unknown_pricing = set(pricing_raw) - {
                "input_per_1m_usd",
                "output_per_1m_usd",
                "cached_input_per_1m_usd",
            }
            if unknown_pricing:
                raise ConfigError(f"unknown pricing keys in {user_path}: {sorted(unknown_pricing)}")
            input_price = pricing_raw.get("input_per_1m_usd", 0.0)
            output_price = pricing_raw.get("output_per_1m_usd", 0.0)
            cached_price = pricing_raw.get("cached_input_per_1m_usd")
            pricing_values = (input_price, output_price, cached_price)
            if any(
                isinstance(value, bool)
                or (value is not None and not isinstance(value, int | float))
                for value in pricing_values
            ):
                raise ConfigError(f"[pricing] values in {user_path} must be numbers")
            try:
                pricing = Pricing(
                    input_per_1m_usd=float(input_price),
                    output_per_1m_usd=float(output_price),
                    cached_input_per_1m_usd=(
                        float(cached_price) if cached_price is not None else None
                    ),
                )
            except ValueError as exc:
                raise ConfigError(f"invalid [pricing] in {user_path}: {exc}") from exc
            sources["pricing"] = "user"
        if recipes_raw := raw.get("recipes"):
            recipes.update(_parse_recipes(recipes_raw, str(user_path)))
            sources["recipes(user)"] = "user"

    # -- 提供商的环境变量覆盖 --------------------------------------------------
    if env_url := os.environ.get("HAVEN_BASE_URL"):
        provider = ProviderConfig(
            base_url=env_url, model=provider.model, api_key_env=provider.api_key_env
        )
        sources["provider.base_url"] = "env"
    if env_key_name := os.environ.get("HAVEN_API_KEY_ENV"):
        # 允许提供商继续使用其惯用变量名（DEEPSEEK_API_KEY、OPENAI_API_KEY 等）。
        # 这里只能配置变量名；秘密本身永远不会从配置中读取。
        provider = ProviderConfig(
            base_url=provider.base_url, model=provider.model, api_key_env=env_key_name
        )
        sources["provider.api_key_env"] = "env"
    if env_model := os.environ.get("HAVEN_MODEL"):
        provider = ProviderConfig(
            base_url=provider.base_url, model=env_model, api_key_env=provider.api_key_env
        )
        sources["provider.model"] = "env"

    # -- tier：用户级选择，因此可以提高上限；在项目文件前应用，以便仓库
    # 仍然不能扩大已选择的预算 -----------------------------------------------
    if tier is not None:
        if tier not in BUDGET_TIERS:
            raise ConfigError(f"unknown budget tier {tier!r}; choose one of {sorted(BUDGET_TIERS)}")
        budget = BUDGET_TIERS[tier]
        sources["budget"] = f"tier:{tier}"

    # -- 项目级别：只能收紧预算并注册 recipe -----------------------------------
    if workspace is not None:
        project_path = project_config_path(workspace)
        if project_path.is_file():
            raw = _read_toml(project_path)
            allowed = {"budget", "recipes"}
            unknown = set(raw) - allowed
            if unknown:
                raise ConfigError(
                    f"project config {project_path} may only contain "
                    f"[budget] and [recipes]; found {sorted(unknown)}"
                )
            if budget_raw := raw.get("budget"):
                project_budget = _parse_budget(budget_raw, str(project_path))
                budget = budget.tightened(project_budget)
                sources["budget"] = f"{sources['budget']}+project(tighten-only)"
            if recipes_raw := raw.get("recipes"):
                recipes.update(_parse_recipes(recipes_raw, str(project_path)))
                sources["recipes(project)"] = "project"

    _validate_provider(provider)
    return ResolvedConfig(
        provider=provider,
        budget=budget,
        pricing=pricing,
        recipes=recipes,
        sources=sources,
    )


def explain(config: ResolvedConfig, sandbox_backend: str = "unknown") -> list[tuple[str, str, str]]:
    """为 `haven config explain` 返回 `(键、值、来源)` 行。秘密只显示存在/缺失，
    绝不显示实际值。"""
    key_state = "present" if config.provider.api_key() else "missing"
    rows = [
        ("provider.base_url", config.provider.base_url, config.sources["provider.base_url"]),
        ("provider.model", config.provider.model, config.sources["provider.model"]),
        (
            "provider.api_key",
            f"{key_state} (env {config.provider.api_key_env})",
            "environment",
        ),
        ("budget.max_steps", str(config.budget.max_steps), config.sources["budget"]),
        ("budget.max_tool_calls", str(config.budget.max_tool_calls), config.sources["budget"]),
        (
            "budget.max_wall_time_seconds",
            str(config.budget.max_wall_time_seconds),
            config.sources["budget"],
        ),
        ("budget.max_cost_usd", str(config.budget.max_cost_usd), config.sources["budget"]),
        (
            "pricing",
            f"in={config.pricing.input_per_1m_usd}/1M out={config.pricing.output_per_1m_usd}/1M",
            config.sources["pricing"],
        ),
    ]
    rows.append(("sandbox.backend", sandbox_backend, "platform"))
    for recipe_id, spec in sorted(config.recipes.items()):
        network = " [network allowed]" if spec.allow_network else ""
        rows.append((f"recipes.{recipe_id}", shlex.join(spec.argv) + network, "config"))
    rows.append(("storage.db", str(db_path()), "platformdirs"))
    rows.append(("storage.artifacts", str(artifacts_dir()), "platformdirs"))
    return rows
