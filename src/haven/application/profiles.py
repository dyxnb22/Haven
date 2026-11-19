"""按模型配置的默认值。

Haven 实际上面向一个模型，但核心仍保持与模型无关：模型公开行为会影响默认值的地方
都在这里以数据形式集中定义，因此代理循环、策略或上下文构建器都不需要根据模型名称
分支处理。

未知模型会获得 `DEFAULT_PROFILE`，这正是 Haven 的历史行为——陌生提供商继承保守默认值，
而不是使用根据名称相似模型猜出的数字。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from haven.domain.pricing import Pricing

#: profiles 引入前 Haven 使用的预算。
DEFAULT_CONTEXT_CHARS = 96_000


@dataclass(frozen=True, slots=True)
class ModelProfile:
    #: 配置中使用的模型名称；也作为 profile 查找的键。
    name: str
    #: 上下文构建器允许纳入请求的字符预算。
    max_context_chars: int = DEFAULT_CONTEXT_CHARS
    #: 输入、输出及缓存 token 的计价配置。
    pricing: Pricing = field(default_factory=Pricing)
    #: 仅在设置后传给提供商，因此未设置表示“使用提供商默认值”，而不是
    #: Haven 选择的某个值。
    reasoning_effort: str | None = None
    #: 提供商是否要求在后续请求中重放工具调用之前的推理（DeepSeek V4 要求；
    #: ADR 0014）。
    requires_tool_call_reasoning: bool = False
    #: 提供商实际的输入 token 窗口。仅用于安全检查：即使按观测到的最密集
    #: chars-per-token 比例计算，`max_context_chars`（字符预算）暗示的 token
    #: 数也必须保持在此窗口以内。这样手动设置的字符预算是经过测量和检查的，
    #: 而不是猜出来的（ROADMAP3 phase 5，由 evals/calibrate_context.py 验证）。
    context_window_tokens: int = 0  # 0 = 未知；跳过安全检查
    #: 提供商是否能从自身前缀继续生成被截断的 assistant 消息（DeepSeek 的
    #: beta prefix-completion）。为 false 时改用 Haven 的对话式续写垫片。
    supports_assistant_prefix: bool = False
    #: 前缀续写所需的 endpoint（如果有）。DeepSeek 只在 beta base URL 接受
    #: `prefix: true`，稳定版会返回 400，因此指向其他地址的运行必须回退到
    #: 垫片，而不能让每个截断轮次都失败。为空表示该能力不需要特殊 endpoint。
    prefix_beta_base_url: str = ""
    #: 流式事件之间允许的最大空闲间隔，超出后适配器超时；每个事件到达时
    #: 重置（不是整个流的截止时间；ADR 0014 面向的是可能连续推理数分钟的
    #: 推理模型）。运行整体仍由墙上时钟预算限制，因此这里仅需足够长，以区分
    #: 真正卡住和缓慢但仍在生成的情况。
    stream_idle_timeout_s: float = 120.0
    reasoning_supported: bool | None = None
    output_limit_tokens: int = 0
    documentation_status: str = ""
    use_max_completion_tokens: bool = False

    def prefix_continuation_enabled(self, base_url: str) -> bool:
        """判断是否可以针对 `base_url` 使用原生前缀续写。

        只有模型支持该能力，并且配置的 endpoint 是它要求的 endpoint（如果有）时才返回
        True。这个守卫可以避免在稳定版 DeepSeek endpoint 上对截断轮次发送 `prefix: true`
        并必然得到 400；这种情况下会改用垫片。
        """
        if not self.supports_assistant_prefix:
            return False
        if not self.prefix_beta_base_url:
            return True
        return base_url.rstrip("/") == self.prefix_beta_base_url.rstrip("/")

    #: 在线评估语料中观测到的最密集 chars-per-token 比例（evals/calibrate_context.py
    #: 统计 report-*/events 流：最小值约 0.6，p10 约 1.0，中位数约为每个输入
    #: token 对应 1.8 个片段内容字符；低端来自固定工具 schema 占据小上下文的请求）。
    #: 保守值 1.0 表示“假设每个纳入预算的字符都可能消耗一个完整 token”，这是
    #: 保持低于窗口时的最坏情况。


MEASURED_MIN_CHARS_PER_TOKEN = 1.0


DEFAULT_PROFILE = ModelProfile(name="default")

# 标准文本费率与模型能力见 OpenAI 的 GPT-4o mini 型号文档（2026-11 核对）。
OPENAI_GPT_4O_MINI = ModelProfile(
    name="gpt-4o-mini",
    pricing=Pricing(
        input_per_1m_usd=0.15,
        output_per_1m_usd=0.60,
        cached_input_per_1m_usd=0.075,
    ),
    context_window_tokens=128_000,
    reasoning_supported=False,
    output_limit_tokens=16_384,
    documentation_status="OpenAI announced shutdown for 2027-01-14",
)

# Chat Completions 的 function calling 需要显式 reasoning_effort=none。
# Luna 有缓存写入和长上下文阶梯计价，Haven 的三档费率卡无法表达完整规则。
OPENAI_GPT_6_LUNA = ModelProfile(
    name="gpt-6-luna",
    reasoning_effort="none",
    context_window_tokens=1_050_000,
    reasoning_supported=True,
    output_limit_tokens=128_000,
    use_max_completion_tokens=True,
    documentation_status="Chat Completions tool calls require effort none",
)

_OPENAI_PROFILES = {
    OPENAI_GPT_4O_MINI.name: OPENAI_GPT_4O_MINI,
    "gpt-4o-mini-2024-07-18": OPENAI_GPT_4O_MINI,
    OPENAI_GPT_6_LUNA.name: OPENAI_GPT_6_LUNA,
}

#: 截至 2026-08 的已发布行为：输入和输出共享 1M token 窗口，缓存命中价格
#: 是未命中的五十分之一。
#:
#: 更大的字符预算并非没有代价：携带更多缓存 token 会使每轮成本略有增加。
#: 它通过避免重新读取来回本；重新读取按未命中费率计费，还会额外消耗一步。
#: `evals/ab.py` 估计每十轮大约避免一次重读即可达到盈亏平衡。它仍远低于
#: 窗口上限，因为延迟和真正新增内容按未命中费率产生的成本都会随请求大小增长。
DEEPSEEK_V4_FLASH = ModelProfile(
    name="deepseek-v4-flash",
    max_context_chars=480_000,
    pricing=Pricing(
        input_per_1m_usd=0.14,
        output_per_1m_usd=0.28,
        cached_input_per_1m_usd=0.0028,
    ),
    requires_tool_call_reasoning=True,
    reasoning_supported=True,
    # 1M token 窗口（2026-08 发布）。按测得的最坏情况 1.0 字符/token，
    # 480k 字符约等于 480k token——不到窗口的一半；实际约 1.8 字符/token
    # 时则接近 270k。字符预算是成本/延迟选择（ADR 0011），不是安全上限；
    # 这个窗口让该结论可以被证明，而不只是断言。
    context_window_tokens=1_000_000,
    # 思考模式（`high`/`max` effort）可能连续数分钟流式输出推理；总截止时间
    # 会杀掉健康的生成，因此这里限制的是宽松的事件间空闲间隔。墙上时钟预算
    # 仍会限制整个运行。
    stream_idle_timeout_s=300.0,
    # 2026-08-13 在线确认（ADR 0022 的待决门禁）：在 beta endpoint 上，带有
    # `prefix: true` 的末尾 assistant 消息会被原地扩展；稳定 endpoint 会返回
    # 明确指出 beta base URL 的 400。因此该能力确实存在，但绑定 endpoint，
    # 所以要连同满足条件的 endpoint 一起声明，而不能只声明一个裸标志。
    supports_assistant_prefix=True,
    prefix_beta_base_url="https://api.deepseek.com/beta",
)

#: `deepseek-chat` 和 `deepseek-reasoner` 是 DeepSeek 文档中 v4-flash 非思考
#: 与思考模式的旧别名，使用相同费率卡和相同的 1M 窗口（api-docs.deepseek.com，
#: 将于 2026-07-24 退役）。在线运行实际发送的就是这些名称；如果将它们解析为
#: `DEFAULT_PROFILE`，这些运行会静默获得 96k 而非 480k 字符预算，并且每次
#: 都按 $0.00 计费。
_LEGACY_DEEPSEEK_ALIASES = ("deepseek-chat", "deepseek-reasoner")

_PROFILES: dict[str, ModelProfile] = {
    DEEPSEEK_V4_FLASH.name: DEEPSEEK_V4_FLASH,
    **dict.fromkeys(_LEGACY_DEEPSEEK_ALIASES, DEEPSEEK_V4_FLASH),
}


def profile_for(model_name: str) -> ModelProfile:
    """返回指定模型的 profile，找不到时返回保守默认值。

    对双轨混合路由器（`routed(fast+deep)`），自适应继承深度模型的上下文上限、
    定价策略、推理要求与前缀续写能力，避免双轨模式被退化为 96k 的保守默认值。
    """
    if model_name in _PROFILES:
        return _PROFILES[model_name]

    if model_name.startswith("routed(") and model_name.endswith(")") and "+" in model_name:
        inner = model_name[7:-1]
        fast_name, _, deep_name = inner.partition("+")
        fast_p = profile_for(fast_name)
        deep_p = profile_for(deep_name)
        pricing = deep_p.pricing if deep_p.pricing.is_known else fast_p.pricing
        # 用 `replace(deep_p, …)` 而不是手写一个 ModelProfile：手写会把此后新增的
        # 每一个字段静默丢成默认值。这里此前就漏了 reasoning_supported、
        # output_limit_tokens、documentation_status 和 use_max_completion_tokens 四个
        # （当前注册的 profile 恰好都没设它们，所以是潜伏缺陷）——其中
        # `use_max_completion_tokens` 直接决定请求里用 max_tokens 还是
        # max_completion_tokens，丢了就是每轮一个 400。
        return replace(
            deep_p,
            name=model_name,
            pricing=pricing,
            # 双轨的字符预算必须装得下任一侧的请求，所以取更宽的那个。
            max_context_chars=max(fast_p.max_context_chars, deep_p.max_context_chars),
            context_window_tokens=max(fast_p.context_window_tokens, deep_p.context_window_tokens),
            stream_idle_timeout_s=max(fast_p.stream_idle_timeout_s, deep_p.stream_idle_timeout_s),
        )

    return DEFAULT_PROFILE


def provider_profile_for(model_name: str, base_url: str) -> ModelProfile:
    """官方费率和文档能力只匹配公开服务的地址。"""
    endpoint = base_url.rstrip("/")
    if model_name in _OPENAI_PROFILES:
        return (
            _OPENAI_PROFILES[model_name]
            if endpoint == "https://api.openai.com/v1"
            else DEFAULT_PROFILE
        )
    profile = profile_for(model_name)
    if profile is DEEPSEEK_V4_FLASH and endpoint not in {
        "https://api.deepseek.com",
        "https://api.deepseek.com/v1",
        "https://api.deepseek.com/beta",
    }:
        # 保留已知 DeepSeek 兼容代理的线协议与流超时行为，费率和窗口由端点决定。
        return replace(
            DEFAULT_PROFILE,
            requires_tool_call_reasoning=profile.requires_tool_call_reasoning,
            stream_idle_timeout_s=profile.stream_idle_timeout_s,
        )
    return profile
