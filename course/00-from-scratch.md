# 模块 00——从零构建：一条代理如何变得可靠

[上一课：第一次离线运行](prerequisite-03-first-offline-journey.md) |
[源码伴读索引](code-reading-map.md) |
[English](archive/en/00-from-scratch.md) | **中文**

本课从最小代理出发，连续揭开十一个朴素假设，再把每种失败映射到 Haven 的修复机制。示例代码只用于
说明机制，不是生产实现；全程不需要 API key。

## 本课路线

- **前置：** 能读基础 Python；零 Agent 经验时先完成预备课 A–C。
- **建议用时：** 第一遍 60–90 分钟，第二遍随模块 01–10 回看。
- **本课产出：** 不背组件名，而是能从十一项朴素假设推导出对应的失败模式和修复机制。
- **最小验证：**

```bash
uv run haven doctor
uv run pytest -q tests/integration/test_agent_journeys.py::TestEditJourney::test_full_fix_with_evidence
```

先运行测试，写下“编辑成功”应包含的阶段；读完再用 Trace 核对。第一遍只抓因果链：**哪种失败，催生了
哪道边界。**

## 如何读这一课

每个阶段都按四个问题展开：

1. **最初会怎么写？** 一个合理但天真的版本。
2. **它具体会在哪里坏？** 不是抽象地说“不安全”，而是指出失败的输入、状态或时序。
3. **修复是什么，代价是什么？** 每个安全机制都会增加约束、代码或交互成本。
4. **今天的实现在哪里？** 跟着文件路径回到仓库，查看成熟版本。

真实构建并非按此顺序发生；本文按因果关系重排，方便学习。

---

## 阶段 0：二十行代理

先看一个在顺利时确实能工作的代理：

```python
messages = [{"role": "user", "content": goal}]
while True:
    reply = model.chat(messages, tools=TOOLS)
    if not reply.tool_calls:
        return reply.text  # done!
    for call in reply.tool_calls:
        result = TOOL_FNS[call.name](**call.args)  # just do it
        messages.append({"role": "tool", "content": result})
```

这段代码暗含很多承诺：工具和参数正确、动作被允许、执行时现实没有变化、写入成功、命令安全、循环
会停止、上下文装得下、进程不中断，而且最终状态真的满足目标。

十一项假设，正好对应后面的十一个阶段。

---

## 阶段 1：模型的 JSON 是提案，不是命令

### 最初会怎么写

```python
TOOL_FNS[call.name](**call.args)
```

### 它会在哪里坏

工具名拼错会抛出 `KeyError`；畸形参数可能到写入层才变成 traceback。模型也可以提出：

```text
../../.ssh/authorized_keys
```

程序若照做就会越界写入：生成字符串不等于获得权限。

### 修复，以及它带来的成本

先由程序把提案变成可审查的数据：

```python
model_cls = ARGS_MODELS.get(call.name)
if model_cls is None:
    return error("unknown_tool")

try:
    args = model_cls.model_validate_json(call.arguments_json)
except ValidationError as exc:
    return error("invalid_arguments", summarize(exc))

facts = workspace.path_facts(args.path)
if not facts.within_workspace:
    return error("denied")
```

三条原则贯穿全系统：

- **失败是结果，不是异常。** `invalid_arguments` 可以反馈给模型，让它修正下一次提案；异常直接
  冲出代理循环，运行就失去了恢复机会。
- **事实由程序采集。** 模型可以提出一个 path，但不能同时声称“这个 path 在工作区内”。路径的
  规范化、摘要和越界判断必须来自真实文件系统。
- **工具集合是程序的一部分。** 运行时不能凭空增加一个未经分类的工具；这也是后来谨慎对待 MCP
  的原因。

代价是维护 schema，并让每个新工具接入 registry、policy、facts 和 executor。阶段 11 会用测试防止遗漏。

实现可从 `src/haven/contracts/tools.py`、`src/haven/application/registry.py` 和
`src/haven/adapters/workspace_fs.py::path_facts` 开始阅读。

---

## 阶段 2：总得有人决定，而且不能是模型

### 最初会怎么写

参数验证通过后，直接执行。

### 它会在哪里坏

系统若没有集中回答“代理允许做什么”，权限就会散落在工具分支里，难以审查和穷尽测试。

### 修复，以及它带来的成本

把决策抽成一个无 I/O 的纯函数：

```python
def evaluate_policy(mode: PermissionMode, facts: ToolFacts) -> PolicyOutcome:
    """(mode, program-collected facts) -> allow | ask | deny. No I/O."""
```

纯函数可以被完整测试，也让权限模型集中可见。Haven 默认拒绝未知工具，不自动放行副作用工具，且只
接受程序采集的 facts。

窄例外是操作数仍在工作区内的明显只读命令；阶段 5 会说明为什么这个例外必须连同操作数判断。

代价是交互摩擦：写入操作需要审批。实现位于 `src/haven/domain/policy.py`、
`src/haven/domain/exec_policy.py` 和 `tests/unit/test_policy.py`。

---

## 阶段 3：一句“允许编辑吗？”远远不够

### 最初会怎么写

```python
if policy_says_ask:
    if input(f"allow {tool} on {path}? [y/N] ") != "y":
        return error("approval_rejected")
    do_it()
```

### 它会在哪里坏

若审批只绑定“编辑文件”这一类别，确认后仍可替换 path 或内容，也可重复消费。批准与执行之间文件还
可能变化，这就是 TOCTOU（检查时与使用时之间的竞态）。

### 修复，以及它带来的成本

让审批摘要（digest）本身代表一次完整动作：

```python
digest = compute_approval_digest(
    workspace_digest=...,
    tool_name=...,
    tool_version=...,
    canonical_args_json=...,
    preimage_digest=...,
    preview_digest=...,
)
```

界面展示 preview，并把 preview 的 digest 写入审批记录；数据库用条件 `UPDATE` 消费审批，使一条
审批最多成功一次。用户批准后，管线还要在执行前重新读取写入前的文件摘要：

```python
if workspace.path_facts(path).digest != approved_preimage:
    return error("stale_preimage")
```

文件漂移时审批失效。ADR 0025 只允许同一运行内、字节完全相同的重复 `repo.check` 复用 standing
approval；写入始终重新询问。

实现位于 `src/haven/domain/approval.py`、`src/haven/application/tool_pipeline.py::_ask_approval`，
以及 `docs/adr/0025-standing-approval-for-identical-checks.md`。

---

## 阶段 4：写文件不是一次 `write()` 调用

### 最初会怎么写

```python
path.write_text(new_content)
```

### 它会在哪里坏

进程中途退出会留下半个文件；函数返回也不证明目标字节正确落盘。若未保存旧内容，还无法计算本次 diff
或回退。

### 修复，以及它带来的成本

在同一文件系统里写临时文件、flush、fsync，然后原子替换；替换后重新读取并校验摘要：

```python
fd, tmp = tempfile.mkstemp(dir=target.parent)
with os.fdopen(fd, "w") as handle:
    handle.write(new_text)
    handle.flush()
    os.fsync(handle.fileno())
os.replace(tmp, target)
postimage = sha256_bytes(target.read_bytes())
if postimage != expected:
    raise WorkspaceError("internal", "postimage mismatch")
```

运行第一次接触某个文件时，还要保存原始内容，才能计算本次运行的 diff，并支持 `haven rewind`。

> write 返回成功不是证据；写入后的 postimage digest 才是证据。

实现位于 `src/haven/adapters/workspace_editor.py::_atomic_write`、`src/haven/adapters/workspace_fs.py::register_run_original` 和多文件
`apply_patch`。

---

## 阶段 5：命令执行才是代理真正危险的地方

### 最初会怎么写

```python
subprocess.run(command_string, shell=True)
```

### 它会在哪里坏

shell 字符串可以包含危险元字符或读取凭据。固定 argv 仍不够：`pytest` 会加载代理刚修改的
`conftest.py`；没有隔离时，爆炸半径仍是整台机器。

一次安全复查发现，仅检查 cwd 会放过工作区外的只读操作；stdout 随后进入 transcript，可能泄漏
`/etc/passwd` 或 `/proc/<parent-pid>/environ` 中的凭据。

### 修复，以及它带来的成本

修复有三层：

1. 模型只能选择用户在 `.haven.toml` 中登记的 recipe id，例如 `verify`，不能选择任意命令字符串；
2. 所有进程都在唯一包装点经过 macOS Seatbelt 或 Linux Landlock；`repo.exec` 使用工作区只读、禁网
   profile，登记的检查使用工作区可写 profile；
3. 没有 sandbox backend 时，模型提议的通用 `repo.exec` 直接拒绝，不能退化成无隔离执行。

ADR 0026 要求连同操作数判断只读命令：工作区内可自动放行，出现绝对路径、`~` 或 `..` 时询问。

实现位于 `src/haven/adapters/process_executor.py`、`src/haven/sandbox/`、
`src/haven/domain/exec_policy.py`，以及 ADR 0009、0013、0017、0026。

---

## 阶段 6：“完成了！”不能证明完成

### 最初会怎么写

模型不再调用工具，就把最终回答当作成功。

### 它会在哪里坏

模型可能没有修改，或已经破坏构建，却仍说“修好了”。成功不能由模型自评。

### 修复：Evidence Gate

修改过文件的运行至少需要三类证据：

1. 真实 diff；
2. 在最后一次写入之后记录的通过检查；
3. 对新增行进行确定性审查：没有 secret、冲突标记、调试器残留，也没有把大文件清空。

模型会优化可见 oracle：它曾编辑测试或写入 `sitecustomize.py` 让检查变绿。因此还需要范围防护、提示
护栏和隐藏 grader。

如果没有配置检查，诚实的结果应是“文件已修改，但无法验证”，然后以 `verification_unavailable`
停止；不能让模型在一个永远无法通过的门禁前烧完整个预算。

实现位于 `src/haven/domain/evidence.py`、`src/haven/domain/review.py`、`src/haven/evalkit/runner.py`
和 ADR 0003。

---

## 阶段 7：`while True` 是一份没有写下来的预算

### 最初会怎么写

模型不再提出工具调用时结束。

### 它会在哪里坏

模型可能重复失败动作或寻找不存在的 bug；自然停止也无法区分成功、放弃、预算耗尽和系统拒绝。

### 修复，以及它带来的成本

为步数、工具调用、墙上时间、token 和成本设置代理无法提高的硬预算。每次运行必须有且只有一个
`StopReason`，例如 `evidence_satisfied`、`no_progress`、`step_budget_exhausted` 或 `effect_unknown`。
同一个（工具、参数、结果）连续出现三次，就判定为卡死。

卡死检测应忽略 `duration_ms` 等无关字段，否则相同动作会因计时抖动被误判为不同结果。

实现位于 `src/haven/domain/budget.py`、`src/haven/domain/stuck.py`、`src/haven/application/run_service.py`，
以及 ADR 0006。

---

## 阶段 8：上下文要选择，不要无限积累

### 最初会怎么写

每轮都执行 `messages.append(...)`。

### 它会在哪里坏

上下文会溢出；变化的 prompt 前缀也无法复用缓存。把预算计数器移到尾部后，同一套件、同一模型的缓存
命中率从 70.9% 提升到 89.3%。

### 修复，以及它带来的成本

上下文布局为：稳定头部（系统规则、项目指导、目标）→ transcript → 易变尾部（计划和计数器）。
超出预算时，完整删除最老的工具单元，换成程序生成的路径、digest、退出码等摘要；调用和结果必须成对
删除，不能留下半个动作。

摘要由程序生成，避免模型编造权限事实。结构化 digest 不含文件内容或模型 prose，可标为 `trusted`；
是否引入 LLM 摘要应通过 ADR 0024 的收益门禁测量。

实现位于 `src/haven/application/context_builder.py`、`src/haven/application/compaction.py`，
以及 ADR 0008、0010、0024。

---

## 阶段 9：进程可能在写入中途死亡

### 最初会怎么写

重启时重放 transcript，然后继续循环。

### 它会在哪里坏

最后一个 `repo.edit` 可能尚未开始，可能已经完成，也可能只完成了一半。重放会把一个已经完成的编辑
再次应用，造成重复副作用。

### 修复，以及它带来的成本

写入前记录期望的前后摘要：

```python
record_execution(
    call_id,
    state=STARTED,
    preimage_digest=...,
    postimage_digest=expected,
)
```

写入后标记 `CONFIRMED`。恢复时比较磁盘：

| 当前内容 | 结论 |
|---|---|
| 等于 preimage | **尚未执行**，可以安全恢复 |
| 等于 postimage | **已经确认**，不要重复执行 |
| 两者都不是 | **副作用不明确**，停止并等人处理 |

`EFFECT_UNKNOWN` 必须由人类运行 `haven reconcile`。例如 `repo.move` 可能已写入目标但尚未删除源文件；
继续或跳过都可能出错，因此系统承认 unknown，不自动重放。

检查点回答“从哪里继续”，日志记录“发生了什么”。检查点是可替换缓存，不是权威历史。

实现位于 `src/haven/adapters/sqlite_session.py`、`src/haven/application/recovery_service.py`、
`src/haven/contracts/checkpoint.py`，以及 ADR 0004。

---

## 阶段 10：不能用一个代理测试另一个代理

### 最初会怎么写

调用真实模型，然后断言输出。

### 它会在哪里坏

真实模型测试慢、不确定、付费，还受 provider 和网络影响，不适合作为机制回归门禁。

### 修复：把两个问题拆开

机制是否可靠，用 `ScriptedModel` 替换模型，但保留真实文件系统、子进程、policy、approval 和 journal，
运行整套离线评估。每个用例都检查允许集合之外不能改文件，禁止内容不能进入 transcript；安全违规直接
让构建失败。

真实工作能力只能由在线评估回答：固定第三方仓库 commit，注入一个小 bug，确认带 bug 时检查失败、
回滚后检查通过，再花钱运行真实模型。离线测试证明的是机制，在线测试才触及模型行为和 provider 线格式。

历史评测从 27/31 到 31/31，主要修复的是重试分类和模型编辑测试等机制问题。中立 grader 的同任务比较
中，Haven 为 12/12、opencode 为 10/12；后者两个失败都通过编辑测试让套件变绿。数字必须连同失败形状
和测量范围一起解释。

实现位于 `src/haven/evalkit/runner.py`、`evals/`、`docs/EVAL.md`、`docs/EVAL_LIVE.md`，以及 ADR 0005。

---

## 阶段 11：知道什么不该构建

### 最初会怎么写

比较表上有你没有的功能，就把它们全部加进来。

### 它会在哪里坏

每个新能力都会穿过已有边界，增加新的失败路径。

### 修复：在数据出现前写收益门禁

先写清它要修复哪类故障、什么测量能证明故障真实存在，再去测量。Haven 的结论包括：

| 推迟的能力 | 门禁或观察 | 结论 |
|---|---|---|
| 只读 LSP | 至少 5 次语义定位失败 | 实际约 1 次，暂不构建 |
| Planner / goal FSM | 规划失败占主导 | 实际主导是收敛问题，暂不构建 |
| Subagents | 长程超时能由委派修复 | 没有观察到这种形状，暂不构建 |
| MCP | 必须运行时发现工具的失败 | 没有，而且会破坏编译内置的不变量 |

实际主导失败是模型未及时停止；新增架构并不能替它收敛。ADR 0007、0016 先写门禁，ADR 0023 再记录
数据结论。

真正添加工具时，让遗漏变得难以隐藏：参数 model、policy 分类、facts handler、execute handler 和
测试都要接入；不完整的添加应在构建时失败。

实现位于 `docs/adr/0007`、`0016`、`0023`、`0024`、`0026` 和 `tests/unit/test_policy.py`。

---

## 现在你应该能解释什么

| 机制 | 移除它之后会怎样 |
|---|---|
| Registry + 严格 schema | 拼写错误会结束运行，畸形参数会变成 traceback |
| 程序采集的 facts | 模型可以给自己授权 |
| 纯 policy | 权限散落在各处，无法完整测试 |
| digest 绑定的一次性审批 | 人类只批准了类别，动作可以漂移或重放 |
| TOCTOU 复查 | 程序可能针对已经变化的内容写入 |
| 原子写入 + postimage | 崩溃会截断文件，成功只能靠猜 |
| recipe + sandbox | “运行测试”的爆炸半径等于整台机器 |
| Evidence Gate | 成功会退化成模型意见 |
| 范围防护 + 隐藏 grader | 模型可以修改测试，而不是修复代码 |
| 预算 + 一个停止原因 | 循环可能运行到账单叫停 |
| 选择式上下文 + 稳定前缀 | 窗口溢出，前缀重复计费 |
| 程序 digest 而非模型摘要 | 摘要可能编造权限事实 |
| 日志 + digest 分类 | 恢复时可能重复应用写入 |
| 离线评估门禁 | 上述机制没有回归保护 |
| 收益门禁 | 不变量会被“令人印象深刻”的功能逐个侵蚀 |

合上正文，从左栏的失败重新推导右栏机制。

## 接下来去哪里

- **逐层深入**：阅读模块 [01](01-mental-models.md) 到 [10](10-engineering-judgment.md)。
- **动手构建**：完成[结业项目](capstone.md)。
- **查看决策如何经受质疑**：阅读 [`docs/DESIGN_QA.md`](../docs/DESIGN_QA.md)。
- **了解真实故障**：阅读 [`docs/POSTMORTEM.md`](../docs/POSTMORTEM.md) 和
  [`docs/EVAL_LIVE.md`](../docs/EVAL_LIVE.md) 的故障章节。
