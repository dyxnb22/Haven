# 结业项目——扩展 Haven，或构建自己的版本

[English](archive/en/capstone.md) | **中文**

选择一条路线完成一个周末规模的实践。验收标准相同：测试通过、记录一个明确决定、每项声明可复现。

开始前：

```bash
uv sync --locked
uv run pytest -q && uv run haven eval --offline
git switch -c capstone/<your-track>
```

## 先选对路线

| 你的目标 | 推荐路线 | 为什么 |
|---|---|---|
| 第一次把课程知识串起来 | A：添加工具 | 会经过契约、policy、facts、执行、证据和测试的完整竖切 |
| 熟悉 LLM API / 流式协议 | B：Provider adapter | 集中练习核心契约与外部线格式的隔离 |
| 深入状态机、数据库与故障语义 | C：恢复演练 | 重点是证明副作用状态，而不是增加产品功能 |
| 练习技术决策与实验设计 | D：收益门禁 | 可能以“不构建”结束，但最能训练证据驱动的判断 |

没有明显偏好时选 A，它覆盖的项目边界最完整。

## 通用推进节奏

每条路线都按五个检查点推进：

1. **先写不变量。** 用三到五句话写清允许什么、拒绝什么、成功需要什么证据。
2. **先看到一个测试失败。** 写最小测试并确认它是因为缺少目标行为而失败，不是因为 fixture 配错。
3. **只做最小竖切。** 先贯通一条成功路径和一条失败路径，再扩展边缘情况。
4. **先跑定向测试。** 只运行改动附近的测试，失败时更容易定位；通过后再运行快速门禁。
5. **最后跑全量离线验证。** 检查 diff，再运行 pytest 和 offline eval，记录实际命令与结果。

在 `CAPSTONE.md` 中持续维护：

| 检查点 | 证据 | 状态 |
|---|---|---|
| 不变量已写清 | 文档链接或段落 | pending |
| 失败测试已观察 | 命令 + 失败原因 | pending |
| 成功/失败路径已贯通 | 测试名 | pending |
| 快速门禁通过 | 命令 + 结果 | pending |
| 全量测试与离线评估通过 | 命令 + 结果 | pending |

## 路线 A：端到端添加一个工具

添加 `repo.symbols`（列出文件中的函数/类定义）或 `repo.stat`（获取大小、行数和 digest，但不
读取完整内容）。不要只改一个函数，要走过你学到的每一层：

1. **契约。** 在 `contracts/tools.py` 写严格参数 model，注册到 `ARGS_MODELS`，补上描述；
2. **Policy。** 完成分类。它是只读工具，应加入 `READ_ONLY_TOOLS`。在分类前，完备性测试应该失败；
3. **Facts 与执行。** 在 `application/tool_pipeline.py` 和 `adapters/workspace_fs.py` 中采集工作区
   facts、执行工具并返回结构化 `ToolResult`；
4. **测试。** 为 workspace 方法写单元测试；用 `ScriptedModel` 写集成流程；对 `repo.symbols`
   加安全测试，确认工作区外路径被拒绝；
5. **证明不变量。** 确认坏路径返回 `not_found`/`denied`，而不是异常（模块 03）。

完成标准：`pytest`、`mypy`、`ruff`、`lint-imports` 和 `haven eval --offline` 全部通过，
并且 `haven debug-context "..."` 能在工具目录中显示新工具。

## 路线 B：实现一个新的 provider adapter

为 Haven 接入从未支持过的 provider，例如 Anthropic Messages、Google 或本地 Ollama server，任选其一。

1. 放入 `adapters/providers/`；核心不得修改；
2. 把线格式映射到中立的 `ModelEvent` union，包括 provider 报告的用量、cache 和 reasoning 字段；
3. 把怪癖留在 adapter：Anthropic 的流式方式不同且使用 `cache_control`，本地模型可能完全不报告用量；
4. 用 `httpx.MockTransport` 做离线契约测试：文本组装、工具调用组装、错误映射，以及 API key 绝不出现在
   错误中。

完成标准：契约测试离线通过；可选地运行 `haven verify-provider --yes` 对真实 endpoint 验证。另写一篇
短笔记，记录你吸收了哪些 provider 怪癖；那篇笔记本身就是你理解边界的证据。

## 路线 C：持久化恢复演练

验证恢复语义：

1. 复现 `tests/recovery/` 的三种中断结果：可证明未执行、可证明已确认、不明确；
2. 自己加入一个崩溃点，例如 `repo.check` 已开始、结果尚未写入日志时进程退出，并决定正确分类。在测试
   docstring 中解释：进程副作用是否可能被证明为“未运行”？
3. 确认你新增的任何场景都不会自动重放不明确副作用。如果找到一个会重放的场景，那就是实际 bug：
   写成模块 10 所要求的 postmortem 条目。

完成标准：新恢复测试通过，并能对你的崩溃点明确证明“绝不自动重放不明确副作用”。

## 路线 D：经过收益门禁的功能（困难路线）

提出一个项目明确推迟的能力，例如上下文摘要、只读 sandbox shell 或 LSP 导航，并让它经历完整纪律：

1. 写一页收益门禁：问题、带真实测量的基线、选项、决定、指标和回滚；
2. **只有门禁通过后**，才实现能推动该指标的最小版本；
3. 测量前后差异：确定的部分离线测，非确定的部分在线测并明确标注；
4. 写 ADR，包括你放弃了什么。

完成标准不是发布功能，而是用测量支持构建或放弃的决定。

## 所有路线的共同标准

先运行仓库统一的快速门禁：

```bash
uv run python scripts/gates.py --mode fast
```

再运行完整的行为验证：

```bash
uv run pytest -q
uv run haven eval --offline        # 安全违规必须为 0
```

快速门禁覆盖格式、lint、类型、分层和文档契约。最后在 `CAPSTONE.md` 记录：构建了什么、做了哪个决定、
哪个数字可复现，以及下次会改变什么。

## 之后去哪里

- 重读 `docs/PROJECT_CARD.md`，用自己的话重写简历 bullet，并用自己复现的数字支撑；
- 从头读完原始计划 `Haven_TUI_Coding_Agent_项目计划.md`，现在你已经有足够上下文理解每个 non-goal；
- 把一个模块教给别人，这是发现“以为理解了”的部分最快的方法。
