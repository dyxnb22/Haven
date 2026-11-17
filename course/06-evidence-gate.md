# 模块 06——证据门禁与确定性审查

[English（早期稿，已被本课取代）](archive/en/06-evidence-gate.md) | **中文（当前）**

> 文件：`src/haven/domain/evidence.py`、`src/haven/domain/review.py`
> 测试：`tests/unit/test_evidence_gate.py`、`tests/unit/test_review.py`、
> `tests/integration/test_agent_journeys.py`
> ADR：[0003——Evidence Gate](../docs/adr/0003-evidence-gate.md)、
> [0007——subagent、MCP 与确定性审查](../docs/adr/0007-subagents-mcp-and-deterministic-review.md)

## 学习目标

- 根据产物而不是模型自述判断运行是否成功；
- 给证据排序，让编辑前的旧通过结果不能冒充编辑后的验证；
- 识别一个注定无法通过的门禁，及时停止而不是无限催促模型；
- 用零 token 的确定性审查拦住几类明显灾难。

## 本课路线

- **前置：** 模块 05 的序列号、停止原因和有限循环。
- **建议用时：** 45–60 分钟。
- **阅读顺序：** 先看 evidence gate 的九个单元测试并预测结果，再读纯函数；最后看 review 规则，
  不要先进入完整 agent journey。
- **最小验证：**

```bash
uv run pytest -q tests/unit/test_evidence_gate.py tests/unit/test_review.py
```

## 门禁究竟在守什么

模型的“完成了”不是仓库状态。`evidence.py` 中的 `evaluate_evidence_gate` 由程序判定：

- 没有编辑的运行，只要给出最终回答即可成功；
- 编辑过文件的运行，必须在**最后一次写入之后**同时记录 `repo.diff` 和至少一次通过的
  `repo.check`；
- 检查失败、diff 缺失或 check 缺失，都会阻止成功。

每条证据都有序列号，所以最后一次写入之前的 check 不算数。这个顺序是整个门禁的关键：没有它，
代理可以先跑绿测试，再编辑一次，最后仍然被判定为成功。

证据不足时，循环会把具体失败原因反馈给模型，有限次要求它补齐；如果仍然没有证据，就以
`evidence_missing` 停止，而不是报告虚假成功。

## 注定无法通过的门禁：要能停下来

一次在线运行把整个工具预算烧光了，原因不是模型能力，而是任务一开始就没有登记任何 check recipe。
模型只要编辑文件，门禁就要求一项不可能存在的通过检查；循环却持续要求它达到这个不可达的结果。

修复方式是区分两种情况：

- “现在失败，再试一次”；
- “无论再试多少次都不可能通过”。

阅读 `evidence.py` 的 `verification_available` 分支和 `GateResult.terminal` 字段。发生了写入但
没有 verifier 时，直接以 `verification_unavailable` 终止，而不是耗尽预算后把锅甩给模型。系统 prompt
也应当在没有配置 check 时停止要求模型运行它。

任何重试循环都必须能识别“注定无法通过”，并立即退出。

## 确定性审查：不要用模型解决定义清楚的问题

ADR 0007 曾评估模型驱动的 Reviewer subagent，但它在线下无法通过自己的收益门禁：脚本模型只会
“发现”脚本预先写好的缺陷。于是采用对 diff 做确定性审查的备用方案。

`review.py` 只检查本次运行新增的行，标记：

- 私钥块、AWS key、`sk-…` token、硬编码密码等已提交凭据，并用 placeholder allowlist 避免把
  `changeme` 误报；
- merge conflict 标记；
- `breakpoint()`、`pdb.set_trace()`、`debugger;` 等调试残留；
- 丢失超过 80% 且超过 50 行的文件，通常意味着文件被清空。

发现问题就以 `review_failed` 阻止成功，并把结果反馈给代理。审查只看新增行，因此不会追究仓库原有内容；
它不消耗 token，也不需要第二个模型。

缺陷类别能明确表达时优先使用确定性检查；把模型留给真正模糊的问题。

## 延伸阅读

- ADR 0003 和 ADR 0007。
- `docs/EVAL_LIVE.md`：注定无法通过的门禁是怎样被发现的。
- 提交 `31fde25`（`fix(evidence)`）：独立的不可达门禁修复。
