"""Cache-aware cost accounting.

On DeepSeek v4 flash a cache-hit input token costs one fiftieth of a miss, so
billing every input token at the miss rate overstates the input bill by nearly
8x at the hit rate Haven actually measures. Cost is reported as a measured
figure, so it has to account for the split.
"""

from haven.domain.pricing import Pricing


class TestSingleRateIsUnchanged:
    def test_without_a_cached_rate_all_input_bills_at_one_rate(self) -> None:
        """No configuration may silently change meaning."""
        pricing = Pricing(input_per_1m_usd=0.14, output_per_1m_usd=0.28)
        assert pricing.cost(1_000_000, 0) == 0.14
        assert pricing.cost(0, 1_000_000) == 0.28

    def test_cached_tokens_are_ignored_when_no_cached_rate_is_set(self) -> None:
        pricing = Pricing(input_per_1m_usd=0.14, output_per_1m_usd=0.28)
        assert pricing.cost(1_000_000, 0, cached_input_tokens=900_000) == 0.14


class TestCacheAwareRate:
    def test_hits_and_misses_bill_separately(self) -> None:
        pricing = Pricing(
            input_per_1m_usd=0.14, output_per_1m_usd=0.28, cached_input_per_1m_usd=0.0028
        )
        # 890k cached at 0.0028, 110k fresh at 0.14
        cost = pricing.cost(1_000_000, 0, cached_input_tokens=890_000)
        assert abs(cost - (0.89 * 0.0028 + 0.11 * 0.14)) < 1e-9

    def test_a_full_cache_hit_costs_the_hit_rate(self) -> None:
        pricing = Pricing(input_per_1m_usd=0.14, cached_input_per_1m_usd=0.0028)
        assert abs(pricing.cost(1_000_000, 0, cached_input_tokens=1_000_000) - 0.0028) < 1e-9

    def test_no_cache_hit_costs_the_miss_rate(self) -> None:
        pricing = Pricing(input_per_1m_usd=0.14, cached_input_per_1m_usd=0.0028)
        assert abs(pricing.cost(1_000_000, 0, cached_input_tokens=0) - 0.14) < 1e-9

    def test_cached_count_above_input_is_clamped(self) -> None:
        """A provider that reports more cached than total must not produce a
        negative bill."""
        pricing = Pricing(input_per_1m_usd=0.14, cached_input_per_1m_usd=0.0028)
        assert pricing.cost(1000, 0, cached_input_tokens=5000) >= 0.0

    def test_the_old_arithmetic_overstated_a_cached_run(self) -> None:
        """The defect this fixes, pinned as a number."""
        pricing = Pricing(
            input_per_1m_usd=0.14, output_per_1m_usd=0.28, cached_input_per_1m_usd=0.0028
        )
        aware = pricing.cost(1_000_000, 0, cached_input_tokens=890_000)
        naive = pricing.cost(1_000_000, 0)
        assert naive / aware > 7.0
