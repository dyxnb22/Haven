"""CLI、TUI 与导出共用的模型和计价文案。"""

from haven.application.profiles import provider_profile_for
from haven.config import ResolvedConfig
from haven.contracts.model_call import ModelCallInfo, ModelSpec

_ROUTE_REASONS = {
    "single": "single model",
    "exploration": "exploration",
    "always_deep": "always deep",
    "edits": "edits recorded",
    "fast_turns": "fast turn limit reached",
    "plan": "plan in progress",
}


def cost_text(cost_usd: float, known: bool) -> str:
    return f"${cost_usd:.6f} estimated" if known else "unknown"


def _reasoning_text(supported: bool | None, effort: str | None) -> str:
    if effort is not None:
        return f"supported, effort {effort}" if supported else f"effort {effort}"
    if supported is False:
        return "unsupported"
    return "provider default" if supported else "unknown (provider default)"


def model_call_text(info: ModelCallInfo) -> str:
    window = f"{info.context_window_tokens:,} tokens" if info.context_window_tokens else "unknown"
    return (
        f"{info.model_name} ({_ROUTE_REASONS[info.selection_reason]}) | "
        f"context {info.context_chars:,} chars, window {window} | "
        f"reasoning {_reasoning_text(info.reasoning_supported, info.reasoning_effort)} | "
        f"usage {info.usage_source} | cost {cost_text(info.cost_usd, info.cost_known)}"
        + (f" ({info.pricing_source})" if info.cost_known else "")
    )


def model_spec_lines(spec: ModelSpec, config: ResolvedConfig) -> list[str]:
    profiles = [provider_profile_for(name, config.provider.base_url) for name in spec.model_names]
    prefix = all(p.prefix_continuation_enabled(config.provider.base_url) for p in profiles)
    lines = []
    if spec.fast_model:
        lines.append("Fast for exploration; deep for edits or an active plan.")
        if spec.fast_turns is not None:
            lines.append(f"Deep from step {max(0, spec.fast_turns) + 1}.")
    for name, profile in zip(spec.model_names, profiles, strict=True):
        window = (
            f"{profile.context_window_tokens:,} tokens"
            if profile.context_window_tokens
            else "unknown"
        )
        output = (
            f", output limit {profile.output_limit_tokens:,} tokens"
            if profile.output_limit_tokens
            else ""
        )
        replay = "required" if profile.requires_tool_call_reasoning else "on provider request"
        lines.append(
            f"  {name} | context {profile.max_context_chars:,} chars, window {window}{output} | "
            f"reasoning {_reasoning_text(profile.reasoning_supported, profile.reasoning_effort)}, "
            f"tool replay {replay} | {'native' if prefix else 'prompt-based'} continuation"
        )
        override = config.sources.get("pricing") == "user"
        pricing = config.pricing if override else profile.pricing
        if pricing.is_known:
            cached = pricing.cached_input_per_1m_usd
            cache_rate = f", ${cached:g} cached input" if cached is not None else ""
            source = "user" if override else f"model-profile:{profile.name}"
            lines.append(
                f"    estimate rates ({source}) per 1M tokens "
                f"${pricing.input_per_1m_usd:g} input{cache_rate}, "
                f"${pricing.output_per_1m_usd:g} output"
            )
        else:
            lines.append("    cost unknown")
        if profile.documentation_status:
            lines.append(f"    {profile.documentation_status}")
    return lines
