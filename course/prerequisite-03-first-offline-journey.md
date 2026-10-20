# 预备课 C——第一次离线运行与 Trace 导览

[上一课：Python 阅读工具箱](prerequisite-02-python-reading-kit.md) | [返回课程总览](README.md) |
[源码伴读索引](code-reading-map.md) |
[下一课：从零构建可靠 Agent](00-from-scratch.md)

这一课用 `ScriptedModel` 跑一条完整修复旅程。模型轨迹预先写好；工作区、工具管线、policy、审批、
文件编辑、检查、证据和事件日志都使用真实实现。全程不需要 API key。

## 本课路线

- **前置：** 预备课 A；能从 pytest 中找到输入、动作和断言。
- **建议用时：** 60–90 分钟。
- **本课产出：** 一张六轮运行表，以及一条从 `tool.proposed` 到 `tool.completed` 的事件链。
- **完成标准：** 能打开 golden trace，指出哪部分来自模型、哪部分来自程序、最终成功由谁判定。

## 1. 环境自检

在仓库根目录运行：

```bash
uv sync --locked
uv run haven doctor
```

`doctor` 应确认 Python、Git、ripgrep、工作区和配置可用。API key 显示 missing 不影响离线课程。

接着预览第一轮 Context：

```bash
uv run haven debug-context "Fix the bug in add()"
```

观察四件事（具体字节数可能变化）：

- system rules 与用户目标来自哪里；
- 仓库中的 `AGENTS.md` 为什么标成 untrusted；
- 本轮暴露了哪些工具；
- 哪些内容没有进入 Context，例如未读取的仓库文件、其他 session 和 secret。

这条命令只展示 `ContextBuilder` 生成的首轮请求，不调用模型。

## 2. 运行第一条完整旅程

```bash
uv run pytest -q \
  tests/golden/test_golden_trace.py::TestGoldenTrace::test_edit_journey_matches_golden
```

测试在 `tmp_path` 临时仓库中运行，不会修改项目源码。打开三个文件：

1. `tests/golden/test_golden_trace.py`：脚本模型每轮准备返回什么；
2. `tests/integration/harness.py`：真实组件怎样组装；
3. `tests/golden/data/edit_journey.json`：程序实际记录的稳定事件序列。

先对照这三个入口，不要通读 `RunService`。

## 3. ScriptedModel 不是“假装任务成功”

测试提前写下模型的六轮输出：

```text
1. repo.search
2. repo.read
3. repo.edit
4. repo.diff
5. repo.check
6. final text
```

脚本只替换“模型下一轮生成什么”，其他机制仍然真实运行：

```text
临时工作区中的真实文件
        ↓
真实 schema / facts / policy / approval
        ↓
真实 edit / diff / 子进程 check
        ↓
真实 Evidence Gate / Trace / StopReason
```

它能验证运行机制，不能证明真实模型面对陌生仓库会选择同样路径。模块 09 会区分机制与能力评测。

## 4. 六轮分别发生了什么

| 轮次 | 模型提出或返回 | 程序的关键决定 | 新产生的事实 |
|---:|---|---|---|
| 1 | 搜索 `BUG` | 只读、工作区内，allow | 找到 `src/calc.py` 第 2 行 |
| 2 | 读取文件 | 只读、工作区内，allow | 得到完整 preimage |
| 3 | 把减法改成加法 | 写操作 ask；用户批准后复查并执行 | edit evidence + postimage digest |
| 4 | 查看 diff | 只读，allow | 真实 diff：1 个文件，+1/-1 |
| 5 | 运行 `verify-calc` | 已登记 recipe，但有副作用，ask | check exit code 0 |
| 6 | 输出最终说明 | Evidence Gate 检查最后写入后的 diff/check | `evidence_satisfied` |

模型选择候选路径；程序决定授权、执行和成功。

### 为什么先 read 再 edit

编辑必须绑定 preimage，才能确认修改基于哪个版本，并检测批准后的文件漂移。

### 为什么 edit 成功后还要 diff

工具 success 只说明那次写入未报错；diff 才展示本次运行的净变化。后续撤销会使最终 diff 为空。

### 为什么 check 必须在 edit 之后

编辑前的绿测试只证明旧版本；Evidence Gate 通过 Trace 序列号只接受最后写入后的检查。

## 5. 学会按“事件簇”读 Trace

不要把 JSON 当成几十个孤立对象。按下面的事件簇阅读：

```text
run.created
  └─ step.started
      ├─ context.built
      ├─ model.completed
      └─ tool.proposed
          ├─ policy.decided
          ├─ approval.requested / approval.decided   # 仅 ask
          ├─ execution.started
          ├─ evidence.recorded / diff.preview        # 视工具而定
          └─ tool.completed
  └─ ...下一轮...
run.finished
```

| 事件 | 它证明什么 | 它不证明什么 |
|---|---|---|
| `model.completed` | 某轮模型流完整组装结束 | 模型说的内容正确 |
| `tool.proposed` | 模型提出了某个动作 | 动作被允许或已执行 |
| `policy.decided` | 程序做出 allow/ask/deny | ask 已获得用户批准 |
| `approval.decided` | 用户对精确动作作出决定 | 执行一定成功 |
| `execution.started` | 执行器开始尝试 | 副作用已经确认完成 |
| `tool.completed` | 工具产生结构化结果 | 整个用户任务成功 |
| `evidence.recorded` | 某项证据进入账本 | 所有成功条件均满足 |
| `run.finished` | 运行以命名原因结束 | `status` 必然是 succeeded |

按“证明什么 / 不证明什么”读 Trace，不要把相邻事件视为同一保证。

## 6. 从旅程映射到架构

现在再打开实现，按一次调用向下追：

```text
test / CLI / TUI
      ↓
RunService._drive                 application：控制有限循环
      ↓
ContextBuilder + ModelPort        application/port：组请求、调用模型
      ↓
ToolPipeline.execute              application：唯一工具通道
      ↓
registry → facts → policy         contract/domain：校验与决定
      ↓
approval → ticket → adapter       application/domain/adapter：授权与执行
      ↓
ToolResult + Evidence + Event     contract/domain：结果、证据、日志
      ↓
下一轮，或 Evidence Gate → finish
```

第一次只追 `repo.edit`。helper 只有在改变输入、权限、执行或证据语义时才展开。

## 7. 做三个对照实验

不用改源码，运行已有测试就能一次只改变一个变量。

### 对照一：拒绝审批

```bash
uv run pytest -q \
  tests/integration/test_agent_journeys.py::TestEditJourney::test_rejection_leaves_file_untouched
```

观察点：模型仍然提出 edit，但 `approval_rejected` 成为工具结果，文件保持原样。提案与权限被分开。

### 对照二：有写入但缺证据

```bash
uv run pytest -q \
  tests/integration/test_agent_journeys.py::TestEditJourney::test_evidence_missing_stops_run
```

观察点：文件可能已经改变，但模型连续说 done 仍不能让运行成功，最终 StopReason 是
`evidence_missing`。

### 对照三：最后检查失败

```bash
uv run pytest -q \
  tests/integration/test_agent_journeys.py::TestEditJourney::test_failing_check_blocks_success
```

观察点：有 edit、有 diff、有 check 还不够；最新 check 的 exit code 必须通过。

把三个失败实验与成功旅程并排比较，分别定位 approval、完整证据和绿检查的作用。

## 8. 你的第一张运行分析表

为 golden trace 填完下面的表。每行只写一条最直接的依据：

| 问题 | 你的答案 | Trace / 测试依据 |
|---|---|---|
| 模型一共完成几轮？ |  |  |
| 哪些工具自动 allow？ |  |  |
| 哪些动作要求 ask？ |  |  |
| 哪个事件第一次证明磁盘已改变？ |  |  |
| 哪些证据发生在最后写入之后？ |  |  |
| 最终 StopReason 是什么？ |  |  |
| 如果删掉 check，为什么不能成功？ |  |  |

只引用结构化字段或测试断言，不把最终文本当证据。

## 9. 与正式课程的连接

三门预备课和正式模块的关系是：

```text
预备课 A：认识消息、工具调用和有限循环
预备课 B：看懂代码与测试使用的构件
预备课 C：跑通一条完整 Trace
      ↓
模块 00：从朴素实现逐项推导生产机制
      ↓
模块 01–10：按层深入源码、测试和工程判断
```

此时不必理解所有 digest、SQLite 表或 sandbox profile。能沿 golden trace 解释“提案 → 决定 → 执行 →
观察 → 证据判定”，即可进入模块 00。

## 下一步

进入[模块 00](00-from-scratch.md)，从一个危险的二十行循环反推每道边界为何存在。
