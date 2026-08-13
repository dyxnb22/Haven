"""Token pricing: pure arithmetic over a provider's published rates."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Pricing:
    input_per_1m_usd: float = 0.0
    output_per_1m_usd: float = 0.0
    #: Providers that cache prefixes bill a hit far below a miss — a factor of
    #: 50 on DeepSeek v4 flash. Left unset, all input bills at one rate, which
    #: keeps every existing configuration meaning exactly what it did.
    cached_input_per_1m_usd: float | None = None

    @property
    def is_known(self) -> bool:
        """Whether a rate card was actually supplied.

        All-zero rates mean nobody configured one — not that the provider is
        free. Renderers must consult this before printing a figure, or an
        unprofiled model reports `$0.0000` for a run that genuinely cost money.
        `cost()` still returns 0.0 in that case, because the alternative is an
        optional float threaded through the checkpoint and the event schema for
        a value no caller can use; the honesty lives at the point of display,
        the same split `Usage.estimated` already uses for token counts.
        """
        return self.input_per_1m_usd > 0.0 or self.output_per_1m_usd > 0.0

    def cost(self, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0) -> float:
        output_cost = output_tokens * self.output_per_1m_usd
        if self.cached_input_per_1m_usd is None:
            return (input_tokens * self.input_per_1m_usd + output_cost) / 1_000_000
        # Clamp: a provider reporting more cached than total must not be able
        # to produce a negative bill.
        cached = max(0, min(cached_input_tokens, input_tokens))
        fresh = input_tokens - cached
        input_cost = cached * self.cached_input_per_1m_usd + fresh * self.input_per_1m_usd
        return (input_cost + output_cost) / 1_000_000
