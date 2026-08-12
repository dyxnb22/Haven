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
