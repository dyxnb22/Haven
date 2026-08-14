"""Layered configuration.

Merge order is fixed and fail-closed:
built-in safe defaults -> user config -> provider environment + CLI budget tier
-> project `.haven.toml` (tighten-only).

A project file can only *tighten* budgets and *register* verification recipes;
it can never raise limits, change the provider, or change the agent approval
policy. A recipe is explicit user-authored process authority and may declare its
own network/readable-root needs. Secrets live in environment variables only and
are reported as present/missing.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import platformdirs

from haven.contracts.tools import RecipeSpec
from haven.domain.budget import BUDGET_TIERS, Budget
from haven.domain.pricing import Pricing

APP_NAME = "haven"

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_API_KEY_ENV = "HAVEN_API_KEY"


class ConfigError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    api_key_env: str = DEFAULT_API_KEY_ENV

    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) or None


@dataclass(frozen=True, slots=True)
class ResolvedConfig:
    provider: ProviderConfig
    budget: Budget
    pricing: Pricing
    recipes: dict[str, RecipeSpec] = field(default_factory=dict)
    #: config key -> where its final value came from
    sources: dict[str, str] = field(default_factory=dict)


def user_config_path() -> Path:
    return Path(platformdirs.user_config_dir(APP_NAME)) / "config.toml"


def data_dir() -> Path:
    """Where the run database and artifacts live (outside any workspace).

    `HAVEN_DATA_DIR` overrides the platform default; tests and sandboxed runs
    use it to avoid touching real user data.
    """
    override = os.environ.get("HAVEN_DATA_DIR")
    if override:
        return Path(override)
    return Path(platformdirs.user_data_dir(APP_NAME))


def db_path() -> Path:
    return data_dir() / "haven.db"


def artifacts_dir() -> Path:
    return data_dir() / "artifacts"


def project_config_path(workspace: Path) -> Path:
    return workspace / ".haven.toml"


def _read_toml(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc


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
        return value

    def _float(key: str, default: float) -> float:
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ConfigError(f"budget key {key!r} in {origin} must be a number")
        return float(value)

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
        argv = spec["argv"]
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(item, str) for item in argv)
        ):
            raise ConfigError(f"recipe {recipe_id!r} argv must be a non-empty list of strings")
        timeout = float(spec.get("timeout_seconds", 120.0))
        recipes[str(recipe_id)] = RecipeSpec(
            id=str(recipe_id),
            argv=tuple(argv),
            timeout_seconds=timeout,
            allow_network=bool(spec.get("allow_network", False)),
            readable_roots=tuple(str(root) for root in spec.get("readable_roots", ())),
        )
    return recipes


def load_config(workspace: Path | None = None, tier: str | None = None) -> ResolvedConfig:
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

    # -- user level ---------------------------------------------------------
    user_path = user_config_path()
    if user_path.is_file():
        raw = _read_toml(user_path)
        if provider_raw := raw.get("provider"):
            if not isinstance(provider_raw, dict):
                raise ConfigError(f"[provider] in {user_path} must be a table")
            provider = ProviderConfig(
                base_url=str(provider_raw.get("base_url", provider.base_url)),
                model=str(provider_raw.get("model", provider.model)),
                api_key_env=str(provider_raw.get("api_key_env", provider.api_key_env)),
            )
            for key in ("base_url", "model", "api_key_env"):
                if key in provider_raw:
                    sources[f"provider.{key}"] = "user"
        if budget_raw := raw.get("budget"):
            budget = _parse_budget(budget_raw, str(user_path))
            sources["budget"] = "user"
        if pricing_raw := raw.get("pricing"):
            if not isinstance(pricing_raw, dict):
                raise ConfigError(f"[pricing] in {user_path} must be a table")
            input_price = pricing_raw.get("input_per_1m_usd", 0.0)
            output_price = pricing_raw.get("output_per_1m_usd", 0.0)
            cached_price = pricing_raw.get("cached_input_per_1m_usd")
            if not isinstance(input_price, int | float) or not isinstance(
                output_price, int | float
            ):
                raise ConfigError(f"[pricing] values in {user_path} must be numbers")
            if cached_price is not None and not isinstance(cached_price, int | float):
                raise ConfigError(f"[pricing] values in {user_path} must be numbers")
            pricing = Pricing(
                input_per_1m_usd=float(input_price),
                output_per_1m_usd=float(output_price),
                cached_input_per_1m_usd=(float(cached_price) if cached_price is not None else None),
            )
            sources["pricing"] = "user"
        if recipes_raw := raw.get("recipes"):
            recipes.update(_parse_recipes(recipes_raw, str(user_path)))
            sources["recipes(user)"] = "user"

    # -- environment overrides for the provider ------------------------------
    if env_url := os.environ.get("HAVEN_BASE_URL"):
        provider = ProviderConfig(
            base_url=env_url, model=provider.model, api_key_env=provider.api_key_env
        )
        sources["provider.base_url"] = "env"
    if env_key_name := os.environ.get("HAVEN_API_KEY_ENV"):
        # Lets a provider keep its conventional variable name (DEEPSEEK_API_KEY,
        # OPENAI_API_KEY, ...). Only the *name* is configurable here; the secret
        # itself is never read from config.
        provider = ProviderConfig(
            base_url=provider.base_url, model=provider.model, api_key_env=env_key_name
        )
        sources["provider.api_key_env"] = "env"
    if env_model := os.environ.get("HAVEN_MODEL"):
        provider = ProviderConfig(
            base_url=provider.base_url, model=env_model, api_key_env=provider.api_key_env
        )
        sources["provider.model"] = "env"

    # -- tier: a user-level choice, so it may raise; applied before the
    # project file so a repository still cannot widen the selected budget -----
    if tier is not None:
        if tier not in BUDGET_TIERS:
            raise ConfigError(f"unknown budget tier {tier!r}; choose one of {sorted(BUDGET_TIERS)}")
        budget = BUDGET_TIERS[tier]
        sources["budget"] = f"tier:{tier}"

    # -- project level: may only tighten budgets and register recipes --------
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

    return ResolvedConfig(
        provider=provider,
        budget=budget,
        pricing=pricing,
        recipes=recipes,
        sources=sources,
    )


def explain(config: ResolvedConfig, sandbox_backend: str = "unknown") -> list[tuple[str, str, str]]:
    """(key, value, source) rows for `haven config explain`. Secrets are shown
    as present/missing, never their value."""
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
        rows.append((f"recipes.{recipe_id}", " ".join(spec.argv) + network, "config"))
    rows.append(("storage.db", str(db_path()), "platformdirs"))
    rows.append(("storage.artifacts", str(artifacts_dir()), "platformdirs"))
    return rows
