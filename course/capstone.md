# 结业项目——扩展 Haven，或构建自己的版本

[English](archive/en/capstone.md) | **中文**

你已经读过这套机制。现在不要再只复述它，选一条路线证明自己能使用它。每条路线都适合一个专注的
周末，验收标准相同：测试通过、记录一个有名字的决定、每项声明都可以复现。

开始前：

```bash
uv sync --locked
uv run pytest -q && uv run haven eval --offline
git switch -c capstone/<your-track>
```

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

让恢复真正值得信任：

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

完成标准不是“功能一定发布”。无论最后构建还是放弃，你都要用一个数字为决定辩护。
“我测量过，它不值得”同样是通过——这才是本项目要训练的技能。

## 所有路线的共同标准

```bash
uv run ruff format --check . && uv run ruff check .
uv run mypy src
uv run lint-imports
uv run pytest -q
uv run haven eval --offline        # 安全违规必须为 0
```

然后写一份简短的 `CAPSTONE.md`：构建了什么、记录了哪个决定、哪个数字可复现，以及你会做的一件
不同的事。面试时真正要讲清楚的是这份判断记录，而不是 diff 的行数。

## 之后去哪里

- 重读 `docs/PROJECT_CARD.md`，用自己的话重写简历 bullet，并用自己复现的数字支撑；
- 从头读完原始计划 `Haven_TUI_Coding_Agent_项目计划.md`，现在你已经有足够上下文理解每个 non-goal；
- 把一个模块教给别人，这是发现“以为理解了”的部分最快的方法。
