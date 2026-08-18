# 模块 06——证据门禁与确定性审查

[English](archive/en/06-evidence-gate.md) | **中文**

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

## 门禁究竟在守什么

模型无论有没有真正修改，通常都很愿意说“完成了”。如果程序接受这句话，测到的只是模型的自信，
不是仓库的状态。`evidence.py` 中的 `evaluate_evidence_gate` 把判断权交还给程序：

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

一般规则比这次 bug 更重要：任何重试或催促循环都必须有“注定无法通过”的出口，否则预算只是被烧掉。

## 确定性审查：不要用模型解决定义清楚的问题

ADR 0007 曾评估模型驱动的 Reviewer subagent，但它在线下无法通过自己的收益门禁：脚本模型只会
“发现”脚本预先写好的缺陷。于是采用对 diff 做确定性审查的备用方案。

`review.py` 只检查本次运行新增的行，标记：

- 私钥块、AWS key、`sk-…` token、硬编码密码等已提交凭据，并用 placeholder allowlist 避免把
  `changeme` 误报；
- merge conflict 标记；
- `breakpoint()`、`pdb.set_trace()`、`debugger;` 等调试残留；
- 丢失超过 80% 且超过 50 行的文件，通常意味着文件被清空。

发现问题就以 `review_failed` 阻止成功，并把结果反馈给代理，像修复失败检查一样处理。只检查新增行，
意味着仓库中原本就存在的内容不会触发本次审查。它不消耗 token，耗时不到毫秒，不需要第二个模型，
也没有抽样带来的漏报。

这是一条很通用的工程经验：缺陷类别定义清楚时，确定性检查在成本、延迟和可靠性上都胜过概率性检查；
把模型留给真正模糊的问题。

## 练习

1. **顺序很重要。** 构造一个通过 check 发生在最后编辑之前的证据账本，断言 gate 不通过；把 check
   移到编辑之后，再观察它通过。
2. **构造不可达门禁。** 没有 recipe，却让模型编辑文件；断言约三步后以
   `verification_unavailable` 停止，而不是耗尽预算。
3. **尝试欺骗审查。** 在 diff 中新增 AWS key，断言 `review_failed`；再只删除一行包含 secret 的内容，
   断言它不触发，因为审查只看新增行。
4. **扩展规则。** 先写测试，再增加“新增行超过 5000 字符”的检查，模拟意外粘贴大段内容。

## 自测

- “模型说完成了”为什么不够？编辑过文件的运行最少需要什么证据？
- 证据为什么必须带序列号？
- “注定无法通过的门禁”告诉了我们关于重试循环的什么一般规则？
- 什么情况下确定性检查比模型 reviewer 更合适？

## 延伸阅读

- ADR 0003 和 ADR 0007。
- `docs/EVAL_LIVE.md`：注定无法通过的门禁是怎样被发现的。
- 提交 `31fde25`（`fix(evidence)`）：独立的不可达门禁修复。
