"""Per-model defaults, expressed as data rather than as branches in the core."""

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
        """An unfamiliar model must inherit today's behavior, not a guess."""
        assert profile_for("some-model-nobody-has-heard-of") is DEFAULT_PROFILE

    def test_the_default_keeps_the_historical_context_budget(self) -> None:
        assert DEFAULT_PROFILE.max_context_chars == DEFAULT_CONTEXT_CHARS == 96_000

    def test_the_default_prices_nothing(self) -> None:
        """Cost must stay zero for an unknown model rather than invented."""
        assert DEFAULT_PROFILE.pricing.input_per_1m_usd == 0.0
        assert DEFAULT_PROFILE.pricing.cached_input_per_1m_usd is None


class TestFlashProfile:
    def test_its_context_budget_is_far_larger_than_the_default(self) -> None:
        """Retained prefix bills at the hit rate, so compacting early costs
        money on this model rather than saving it."""
        assert DEEPSEEK_V4_FLASH.max_context_chars > 4 * DEFAULT_CONTEXT_CHARS

    def test_its_context_budget_stays_inside_the_window_at_the_measured_worst_case(
        self,
    ) -> None:
        """The char budget must imply a token count under the window even at
        the densest chars-per-token ratio observed live (evals/
        calibrate_context.py), so the hand-set constant is checked, not guessed."""
        from haven.application.profiles import MEASURED_MIN_CHARS_PER_TOKEN

        worst_case_tokens = DEEPSEEK_V4_FLASH.max_context_chars / MEASURED_MIN_CHARS_PER_TOKEN
        assert DEEPSEEK_V4_FLASH.context_window_tokens > 0
        assert worst_case_tokens < DEEPSEEK_V4_FLASH.context_window_tokens

    def test_it_prices_cache_hits_far_below_misses(self) -> None:
        pricing = DEEPSEEK_V4_FLASH.pricing
        assert pricing.cached_input_per_1m_usd is not None
        assert pricing.input_per_1m_usd / pricing.cached_input_per_1m_usd > 10

    def test_it_leaves_reasoning_effort_to_the_provider(self) -> None:
        """No default is changed on a guess; the A/B harness measures it first."""
        assert DEEPSEEK_V4_FLASH.reasoning_effort is None

    def test_it_allows_a_longer_idle_stream_gap_than_the_default(self) -> None:
        """Thinking mode can pause between streamed tokens longer than a
        non-reasoning model would, so the idle bound is more generous here."""
        assert DEEPSEEK_V4_FLASH.stream_idle_timeout_s > DEFAULT_PROFILE.stream_idle_timeout_s

    def test_it_supports_native_prefix_continuation_on_the_beta_endpoint(self) -> None:
        """Confirmed live 2026-08: the beta endpoint extends an assistant
        `prefix: true` message in place; the stable endpoint 400s on it. So the
        capability is real but only when pointed at the beta endpoint."""
        assert DEEPSEEK_V4_FLASH.supports_assistant_prefix is True
        assert DEEPSEEK_V4_FLASH.prefix_continuation_enabled("https://api.deepseek.com/beta")
        assert DEEPSEEK_V4_FLASH.prefix_continuation_enabled("https://api.deepseek.com/beta/")
        assert not DEEPSEEK_V4_FLASH.prefix_continuation_enabled("https://api.deepseek.com")


class TestLegacyAliases:
    """`deepseek-chat` and `deepseek-reasoner` are documented aliases of
    v4-flash (non-thinking and thinking mode), retiring 2026-07-24. They are
    the names a live run actually used, and resolving them to DEFAULT_PROFILE
    silently gave those runs a 96k context budget and a $0.00 bill."""

    def test_the_legacy_chat_alias_resolves_to_flash(self) -> None:
        assert profile_for("deepseek-chat") is DEEPSEEK_V4_FLASH

    def test_the_legacy_reasoner_alias_resolves_to_flash(self) -> None:
        assert profile_for("deepseek-reasoner") is DEEPSEEK_V4_FLASH

    def test_a_profiled_model_prices_a_real_run(self) -> None:
        """The regression that matters: a run on this model must not bill $0."""
        assert profile_for("deepseek-chat").pricing.is_known is True

    def test_an_unknown_model_still_reports_an_unknown_price(self) -> None:
        assert profile_for("some-model-nobody-has-heard-of").pricing.is_known is False


class TestPrefixEndpointGuard:
    """A profile that needs a specific endpoint for prefix continuation only
    enables it there, so a truncated turn on the wrong endpoint falls back to
    the shim instead of a guaranteed 400."""

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
