"""按模型定义默认值，以数据表达，而不是在核心层使用分支。"""

import pytest

from haven.application.profiles import (
    DEEPSEEK_V4_FLASH,
    DEFAULT_CONTEXT_CHARS,
    DEFAULT_PROFILE,
    profile_for,
    provider_profile_for,
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


class TestRoutedProfilesInheritEverything:
    """双轨 profile 必须继承深度模型除显式覆盖外的全部字段。

    此前这里手写一个 ModelProfile，于是此后新增的每个字段都会被静默丢成默认值——
    已经漏了 reasoning_supported、output_limit_tokens、documentation_status 和
    `use_max_completion_tokens` 四个。最后那个直接决定请求里用 max_tokens 还是
    max_completion_tokens，丢了就是每轮一个 400。当前注册的 profile 恰好都没设它们，
    所以这是潜伏缺陷；这里用临时注册的两个 profile 把它钉住。
    """

    @staticmethod
    def _register(monkeypatch: pytest.MonkeyPatch) -> None:
        import haven.application.profiles as profiles_module

        monkeypatch.setitem(
            profiles_module._PROFILES,
            "fast-x",
            profiles_module.ModelProfile(
                name="fast-x",
                max_context_chars=10_000,
                stream_idle_timeout_s=30.0,
                reasoning_supported=False,
                output_limit_tokens=4_096,
                documentation_status="fast doc",
                use_max_completion_tokens=False,
            ),
        )
        monkeypatch.setitem(
            profiles_module._PROFILES,
            "deep-x",
            profiles_module.ModelProfile(
                name="deep-x",
                max_context_chars=90_000,
                stream_idle_timeout_s=600.0,
                reasoning_supported=True,
                output_limit_tokens=128_000,
                documentation_status="deep doc",
                use_max_completion_tokens=True,
                reasoning_effort="high",
                requires_tool_call_reasoning=True,
                supports_assistant_prefix=True,
                prefix_beta_base_url="https://api.example.com/beta",
            ),
        )

    def test_no_field_is_silently_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._register(monkeypatch)
        routed = profile_for("routed(fast-x+deep-x)")

        # 深模型独有的能力字段必须继承过来，而不是回到 dataclass 默认值
        assert routed.use_max_completion_tokens is True
        assert routed.reasoning_supported is True
        assert routed.output_limit_tokens == 128_000
        assert routed.documentation_status == "deep doc"
        assert routed.reasoning_effort == "high"
        assert routed.requires_tool_call_reasoning is True
        assert routed.supports_assistant_prefix is True
        assert routed.prefix_beta_base_url == "https://api.example.com/beta"

    def test_the_three_widest_limits_still_take_the_max(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._register(monkeypatch)
        routed = profile_for("routed(fast-x+deep-x)")

        # 双轨的字符预算必须装得下任一侧的请求
        assert routed.max_context_chars == 90_000
        assert routed.context_window_tokens == 0  # 两侧都是 0（未知）
        assert routed.stream_idle_timeout_s == 600.0

    def test_the_real_deepseek_pair_is_unchanged(self) -> None:
        """修复不能改变现有行为：真实注册的那一对必须一个字段都不差。"""
        routed = profile_for("routed(deepseek-chat+deepseek-v4-flash)")

        assert routed.max_context_chars == DEEPSEEK_V4_FLASH.max_context_chars
        assert routed.context_window_tokens == DEEPSEEK_V4_FLASH.context_window_tokens
        assert routed.stream_idle_timeout_s == DEEPSEEK_V4_FLASH.stream_idle_timeout_s
        assert routed.pricing == DEEPSEEK_V4_FLASH.pricing
        assert routed.requires_tool_call_reasoning is True
        assert routed.supports_assistant_prefix is True
        assert routed.prefix_beta_base_url == DEEPSEEK_V4_FLASH.prefix_beta_base_url
        assert routed.use_max_completion_tokens == DEEPSEEK_V4_FLASH.use_max_completion_tokens


class TestOpenAIProfilesResolveByName:
    """`_PROFILES` 与 `_OPENAI_PROFILES` 曾是两张互不相通的表。

    于是 `routed(gpt-6-luna+deepseek-v4-flash)` 的 fast 一侧静默退化成
    DEFAULT_PROFILE——它的 1M 窗口按 0 参与 `max()`，安全上限被低估，而
    `max_context_chars` 取的是另一侧所以看起来毫无异常。
    """

    def test_an_openai_model_resolves_to_its_real_profile(self) -> None:
        p = profile_for("gpt-4o-mini")
        assert p.name == "gpt-4o-mini"
        assert p.context_window_tokens == 128_000
        assert p.output_limit_tokens == 16_384

    def test_a_routed_pair_sees_the_fast_models_real_window(self) -> None:
        routed = profile_for("routed(gpt-6-luna+deepseek-v4-flash)")
        # 105 万（luna）而不是 100 万（只算 deepseek 那一侧）
        assert routed.context_window_tokens == 1_050_000
        # 字符预算仍取更宽的一侧
        assert routed.max_context_chars == 480_000

    def test_the_endpoint_dispatch_still_governs_pricing(self) -> None:
        """`profile_for` 现在认识 OpenAI 模型，但费率仍只由端点判定。"""
        assert provider_profile_for("gpt-4o-mini", "https://api.openai.com/v1").pricing.is_known
        assert not provider_profile_for(
            "gpt-4o-mini", "https://proxy.example.com/v1"
        ).pricing.is_known
        assert not provider_profile_for("gpt-4o-mini", "https://api.deepseek.com").pricing.is_known

    def test_an_unknown_model_still_falls_back_to_the_default(self) -> None:
        assert profile_for("totally-unknown-model") is DEFAULT_PROFILE
