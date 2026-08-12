"""Per-model defaults.

Haven targets one model in practice, but the core stays model-neutral: the
places where a model's published behavior should change a default are gathered
here as data, so nothing in the agent loop, the policy, or the context builder
has to branch on a model name.

An unknown model gets `DEFAULT_PROFILE`, which is exactly Haven's historical
behavior — an unfamiliar provider inherits a conservative default rather than
numbers guessed from a similar-sounding one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from haven.domain.pricing import Pricing

#: The budget Haven used before profiles existed.
DEFAULT_CONTEXT_CHARS = 96_000


@dataclass(frozen=True, slots=True)
class ModelProfile:
    name: str
    max_context_chars: int = DEFAULT_CONTEXT_CHARS
    pricing: Pricing = field(default_factory=Pricing)
    #: Passed to the provider only when set, so an unset value means "whatever
    #: the provider defaults to" rather than a value Haven chose.
    reasoning_effort: str | None = None
    #: Whether the provider requires the reasoning that preceded a tool call to
    #: be replayed on subsequent requests (DeepSeek V4 does; ADR 0014).
    requires_tool_call_reasoning: bool = False


DEFAULT_PROFILE = ModelProfile(name="default")

#: Published behavior as of 2026-08: a 1M-token window shared by input and
#: output, and a cache hit priced at one fiftieth of a miss.
#:
#: The larger character budget is not free: carrying more cached tokens costs
#: slightly more per turn. It pays for itself by avoiding re-reads, which bill
#: at the miss rate and also cost a step — `evals/ab.py` puts the break-even at
#: roughly one avoided re-read every ten turns. It stays far below the window
#: because latency and the miss-rate cost of genuinely new content both grow
#: with request size.
DEEPSEEK_V4_FLASH = ModelProfile(
    name="deepseek-v4-flash",
    max_context_chars=480_000,
    pricing=Pricing(
        input_per_1m_usd=0.14,
        output_per_1m_usd=0.28,
        cached_input_per_1m_usd=0.0028,
    ),
    requires_tool_call_reasoning=True,
)

_PROFILES: dict[str, ModelProfile] = {
    DEEPSEEK_V4_FLASH.name: DEEPSEEK_V4_FLASH,
}


def profile_for(model_name: str) -> ModelProfile:
    """The profile for a model, or the conservative default."""
    return _PROFILES.get(model_name, DEFAULT_PROFILE)
