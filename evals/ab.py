"""离线上下文变体的 A/B 比较，使用模型的真实费率卡计价。

这里的一切都是确定性的，不需要网络：它测量每个变体*发送*出去的成本，而不是
代理执行得多好。质量是实时问题，本脚本有意不回答它——见生成报告的结尾部分。

从仓库根目录运行：  uv run python evals/ab.py
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from haven.application.context_builder import ContextBuilder
from haven.application.profiles import DEEPSEEK_V4_FLASH, DEFAULT_PROFILE
from haven.contracts.model import ModelMessage
from haven.contracts.tools import tool_schemas
from haven.domain.budget import BUDGET_TIERS, Budget, BudgetUsage

OUT = Path(__file__).parent.parent / "eval_report" / "ab-report.md"

#: 英文散文的粗略行业约定。这里只用于数量级估算；实际的 token 拆分来自提供方的用量字段。
CHARS_PER_TOKEN = 4

GOAL = "Fix the bug in add() and verify with the calc recipe"


@dataclass(frozen=True, slots=True)
class Variant:
    name: str
    max_context_chars: int
    budget: Budget
    transcript_turns: int
    note: str


#: 所有变体都使用同一张费率表，因为这里要比较的是给定上下文预算
#: “在这个模型上”的成本。若用默认配置中空的费率表为旧默认值定价，就没有可比结果。
RATE_CARD = DEEPSEEK_V4_FLASH.pricing


def synthetic_transcript(turns: int) -> list[ModelMessage]:
    """一种合理的偏读取历史：每一轮读取一个 8 KB 文件。"""
    messages: list[ModelMessage] = []
    for index in range(turns):
        body = json.dumps(
            {
                "status": "ok",
                "result": {
                    "path": f"src/module_{index:02d}.py",
                    "digest": f"{index:08x}",
                    "content": "x" * 8_000,
                },
            }
        )
        messages.append(
            ModelMessage(
                role="tool",
                content=f'<tool_output tool="repo.read">\n{body}\n</tool_output>',
                tool_call_id=f"c{index}",
            )
        )
        messages.append(ModelMessage(role="assistant", content=f"Read module {index}."))
    return messages


def measure(variant: Variant) -> dict[str, float]:
    builder = ContextBuilder(
        goal=GOAL,
        tools=tool_schemas(),
        budget=variant.budget,
        recipe_ids=("verify-calc",),
        sandbox_backend="seatbelt",
        max_context_chars=variant.max_context_chars,
    )
    transcript = synthetic_transcript(variant.transcript_turns)
    request, segments = builder.build(transcript, BudgetUsage(steps=variant.transcript_turns))

    chars = sum(len(m.content) for m in request.messages)
    tokens = chars // CHARS_PER_TOKEN
    compacted = any(segment.source == "run_digest" for segment in segments)

    # 稳定前缀上的稳态轮次：除了最新的工具输出之外，其余内容都已缓存。
    # 这正是 ADR 0008 所针对的使用形态。
    fresh = min(tokens, 8_000 // CHARS_PER_TOKEN)
    cached = tokens - fresh
    return {
        "chars": chars,
        "approx_tokens": tokens,
        "compacted": float(compacted),
        "cost_all_miss": RATE_CARD.cost(tokens, 0),
        "cost_steady_state": RATE_CARD.cost(tokens, 0, cached_input_tokens=cached),
    }


VARIANTS = (
    Variant(
        name="96k budget / standard tier",
        max_context_chars=DEFAULT_PROFILE.max_context_chars,
        budget=BUDGET_TIERS["standard"],
        transcript_turns=16,
        note="Haven's budget before per-model profiles. Compacts on this history.",
    ),
    Variant(
        name="480k budget / standard tier",
        max_context_chars=DEEPSEEK_V4_FLASH.max_context_chars,
        budget=BUDGET_TIERS["standard"],
        transcript_turns=16,
        note="Same history, this model's budget: nothing is dropped.",
    ),
    Variant(
        name="480k budget / deep tier",
        max_context_chars=DEEPSEEK_V4_FLASH.max_context_chars,
        budget=BUDGET_TIERS["deep"],
        transcript_turns=48,
        note="A long run the deep tier permits and the larger budget absorbs.",
    ),
)


def main() -> None:
    rows = [(variant, measure(variant)) for variant in VARIANTS]

    lines = [
        "# Context A/B (offline, deterministic)",
        "",
        "Each variant builds a real first-turn request from a synthetic read-heavy",
        "history and prices it with the DeepSeek v4 flash rate card. Every row uses",
        "the same rate card, because the question is what a context budget costs on",
        "this model. No network, no provider call, no model quality claim.",
        "",
        "| variant | turns | chars | ~tokens | compacted | all-miss cost | steady-state cost |",
        "|---|---:|---:|---:|:--:|---:|---:|",
    ]
    for variant, m in rows:
        lines.append(
            f"| {variant.name} | {variant.transcript_turns} | {int(m['chars']):,} | "
            f"{int(m['approx_tokens']):,} | {'yes' if m['compacted'] else 'no'} | "
            f"${m['cost_all_miss']:.6f} | ${m['cost_steady_state']:.6f} |"
        )

    lines += ["", "## Notes", ""]
    lines += [f"- **{variant.name}** — {variant.note}" for variant, _ in rows]

    assert RATE_CARD.cached_input_per_1m_usd is not None
    ratio = RATE_CARD.input_per_1m_usd / RATE_CARD.cached_input_per_1m_usd

    small, large = rows[0][1], rows[1][1]
    per_turn_penalty = large["cost_steady_state"] - small["cost_steady_state"]
    reread_tokens = 8_000 // CHARS_PER_TOKEN
    reread_cost = RATE_CARD.cost(reread_tokens, 0)
    break_even = reread_cost / per_turn_penalty if per_turn_penalty > 0 else float("inf")

    lines += [
        "",
        "## What the larger budget actually costs, and what it buys",
        "",
        "The obvious argument — that a cache hit costs "
        f"1/{ratio:.0f} of a miss, so keeping context is free — is **not** what the",
        "numbers show. Cached tokens are cheap, not free, so carrying more of them",
        f"costs slightly more per turn: **${per_turn_penalty:.6f}** in the two rows above.",
        "",
        "The case for the larger budget is avoided re-reads, and it is quantifiable.",
        "When compaction drops a file the agent still needs, it must read it again:",
        f"one 8 KB re-read is ~{reread_tokens:,} fresh tokens at the miss rate, or",
        f"**${reread_cost:.6f}** — plus a step and a tool call against the budget.",
        "",
        "So the larger budget pays for itself if it avoids one re-read every",
        f"**{break_even:.0f} turns**. On a read-heavy task that is an easy bar to clear,",
        "and the step it saves is often worth more than the fraction of a cent.",
        "",
        "The honest summary: this is a modest, bounded trade, not a free win.",
        "",
        "## What this does not measure",
        "",
        "Task success. Every variant here replays the same synthetic history, so this",
        "is a measurement of request size and price, not of agent quality. Comparing",
        "quality needs `haven eval --live`, a real key, and a sample larger than this",
        "project has run. See `docs/EVAL_LIVE.md` for the live numbers actually",
        "observed, and for the numbers still marked as not yet measured.",
    ]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[: 8 + len(rows)]))
    print(f"\nwritten to {OUT}")


if __name__ == "__main__":
    main()
