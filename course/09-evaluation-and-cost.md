# 模块 09——评估与成本

[English](archive/en/09-evaluation-and-cost.md) | **中文**

> 文件：`src/haven/evalkit/runner.py`、`evals/generate_cases.py`、
> `evals/compare_prompt.py`、`docs/EVAL.md`、`docs/EVAL_LIVE.md`
> 测试：`tests/eval/test_eval_suite.py`
> ADR：[0005——离线评估与脚本模型](../docs/adr/0005-offline-eval-and-scripted-model.md)、
> [0008——prompt-cache 前缀稳定性](../docs/adr/0008-prompt-cache-prefix-stability.md)

## 学习目标

- 构建可复现、同时能充当安全门禁的离线评估；
- 把质量指标和安全指标分开报告，避免平均数掩盖回归；
- 明确只有在线运行才能回答的问题；
- 用前后测量和诚实的限定做成本工程。

## 本课路线

- **前置：** 模块 02 的 scripted/real provider 边界，模块 06 的 Evidence Gate。
- **建议用时：** 60 分钟；在线练习另计，并会产生费用。
- **阅读顺序：** 先读一个 case JSON，再看 runner 如何建立临时工作区，最后对照报告字段；只有在能
  解释离线评估边界后才阅读在线历史数字。
- **最小验证：**

```bash
uv run pytest -q tests/eval/test_eval_suite.py
uv run haven eval --offline
```

## 把离线评估当作 CI 门禁

打开 `evalkit/runner.py`。每个用例是 `evals/cases/` 中的 JSON：它指定一个 fixture、脚本模型的
turn、注册的检查 recipe 和预期结果。运行时复制 fixture 到临时目录，使用真实的 RunService、工具管线、
文件系统和子进程执行器，只有模型被替换成脚本模型。

因此评估快速、免费、确定，同时覆盖模型之外的真实应用栈。

无论用例自己的预期是什么，每个用例都必须满足两条全局不变量：

1. `expect.allowed_changed_files` 之外不能有文件变化；
2. `transcript_must_not_contain` 指定的禁止字符串不能进入模型 transcript。

安全不变量与任务成功分别报告，不能平均成一个分数；高任务成功率不能掩盖安全回归。
`haven eval --offline` 会单独打印 `security violations: N`，只要不是 0，CI 就失败。

`tests/eval/test_eval_suite.py` 把整套评估当普通测试运行，这样门禁不会因为没人手动运行而腐烂。

## 离线能测什么，不能测什么

离线套件擅长验证机制：policy 是否拒绝越界、审批是否一次性、Evidence Gate 是否按顺序取证、恢复
是否正确分类。它不能证明模型会不会面对一个真实仓库找到正确修复，因为脚本模型的轨迹是预先写好的。

要测真实工作能力，就需要在线评估：固定第三方仓库的 commit，注入一个小 bug，让项目自己的测试担任
oracle，确认有 bug 时为红、还原后为绿，再用真实 provider 驱动运行。`docs/EVAL_LIVE.md` 记录了这类
运行，也记录了它暴露出的六类真实缺陷：丢失推理内容、带命名空间工具名被拒绝、错误 body 被丢弃、
工具错误中止整个运行、注定无法通过的 evidence gate，以及范围配置错误。

可迁移的结论是：fake 能验证你的**机制**，不能验证你对真实 provider 线格式和真实模型行为的假设。
两种测试都需要，但必须说清每种测试抓得到、抓不到什么。

## 成本工程要先测量

第一次在线运行中，每个短任务大约消耗 26k 输入 token，于是项目开始做 prompt-cache 优化。过程本身就是
一堂成本课：

1. **测基线。** 8 个任务的命中率是 71%。
2. **找机制。** 易变的预算计数器位于第二条消息；前缀缓存从开头匹配，所以后面的 transcript 反复计费。
3. **修改并复测。** 把易变内容移到尾部后，命中率升到 89%，输入 token 从 127k 降到 114k。
4. **说明不能证明什么。** 固定的 system+tools 前缀本来就会缓存，“之前”的 71% 并非全部来自这次
   改动；收益主要作用于 transcript，会随运行长度增长，在这些短任务上约为 10%。前后通过数从 8 变 7
   是模型噪声，不能归因于这项修改。

`evals/compare_prompt.py` 可以离线计算上下文变化带来的确定性部分（新增字节/token）。在线百分比
位于 `EVAL_LIVE.md`，并明确标成不可复现。能精确计算就计算，否则标成估计并说明边界。

## 延伸阅读

- ADR 0005 与 ADR 0008。
- 完整阅读 `docs/EVAL.md`、`docs/EVAL_LIVE.md`。
- 提交 `5d77d59`（`feat(eval)`）、`9d8e684` / `c754f0f`（缓存与计费）。
