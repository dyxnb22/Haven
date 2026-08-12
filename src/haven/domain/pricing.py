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
