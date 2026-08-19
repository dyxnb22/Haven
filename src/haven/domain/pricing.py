"""Token 定价：对提供商公布的费率进行纯算术运算。"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Pricing:
    """模型输入、输出和缓存 token 的单价表。"""

    #: 每一百万个未命中缓存的输入 token 收取的美元费用。
    input_per_1m_usd: float = 0.0
    #: 每一百万个生成输出 token 收取的美元费用。
    output_per_1m_usd: float = 0.0
    #: 支持前缀缓存的提供商对命中缓存的计费远低于未命中；DeepSeek v4 flash
    #: 的差距是 50 倍。如果不设置该字段，所有输入都按同一费率计费，从而
    #: 保证现有配置的含义完全不变。
    cached_input_per_1m_usd: float | None = None

    def __post_init__(self) -> None:
        """拒绝会产生负账单或绕过费用预算的非有限/负费率。"""
        rates = (
            self.input_per_1m_usd,
            self.output_per_1m_usd,
            self.cached_input_per_1m_usd,
        )
        if any(
            rate is not None
            and (
                isinstance(rate, bool)
                or not isinstance(rate, int | float)
                or not math.isfinite(rate)
                or rate < 0
            )
            for rate in rates
        ):
            raise ValueError("pricing rates must be finite and non-negative")

    @property
    def is_known(self) -> bool:
        """费率卡是否确实已提供。

        全零费率表示没有人配置费率，并不表示提供商免费。渲染器在显示金额前必须
        查询此属性，否则未配置费率的模型会把一次实际产生费用的运行报告为
        `$0.0000`。在这种情况下，`cost()` 仍返回 0.0，因为另一种做法需要把可选
        浮点数贯穿检查点和事件模式，而调用者无法使用那个值；诚实性应体现在显示
        金额的地方，这与 `Usage.estimated` 对 token 数量采用的区分相同。
        """
        return self.input_per_1m_usd > 0.0 or self.output_per_1m_usd > 0.0

    def cost(self, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0) -> float:
        """按输入、输出及缓存命中 token 数计算美元费用。"""
        output_cost = output_tokens * self.output_per_1m_usd
        if self.cached_input_per_1m_usd is None:
            return (input_tokens * self.input_per_1m_usd + output_cost) / 1_000_000
        # 限制范围：提供商报告的缓存量不能大于总量，否则可能算出负账单。
        cached = max(0, min(cached_input_tokens, input_tokens))
        fresh = input_tokens - cached
        input_cost = cached * self.cached_input_per_1m_usd + fresh * self.input_per_1m_usd
        return (input_cost + output_cost) / 1_000_000
