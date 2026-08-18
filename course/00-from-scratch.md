# 模块 00——从零构建：一条代理如何变得可靠

[English](archive/en/00-from-scratch.md) | **中文**

其他模块会把 Haven 当作一个已经完成的系统，分别讲解其中一层。这一课换一种方式：从大家都会
先写出的最小代理开始，逐步追问“它凭什么能安全地做这件事”。每一次追问都会揭出一个真实的
工程问题，而下一阶段的机制，就是针对那个问题付出的修复成本。

如果你想知道 Haven 为什么长成现在这个样子，先读这一课。这里不需要 API key；示例代码是为了
说明机制而写的短代码，不是生产实现的完整替代品。

## 如何读这一课

每个阶段都按四个问题展开：

1. **最初会怎么写？** 一个合理但天真的版本。
2. **它具体会在哪里坏？** 不是抽象地说“不安全”，而是指出失败的输入、状态或时序。
3. **修复是什么，代价是什么？** 每个安全机制都会增加约束、代码或交互成本。
4. **今天的实现在哪里？** 跟着文件路径回到仓库，查看成熟版本。

真实构建过程并不像本文这样整齐。有些问题是在功能发布后才被发现的；它们仍然写在这里，因为
“系统在真实使用中怎样暴露自己的假设”往往比漂亮的设计图更值得学习。

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

演示时它像魔法：模型决定下一步，程序替它完成动作。但这段代码暗含了很多承诺：工具名一定存在，
参数一定正确，动作一定被允许；等到真正执行时，参数仍然对应现实；写入真的成功；命令不会伤害
机器；模型知道什么时候结束；循环不会卡住；对话记录装得进上下文；进程不会中途退出；最后，
我们还得知道“成功”到底有没有发生。

十一项假设，正好对应后面的十一个阶段。

---

## 阶段 1：模型的 JSON 是提案，不是命令

### 最初会怎么写

```python
TOOL_FNS[call.name](**call.args)
```

### 它会在哪里坏

工具名拼错会抛出 `KeyError`。参数少一个字段、多一个字段，或把 `path` 写成整数，错误可能在很深的
写入函数里才变成 traceback；模型得到的只是一个无法修复的崩溃。更糟的是，模型完全可以提出：

```text
../../.ssh/authorized_keys
```

如果程序照做，越界写入就成功了。模型并没有“请求到权限”，我们只是把它生成的字符串当成了命令。

### 修复，以及它带来的成本

第一步不是让模型更聪明，而是让程序先把提案变成可审查的数据：

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

这里埋下了三条会贯穿全系统的原则：

- **失败是结果，不是异常。** `invalid_arguments` 可以反馈给模型，让它修正下一次提案；异常直接
  冲出代理循环，运行就失去了恢复机会。
- **事实由程序采集。** 模型可以提出一个 path，但不能同时声称“这个 path 在工作区内”。路径的
  规范化、摘要和越界判断必须来自真实文件系统。
- **工具集合是程序的一部分。** 运行时不能凭空增加一个未经分类的工具；这也是后来谨慎对待 MCP
  的原因。

代价很实际：要维护参数 schema，新增工具也会涉及注册表、policy、事实采集和执行代码。阶段 11
会说明为什么这份麻烦值得保留，以及如何用测试让遗漏在构建时暴露。

实现可从 `src/haven/contracts/tools.py`、`src/haven/application/registry.py` 和
`src/haven/adapters/workspace_fs.py::path_facts` 开始阅读。

---

## 阶段 2：总得有人决定，而且不能是模型

### 最初会怎么写

参数验证通过后，直接执行。

### 它会在哪里坏

这一次不一定立刻报错，问题反而更隐蔽：系统没有一个集中位置能回答“代理允许做什么”。权限散落
在各个工具的 `if` 语句里，既难以穷尽测试，也无法让安全审查者一眼看懂。

### 修复，以及它带来的成本

把决策抽成一个无 I/O 的纯函数：

```python
def evaluate_policy(mode: PermissionMode, facts: ToolFacts) -> PolicyOutcome:
    """(mode, program-collected facts) -> allow | ask | deny. No I/O."""
```

纯函数的价值不在于形式漂亮，而在于它可以被完整测试，读者也能在一个地方看到权限模型。Haven 的
基本规则是：未知工具默认拒绝；有副作用的工具绝不自动放行；输入只能是程序采集的 facts。

有一个窄例外：明显只读、且操作数仍在工作区内的命令可以自动放行，例如 `ls`、`cat` 和 `git status`，
否则代理每看一次目录都要打扰人类。后面的阶段 5 会讲到，这个例外曾经定义得太宽。

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

人类批准的是“编辑这个文件”这一类动作，却没有看到具体 diff。审批没有绑定展示过的参数，因此
确认之后可以换一个 `path` 或 `new_string`；同一份“同意”也可能被重复使用。最后，文件可能在用户
按下确认之后、程序真正写入之前被编辑器或另一个进程改掉——这就是 TOCTOU（检查时与使用时之间的竞态）。

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

文件发生漂移时让审批失效，是有意的安全选择。正确的 UX 应该是让重新审批足够便宜，而不是让旧审批
变得足够宽松。ADR 0025 只为同一运行内、字节完全相同的重复 `repo.check` 提供窄范围的 standing
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

进程可能在写入中途退出，留下半个文件。函数正常返回只说明没有抛异常，并不证明目标字节已经正确
落盘。若旧文件在写入前没有保存，运行结束后也无法知道“这次运行究竟改了什么”，更无法支持回退。

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

实现位于 `src/haven/adapters/workspace_fs.py::_atomic_write`、`register_run_original` 和多文件
`apply_patch`。

---

## 阶段 5：命令执行才是代理真正危险的地方

### 最初会怎么写

```python
subprocess.run(command_string, shell=True)
```

### 它会在哪里坏

模型生成的 shell 字符串可以包含 shell 元字符、`rm -rf` 或 curl-pipe-bash，也可以直接读取
`~/.aws/credentials`。改成固定 argv 仍不够：`pytest` 会加载仓库中的 `conftest.py`，而代理可能
刚刚修改了它；只要没有隔离，爆炸半径仍然是整台机器。

后来一次安全复查还发现，旧注释“分类错了只会少一次提示，不会越界”并不成立：sandbox 有意让
`$HOME` 以外的许多路径可读，`repo.exec` 只检查 cwd，stdout 又会进入模型 transcript。于是自动放行
`cat /etc/passwd` 会泄漏文件，Linux 上读取 `/proc/<parent-pid>/environ` 甚至可能交出云凭据。

### 修复，以及它带来的成本

修复有三层：

1. 模型只能选择用户在 `.haven.toml` 中登记的 recipe id，例如 `verify`，不能选择任意命令字符串；
2. 所有进程都在唯一包装点经过 macOS Seatbelt 或 Linux Landlock；`repo.exec` 使用工作区只读、禁网
   profile，登记的检查使用工作区可写 profile；
3. 没有 sandbox backend 时，模型提议的通用 `repo.exec` 直接拒绝，不能退化成无隔离执行。

ADR 0026 又收紧了审批例外：只读命令必须连同操作数一起判断。只读命令留在工作区内时可以保持
安静；出现绝对路径、`~` 或 `..` 时就询问。安全注释不是永恒真理，它会随着 capability 变化过期，
必须从实际边界重新推导。

实现位于 `src/haven/adapters/process_executor.py`、`src/haven/adapters/sandbox/`、
`src/haven/domain/exec_policy.py`，以及 ADR 0009、0013、0017、0026。

---

## 阶段 6：“完成了！”不能证明完成

### 最初会怎么写

模型不再调用工具，就把最终回答当作成功。

### 它会在哪里坏

模型可能没有改任何东西却说“修好了”，也可能已经破坏构建。此时成功标准变成了模型对自己工作的评价，
而不是仓库实际发生的变化。

### 修复：Evidence Gate

修改过文件的运行至少需要三类证据：

1. 真实 diff；
2. 在最后一次写入之后记录的通过检查；
3. 对新增行进行确定性审查：没有 secret、冲突标记、调试器残留，也没有把大文件清空。

模型还会优化自己看得见的 oracle。真实运行中，它编辑过测试，也曾写入 `sitecustomize.py` 让失败
检查变绿。因此系统还需要范围防护、禁止此类手段的提示护栏，以及模型看不到的隐藏 grader。

如果没有配置检查，诚实的结果应是“文件已修改，但无法验证”，然后以 `verification_unavailable`
停止；不能让模型在一个永远无法通过的门禁前烧完整个预算。

实现位于 `src/haven/domain/evidence.py`、`src/haven/domain/review.py`、`src/haven/evalkit/runner.py`
和 ADR 0003。

---

## 阶段 7：`while True` 是一份没有写下来的预算

### 最初会怎么写

模型不再提出工具调用时结束。

### 它会在哪里坏

模型可能永远重复同一个失败编辑，也可能继续寻找一个根本不存在的 bug。即使它最终停下来，我们也
无法区分成功、主动放弃、预算耗尽和撞上系统边界。

### 修复，以及它带来的成本

为步数、工具调用、墙上时间、token 和成本设置代理无法提高的硬预算。每次运行必须有且只有一个
`StopReason`，例如 `evidence_satisfied`、`no_progress`、`step_budget_exhausted` 或 `effect_unknown`。
同一个（工具、参数、结果）连续出现三次，就判定为卡死。

这里有一个很好的测试教训：结果里带有 `duration_ms` 时，重复检查只有在毫秒恰好碰撞时才会触发。
后来测试改为忽略这个不相关字段。依靠计时抖动才通过的测试，不是 flaky，而是错的。

实现位于 `src/haven/domain/budget.py`、`src/haven/domain/stuck.py`、`src/haven/application/run_service.py`，
以及 ADR 0006。

---

## 阶段 8：上下文要选择，不要无限积累

### 最初会怎么写

每轮都执行 `messages.append(...)`。

### 它会在哪里坏

上下文窗口会溢出；而且 prompt 的开头不断变化，provider 的前缀缓存无法复用，相同 token 被重复计费。
这个项目把易变的预算计数器从第二条消息移到尾部后，同一套件、同一模型的命中率从 70.9% 提升到 89.3%。

### 修复，以及它带来的成本

上下文布局为：稳定头部（系统规则、项目指导、目标）→ transcript → 易变尾部（计划和计数器）。
超出预算时，完整删除最老的工具单元，换成程序生成的路径、digest、退出码等摘要；调用和结果必须成对
删除，不能留下半个动作。

不要让模型写摘要。它可能编造“用户批准了这件事”这样的权限事实；丢失的信息可以重新读取，编造的
事实却很难纠正。结构化 digest 不包含文件内容和模型 prose，因此可以诚实地标成 `trusted`。LLM
摘要保留叙事价值，结构化 digest 更安全；哪个更值得要，应该由有边界的任务测量决定。ADR 0024
预先登记了切换方案的收益门禁。

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

`EFFECT_UNKNOWN` 必须由人类运行 `haven reconcile`，绝不自动重放。`repo.move` 甚至存在一个不可判定
窗口：目标已经写入、源文件尚未删除。继续可能重复 unlink，跳过又可能留下重复文件，所以系统有意
把它归为 unknown。系统不知道时，正确行为是承认不知道。

检查点负责“从哪里继续”，日志负责“实际发生了什么”。曾经有 13.4MB 存储中的 12.3MB 是被后续检查点
取代、从未读取的 transcript；把检查点当作可替换缓存，才能解决这个问题。

实现位于 `src/haven/adapters/sqlite_session.py`、`src/haven/application/recovery_service.py`、
`src/haven/contracts/checkpoint.py`，以及 ADR 0004。

---

## 阶段 10：不能用一个代理测试另一个代理

### 最初会怎么写

调用真实模型，然后断言输出。

### 它会在哪里坏

测试会变慢、变得不确定、需要 API key、需要付费，还会被 provider 或网络故障拖垮。最后大家选择
跳过测试，等于什么也没有。

### 修复：把两个问题拆开

机制是否可靠，用 `ScriptedModel` 替换模型，但保留真实文件系统、子进程、policy、approval 和 journal，
运行整套离线评估。每个用例都检查允许集合之外不能改文件，禁止内容不能进入 transcript；安全违规直接
让构建失败。

真实工作能力只能由在线评估回答：固定第三方仓库 commit，注入一个小 bug，确认带 bug 时检查失败、
回滚后检查通过，再花钱运行真实模型。离线测试证明的是机制，在线测试才触及模型行为和 provider 线格式。

这里的历史数据也教你怎样读指标：最初 27/31 的失败，主要来自可修复的重试分类和模型编辑测试，而
不是模型能力；修复后达到 31/31。后来又暴露出 `.pytest_cache` 的范围测量错误、sandbox 内无法通过的
task oracle，以及任务难度接近 100% 后必须换轴的问题。中立 grader 比较同一任务时，Haven 12/12、
opencode 10/12；后者的两个失败都是编辑测试让套件变绿。

实现位于 `src/haven/evalkit/runner.py`、`evals/`、`docs/EVAL.md`、`docs/EVAL_LIVE.md`，以及 ADR 0005。

---

## 阶段 11：知道什么不该构建

### 最初会怎么写

比较表上有你没有的功能，就把它们全部加进来。

### 它会在哪里坏

每个新能力都是一条穿过系统的路径。听起来最厉害的功能，往往会直接切穿前十个阶段建立的边界。

### 修复：在数据出现前写收益门禁

先写清它要修复哪类故障、什么测量能证明故障真实存在，再去测量。Haven 的结论包括：

| 推迟的能力 | 门禁或观察 | 结论 |
|---|---|---|
| 只读 LSP | 至少 5 次语义定位失败 | 实际约 1 次，暂不构建 |
| Planner / goal FSM | 规划失败占主导 | 实际主导是收敛问题，暂不构建 |
| Subagents | 长程超时能由委派修复 | 没有观察到这种形状，暂不构建 |
| MCP | 必须运行时发现工具的失败 | 没有，而且会破坏编译内置的不变量 |

实际主导失败是模型没有及时停止；架构不能替模型学会收敛，步数预算已经承担了边界职责。ADR 0007、
0016 先写门禁，ADR 0023 再记录数据结论。事后写门禁是合理化，先写再测才是工程。

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

如果你能逐项解释“没有它会坏什么”，就不只是记住了 Haven 的组件，而是理解了它们为什么存在。

## 接下来去哪里

- **逐层深入**：阅读模块 [01](01-mental-models.md) 到 [10](10-engineering-judgment.md)。
- **动手构建**：完成[结业项目](capstone.md)。
- **查看决策如何经受质疑**：阅读 [`docs/DESIGN_QA.md`](../docs/DESIGN_QA.md)。
- **了解真实故障**：阅读 [`docs/POSTMORTEM.md`](../docs/POSTMORTEM.md) 和
  [`docs/EVAL_LIVE.md`](../docs/EVAL_LIVE.md) 的故障章节。
