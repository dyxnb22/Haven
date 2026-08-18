"""感知缓存的成本统计。

在 DeepSeek v4 flash 上，缓存命中的输入 token 成本是未命中的五十分之一，因此按未
命中费率计费所有输入 token，会在 Haven 实际测得的命中率下将输入账单高估近 8 倍。
成本按实测数字报告，因此必须考虑两种费率的拆分。
"""

from haven.domain.pricing import Pricing


class TestUnknownIsNotFree:
    """未配置的费率卡不得渲染为 $0.0000。

    审计时发现：针对 `deepseek-chat` 的每次实时运行都报告 $0.0000，因为模型没有
    profile，应用了全零的默认 `Pricing`。读者无法区分这种情况和真正免费的提供商。
    这与过时覆盖率数字属于同一类缺陷——生成的数字撒谎，而不是失败
    （`docs/DEFENSIVE_PATTERNS.md`）。
    """

    def test_an_unconfigured_rate_card_is_not_known(self) -> None:
        assert Pricing().is_known is False

    def test_a_configured_rate_card_is_known(self) -> None:
        assert Pricing(input_per_1m_usd=0.14, output_per_1m_usd=0.28).is_known is True

    def test_an_output_only_rate_card_still_counts_as_known(self) -> None:
        """有些提供商只对输出计费；这仍然是真实的费率卡。"""
        assert Pricing(output_per_1m_usd=0.28).is_known is True


class TestSingleRateIsUnchanged:
    def test_without_a_cached_rate_all_input_bills_at_one_rate(self) -> None:
        """任何配置都不得悄悄改变含义。"""
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
        # 890k 按 0.0028 计缓存，110k 按 0.14 计新输入
        cost = pricing.cost(1_000_000, 0, cached_input_tokens=890_000)
        assert abs(cost - (0.89 * 0.0028 + 0.11 * 0.14)) < 1e-9

    def test_a_full_cache_hit_costs_the_hit_rate(self) -> None:
        pricing = Pricing(input_per_1m_usd=0.14, cached_input_per_1m_usd=0.0028)
        assert abs(pricing.cost(1_000_000, 0, cached_input_tokens=1_000_000) - 0.0028) < 1e-9

    def test_no_cache_hit_costs_the_miss_rate(self) -> None:
        pricing = Pricing(input_per_1m_usd=0.14, cached_input_per_1m_usd=0.0028)
        assert abs(pricing.cost(1_000_000, 0, cached_input_tokens=0) - 0.14) < 1e-9

    def test_cached_count_above_input_is_clamped(self) -> None:
        """提供商报告的缓存量超过总量时，不得产生负账单。"""
        pricing = Pricing(input_per_1m_usd=0.14, cached_input_per_1m_usd=0.0028)
        assert pricing.cost(1000, 0, cached_input_tokens=5000) >= 0.0

    def test_the_old_arithmetic_overstated_a_cached_run(self) -> None:
        """本修复解决的缺陷，以数字固定。"""
        pricing = Pricing(
            input_per_1m_usd=0.14, output_per_1m_usd=0.28, cached_input_per_1m_usd=0.0028
        )
        aware = pricing.cost(1_000_000, 0, cached_input_tokens=890_000)
        naive = pricing.cost(1_000_000, 0)
        assert naive / aware > 7.0
