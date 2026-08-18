"""按模型定义默认值，以数据表达，而不是在核心层使用分支。"""

from haven.application.profiles import (
    DEEPSEEK_V4_FLASH,
    DEFAULT_CONTEXT_CHARS,
    DEFAULT_PROFILE,
    profile_for,
)


class TestLookup:
    def test_the_flash_model_gets_its_own_profile(self) -> None:
        assert profile_for("deepseek-v4-flash") is DEEPSEEK_V4_FLASH

    def test_an_unknown_model_falls_back_to_the_conservative_default(self) -> None:
        """陌生模型必须继承当前行为，而不是继承猜测。"""
        assert profile_for("some-model-nobody-has-heard-of") is DEFAULT_PROFILE

    def test_the_default_keeps_the_historical_context_budget(self) -> None:
        assert DEFAULT_PROFILE.max_context_chars == DEFAULT_CONTEXT_CHARS == 96_000

    def test_the_default_prices_nothing(self) -> None:
        """未知模型的成本必须保持为零，而不是臆造价格。"""
        assert DEFAULT_PROFILE.pricing.input_per_1m_usd == 0.0
        assert DEFAULT_PROFILE.pricing.cached_input_per_1m_usd is None


class TestFlashProfile:
    def test_its_context_budget_is_far_larger_than_the_default(self) -> None:
        """保留的前缀按命中费率计费，因此在此模型上过早压缩会产生费用，而不是节省
        费用。"""
        assert DEEPSEEK_V4_FLASH.max_context_chars > 4 * DEFAULT_CONTEXT_CHARS

    def test_its_context_budget_stays_inside_the_window_at_the_measured_worst_case(
        self,
    ) -> None:
        """即使采用实时观察到的最密集字符/token 比（`evals/calibrate_context.py`），
        字符预算推导出的 token 数也必须低于窗口；这样手工设置的常量是经过检查的，
        而不是猜出来的。"""
        from haven.application.profiles import MEASURED_MIN_CHARS_PER_TOKEN

        worst_case_tokens = DEEPSEEK_V4_FLASH.max_context_chars / MEASURED_MIN_CHARS_PER_TOKEN
        assert DEEPSEEK_V4_FLASH.context_window_tokens > 0
        assert worst_case_tokens < DEEPSEEK_V4_FLASH.context_window_tokens

    def test_it_prices_cache_hits_far_below_misses(self) -> None:
        pricing = DEEPSEEK_V4_FLASH.pricing
        assert pricing.cached_input_per_1m_usd is not None
        assert pricing.input_per_1m_usd / pricing.cached_input_per_1m_usd > 10

    def test_it_leaves_reasoning_effort_to_the_provider(self) -> None:
        """不会凭猜测改变默认值；A/B 工具会先进行测量。"""
        assert DEEPSEEK_V4_FLASH.reasoning_effort is None

    def test_it_allows_a_longer_idle_stream_gap_than_the_default(self) -> None:
        """思考模式在流式 token 之间可能比非推理模型暂停更久，因此这里的空闲上限
        更宽松。"""
        assert DEEPSEEK_V4_FLASH.stream_idle_timeout_s > DEFAULT_PROFILE.stream_idle_timeout_s

    def test_it_supports_native_prefix_continuation_on_the_beta_endpoint(self) -> None:
        """2026-08 实时确认：beta endpoint 会原地续写带 `prefix: true` 的 assistant
        消息；stable endpoint 会对此返回 400。因此该能力确实存在，但只有指向 beta
        endpoint 时才启用。"""
        assert DEEPSEEK_V4_FLASH.supports_assistant_prefix is True
        assert DEEPSEEK_V4_FLASH.prefix_continuation_enabled("https://api.deepseek.com/beta")
        assert DEEPSEEK_V4_FLASH.prefix_continuation_enabled("https://api.deepseek.com/beta/")
        assert not DEEPSEEK_V4_FLASH.prefix_continuation_enabled("https://api.deepseek.com")


class TestLegacyAliases:
    """`deepseek-chat` 和 `deepseek-reasoner` 是 v4-flash 的文档别名（非思考和思考
    模式），于 2026-07-24 退役。实时运行实际使用过这些名称；将它们解析为
    DEFAULT_PROFILE 会悄悄给这些运行 96k 上下文预算和 $0.00 账单。"""

    def test_the_legacy_chat_alias_resolves_to_flash(self) -> None:
        assert profile_for("deepseek-chat") is DEEPSEEK_V4_FLASH

    def test_the_legacy_reasoner_alias_resolves_to_flash(self) -> None:
        assert profile_for("deepseek-reasoner") is DEEPSEEK_V4_FLASH

    def test_a_profiled_model_prices_a_real_run(self) -> None:
        """重要的回归保证：该模型上的运行不得计费为 $0。"""
        assert profile_for("deepseek-chat").pricing.is_known is True

    def test_an_unknown_model_still_reports_an_unknown_price(self) -> None:
        assert profile_for("some-model-nobody-has-heard-of").pricing.is_known is False


class TestPrefixEndpointGuard:
    """需要特定 endpoint 才能前缀续写的 profile 只在该 endpoint 启用能力，因此错误
    endpoint 上的截断轮次会回退到 shim，而不是必然返回 400。"""

    def test_a_profile_without_a_required_endpoint_enables_prefix_anywhere(self) -> None:
        from haven.application.profiles import ModelProfile

        p = ModelProfile(name="anyendpoint", supports_assistant_prefix=True)
        assert p.prefix_continuation_enabled("https://example.com/v1")

    def test_a_profile_without_the_capability_never_enables_prefix(self) -> None:
        from haven.application.profiles import ModelProfile

        p = ModelProfile(
            name="off",
            supports_assistant_prefix=False,
            prefix_beta_base_url="https://api.deepseek.com/beta",
        )
        assert not p.prefix_continuation_enabled("https://api.deepseek.com/beta")
