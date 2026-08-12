"""A model's published rate card is only worth carrying if a run consults it.

Without this, `cost_usd` reads $0.0000 for a model whose prices Haven knows,
and the profile's pricing is dead data.
"""

from pathlib import Path

from haven.application.profiles import DEEPSEEK_V4_FLASH
from haven.domain.pricing import Pricing
from tests.integration.harness import Harness, finish, make_repo, text, tool, usage


def read_turns() -> list[list[object]]:
    return [
        [tool("c1", "repo.read", path="src/calc.py"), finish("tool_calls"), usage(2000, 100)],
        [text("Read it."), finish(), usage(2000, 100)],
    ]


class TestPricingComesFromTheProfile:
    async def test_a_known_model_is_priced_without_any_config(self, tmp_path: Path) -> None:
        h = Harness(make_repo(tmp_path), read_turns(), model_name="deepseek-v4-flash")  # type: ignore[arg-type]
        outcome = await h.service.run("Read calc.py")
        assert outcome.cost_usd > 0.0

    async def test_an_unknown_model_is_still_priced_at_zero(self, tmp_path: Path) -> None:
        """Haven must not invent a price for a model it does not know."""
        h = Harness(make_repo(tmp_path), read_turns(), model_name="mystery-model")  # type: ignore[arg-type]
        outcome = await h.service.run("Read calc.py")
        assert outcome.cost_usd == 0.0

    async def test_configured_rates_win_over_the_profile(self, tmp_path: Path) -> None:
        expensive = Pricing(input_per_1m_usd=1000.0, output_per_1m_usd=1000.0)
        h = Harness(
            make_repo(tmp_path),
            read_turns(),  # type: ignore[arg-type]
            model_name="deepseek-v4-flash",
            pricing=expensive,
        )
        outcome = await h.service.run("Read calc.py")

        flash_cost = DEEPSEEK_V4_FLASH.pricing.cost(outcome.input_tokens, outcome.output_tokens)
        assert outcome.cost_usd > flash_cost * 100
