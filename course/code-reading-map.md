# Haven 源码伴读索引

[返回课程总览](README.md)

这份索引把每课绑定到源码、测试和 Trace。学习时把对应行当作阅读任务单。

## 使用方法

每课只做四件事：

1. 用 `rg` 找到入口符号，不从目录里随机点文件；
2. 先读行为测试并预测断言，再读实现；
3. 在 Trace 或结构化结果中找到这段实现留下的可观察证据；
4. 运行定向命令，把“我认为”变成“我观察到”。

常用命令：

```bash
rg -n "class ModelRequest|class ToolCallProposal" src/haven tests
rg -n "def evaluate_policy|policy.decided" src/haven tests
uv run pytest -q path/to/test.py::TestClass::test_name
```

## 先认一条主链

第一次只追一个 `repo.edit`：

```text
ContextBuilder.build
  → ModelPort.generate_stream
  → RunActor.drive
  → RunActor._call_model_step
  → ModelResult.tool_calls
  → ToolPipeline.execute
  → ToolRegistry.validate
  → ToolFactsCollector.collect
  → evaluate_policy
  → ApprovalCoordinator.ask
  → mint_ticket
  → ToolExecutor.run_ticketed
  → ToolResult / EvidenceLedger / EventEmitter
  → AnswerResolver.finish_with_gate
  → RunService._finish
```

这些符号分散在不同文件，对应真实的依赖边界。

## 每课的项目锚点

| 课程 | 源码入口 | 先读测试/夹具 | 应观察的结果 |
|---|---|---|---|
| 预备 A | `contracts/model.py` 的 `ModelRequest`、`ToolCallProposal`、`ModelResult`；`run_actor.py::RunActor.drive` | `tests/contract/test_scripted_model.py` | `model.completed` 与 `tool.proposed` 是两类事件 |
| 预备 B | `ports/model.py::ModelPort`、`tests/integration/harness.py::Harness` | `test_scripted_model.py`、`test_policy.py` | 同一个 port 可装配真实或脚本模型 |
| 预备 C | `tests/golden/test_golden_trace.py::edit_journey_turns` | `test_edit_journey_matches_golden` | 六轮完整 `edit_journey.json` |
| 00 | `RunService`、`ToolPipeline`、workspace、evidence、recovery | `test_full_fix_with_evidence` | 十一道边界共同产生 `evidence_satisfied` |
| 01 | `RunContext`、`ContextBuilder.build`、`ModelResult`、`EventEnvelope` | `test_context_builder.py`、`test_transitions.py` | State、Context、单轮结果和持久事件不混用 |
| 02 | `ModelPort`、`OpenAICompatibleModel.generate_stream`、`ScriptedModel.generate_stream` | 两个 provider contract 测试 | SSE/脚本输入都归一为 `ModelEvent` |
| 03 | `ToolPipeline.execute`、`ToolRegistry.validate`、`mint_ticket` | `test_tool_error_containment.py` | proposal → policy → execution → completion |
| 04 | `evaluate_policy`、`compute_approval_digest`、workspace path facts | policy、approval、workspace escape 测试 | allow / ask / deny 与一次性审批 |
| 05 | `RunActor.drive`、`check_budget`、`StuckLoopDetector`、`ContextBuilder` | budget、stuck、context、provider retry 测试 | 每次停止都有 `StopReason` |
| 06 | `evaluate_evidence_gate`、`review_diff` | evidence gate、review 测试 | final text 只有通过 gate 才能成为成功 |
| 07 | `RecoveryService.inspect`、`ReplayService.replay`、`SqliteSessionStore` | recovery 与 session contract 测试 | not-run / confirmed / effect-unknown |
| 08 | `presenter.reduce`、`HavenApp`、`EventEmitter` | presenter、TUI、golden 测试 | live 与 replay 消费同一事件语义 |
| 09 | `evalkit/runner.py::run_suite`、`case_runner.py::run_case`、`evals/cases/*.json` | `test_eval_suite.py` | 任务质量与安全违规分开报告 |
| 10 | `docs/adr/`、`docs/POSTMORTEM.md`、项目门禁脚本 | ADR link、docs、gate graph 测试 | 决策、反例和回归保护形成闭环 |
| 结业 | 取决于所选路线，但必须穿过现有 port/policy/test 边界 | 新测试 + 相邻既有测试 | diff、验证结果和决策记录 |

## 三种阅读深度

### 第一层：能讲主链

只回答：输入是什么、输出是什么、谁调用它、失败怎样返回。此时不必展开 helper。

### 第二层：能解释边界

回答：为什么逻辑位于这一层，若移动到模型、UI 或 adapter 会破坏什么不变量。

### 第三层：能安全修改

先写失败测试，再修改最小实现；运行相邻测试、快速门禁和离线评估，并检查 Trace 是否发生预期变化。

第三层的标志是：面对新工具或 Provider，能指出需要修改的契约、策略、适配器和测试。

## 代码阅读记录模板

每学一课复制一次：

```text
入口符号：
调用者：
输入 / 输出类型：
正常路径：
失败路径与稳定错误码：
对应 Trace 事件：
固定行为的测试：
我实际运行的命令与结果：
```

## 哪些文件暂时不要先读

- `evals/real/`、`evals/headtohead/` 和生成的 `eval_report/`：不是入门源码路径；
- `docs/ROADMAP*.md` 与原始计划：用于理解历史，不代表当前行为；
- `interfaces/tui/app.py` 的 widget 细节：先学 `presenter.reduce`；
- `openai_compatible.py` 的每个 SSE 分支：先掌握 `ModelEvent` 契约；
- `sqlite_session.py` 的全部 SQL：先掌握 session port 与恢复三态。

先抓稳定边界，再进入实现细节。
