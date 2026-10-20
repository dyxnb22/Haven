# 预备课 B——读懂 Haven 所需的 Python 与测试

[上一课：Agent 基础](prerequisite-01-agent-basics.md) | [返回课程总览](README.md) |
[源码伴读索引](code-reading-map.md) |
[下一课：第一次离线运行](prerequisite-03-first-offline-journey.md)

这是一份源码阅读工具箱，只讲 Python 3.12、Pydantic、asyncio、Protocol 和 pytest 在 Haven 中的职责。

## 本课路线

- **前置：** 会读函数、类、异常、列表/字典和基本类型注解。
- **建议用时：** 60–90 分钟。
- **本课产出：** 看到 Pydantic model、frozen dataclass、Protocol、`AsyncIterator` 和 pytest fixture 时，
  能说出它们为什么在这一层出现。
- **最小验证：**

```bash
uv run pytest -q tests/contract/test_scripted_model.py tests/unit/test_policy.py
```

## 本课的项目锚点

| 要读懂的构件 | 当前项目位置 | 用它的原因 |
|---|---|---|
| Pydantic `StrictModel` | `contracts/base.py`、`contracts/model.py`、`contracts/tools.py` | 校验跨边界的数据形状 |
| frozen dataclass | `domain/budget.py`、`domain/policy.py` | 表达不可随意修改的领域值 |
| `Protocol` port | `ports/model.py`、`ports/session.py`、`ports/workspace.py` | 核心定义需要，adapter 提供实现 |
| `AsyncIterator` | `ModelPort.generate_stream` | 按顺序消费 Provider 流事件 |
| 依赖注入 | `bootstrap.py`、`tests/integration/harness.py` | 在真实与离线实现之间装配 |
| 结构化错误 | `contracts/tools.py::ToolResult`、`ports/model.py::ProviderError` | 让循环按错误类别处理 |
| 稳定摘要 | `domain/approval.py`、`domain/ticket.py` | 绑定审批和执行动作 |
| 条件更新 | `adapters/sqlite_session.py::consume_approval` | 在存储层保证最多消费一次 |

## 1. 先看项目形状，不要先钻进最大文件

Haven 使用 `src/` 布局：

```text
src/haven/
  contracts/     跨边界传递的数据形状
  domain/        不依赖 I/O 的规则与判断
  ports/         核心需要外部世界提供什么
  application/   运行循环与用例编排
  adapters/      文件、进程、SQLite、Provider 的具体实现
  interfaces/    CLI 和 TUI
  bootstrap.py   把上述对象装配起来
tests/
  unit/ contract/ integration/ security/ recovery/ tui/ golden/ eval/
```

先记依赖方向：

```text
interfaces → application → domain/contracts/ports ← adapters
                         ↑
                    bootstrap 负责装配
```

核心通过 port 声明需要，adapter 提供实现。application 不直接导入 adapter，`import-linter` 会强制这条规则。

## 2. 类型注解不是运行时校验

普通 Python 注解：

```python
def read(path: str) -> str: ...
```

不会阻止运行时调用 `read(123)`。`mypy` 做静态检查；模型生成的 JSON 必须另做运行时验证。

Haven 的边界对象继承 Pydantic `StrictModel`：

```python
class ToolCallProposal(StrictModel):
    call_id: str
    tool_name: str
    arguments_json: str
```

Pydantic 回答“数据形状是否合法”；动作是否允许、文件是否存在，分别属于 policy 和程序采集的 facts。

阅读规则：

- 看到 `contracts/` 中的 model，先问它跨越哪条边界；
- 看到 `domain/` 中的 dataclass，先问它表达哪条规则或事实；
- 不要把“通过 schema”误读成“可以执行”。

## 3. 判别联合：事件为什么都有 `kind`

模型流可能产生文本、推理片段、完整工具调用、用量和结束事件。Haven 用带判别字段的联合表示：

```python
class TextDelta(StrictModel):
    kind: Literal["text_delta"] = "text_delta"
    text: str


class StreamFinished(StrictModel):
    kind: Literal["finished"] = "finished"
    finish_reason: Literal["stop", "tool_calls", "length", "error"]
```

消费者按 `event.kind` 分支，不必猜可选字段。应用事件也采用同一方式；序列化后，`kind` 还负责恢复类型。

## 4. Frozen dataclass：把状态变化写成新值

预算和许多领域值使用不可变 dataclass：

```python
@dataclass(frozen=True, slots=True)
class BudgetUsage:
    steps: int = 0
    tool_calls: int = 0

    def charge_step(self) -> BudgetUsage:
        return replace(self, steps=self.steps + 1)
```

`frozen=True` 阻止字段被随手修改，`replace` 返回新值。这让状态变化更适合检查点、测试和纯函数。

阅读 domain 代码时优先找这种形状：

```text
输入值 → 纯函数 → 新值 / 明确判定
```

例如 policy 不读取磁盘；磁盘事实由 application/adapters 先收集，再作为值传给 policy。

## 5. Protocol：核心定义需要，外层提供实现

`ports/model.py` 中的核心接口大致是：

```python
class ModelPort(Protocol):
    @property
    def model_name(self) -> str: ...

    def generate_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]: ...
```

实现类不必显式继承 `ModelPort`；只要具有相同结构，静态类型检查就认为它满足协议。真实
`OpenAICompatibleModel` 和离线 `ScriptedModel` 因而可以被同一个 `RunService` 使用。

核心先声明需要，adapter 再满足；因此替换 Provider 和离线测试都不必修改循环。

看到 Protocol 时回答三个问题：

1. 谁拥有这个接口？应该是使用者一侧的核心。
2. 哪些 adapter 实现它？
3. 是否有契约测试对所有实现运行同一套行为？

## 6. `async`、异步迭代与取消

LLM 流、事件 sink、SQLite 和子进程都涉及等待。最小语义是：

```python
async def consume(model: ModelPort, request: ModelRequest) -> None:
    async for event in model.generate_stream(request):
        handle(event)
```

- 调用 `async def` 得到 coroutine；`await` 或创建 task 后才执行；
- `async for` 逐个消费异步事件，不必等整个响应完成；
- `await` 是任务可能让出控制权、也可能接收取消的位置；
- 超时与取消必须继续传播并清理资源，不能用宽泛 `except Exception` 悄悄吞掉。

先沿一条流追踪即可：Provider adapter 产出 `ModelEvent`，`RunService` 组装 `ModelResult`，
`EventEmitter` 把应用事件交给 journal 和 UI。

## 7. 依赖注入：Harness 为什么能完全离线

`RunService` 不在内部创建 HTTP client、SQLite 或真实文件系统；这些依赖由构造函数传入。集成测试因此
可以组装：

```text
ScriptedModel
  + 临时目录中的真实 FsWorkspace
  + MemorySessionStore
  + RecordingLauncher
  + AutoApprover
  → RunService
```

只有模型被脚本替换；policy、工具管线、文件读写、证据门禁和循环仍是真实现。

`tests/integration/harness.py` 是理解组合根的捷径：它比生产 `bootstrap.py` 小，却展示了完整依赖图。

## 8. 错误作为数据，异常留给真正意外

参数错误、路径不存在和权限拒绝是预期失败，应让模型有机会修正下一步。

Haven 把预期工具失败映射为稳定结果：

```text
ToolResult(status="error", error_code="not_found", message="...")
```

数据库损坏、非法状态迁移等不变量破坏则应抛异常。判断标准是：调用者是否应根据该失败继续决策。

## 9. Coding Agent 的文件与进程词汇

| 词 | 最小含义 | 在 Haven 中为什么重要 |
|---|---|---|
| **workspace** | 当前 Agent 被允许操作的目录边界 | 路径必须规范化后仍位于边界内 |
| **preimage / postimage** | 动作前 / 预期动作后的文件内容或摘要 | 审批、TOCTOU 检查和崩溃恢复的依据 |
| **diff** | 两个文件状态之间的净变化 | 证明本次运行最终改了什么，不等于“曾执行过 edit” |
| **process** | 操作系统正在运行的程序实例 | check/exec 可能读取、写入、联网或产生子进程 |
| **argv** | 不经 shell 二次解析的参数数组 | 比任意 shell 字符串更容易审查，但仍需 sandbox |
| **cwd** | 进程的当前工作目录 | 相对路径从这里解析，不能只检查命令名 |
| **stdout / stderr** | 进程的普通输出 / 错误输出 | 可能很大、含敏感信息，也不自动代表成功或失败 |
| **exit code** | 进程结束时的整数状态 | 通常 0 表示 recipe 通过，但要结合工作区、时机和配方身份 |
| **atomic write** | 先写临时文件，再以单次替换发布 | 避免目标文件只写了一半 |
| **sandbox** | 由操作系统强制的文件、网络、进程能力边界 | 即使模型或仓库诱导命令，内核仍限制爆炸半径 |

工作区可能已有用户修改，因此 Haven 记录运行基线与文件原件，计算本次运行的变化；Git commit 不是
唯一依据，也不能用来粗暴恢复整个仓库。

## 10. Digest、稳定序列化与一次性更新

审批和恢复要求同样输入跨进程产生相同 digest，因此使用稳定序列化与 SHA-256，而不是进程间可能变化的
Python `hash()`。

一次性审批最终由数据库条件更新保证，逻辑类似：

```sql
UPDATE approvals
SET consumed_at = :now
WHERE id = :id
  AND digest = :digest
  AND consumed_at IS NULL;
```

只有影响一行才算消费成功。检查与修改在同一个数据库原语中完成，一次性保证因此落在存储层。

## 11. 测试是最短的可执行说明书

Haven 的测试目录不是按“都叫 test”堆在一起，而是回答不同问题：

| 类型 | 回答的问题 | 例子 |
|---|---|---|
| unit | 一个纯规则在边界输入下怎样判断？ | policy、budget、evidence |
| contract | 两个实现是否遵守同一接口？ | real/scripted model、memory/SQLite store |
| integration | 真实组件串起来是否完成旅程？ | read/edit/diff/check |
| security | 是否存在越界或 sandbox 逃逸？ | workspace escapes |
| recovery | 崩溃后的效果怎样分类？ | not run / confirmed / unknown |
| golden | 事件协议是否意外漂移？ | edit journey trace |
| eval | 多个任务和攻击样本是否整体回归？ | offline suite |

读模块时先读测试名和断言，预测结果，再进实现找原因。fixture 构造环境，assert 固定行为，参数化用例
展示需要覆盖的维度。

## 12. 现在必须会与可以推迟

**继续课程前应该会：**

- 区分静态注解、Pydantic 运行时校验和 domain policy；
- 解释 Protocol/adapter 的依赖方向；
- 看懂 `async def`、`await`、`async for` 的控制流；
- 从 pytest 的 arrange / act / assert 找到输入和预期；
- 理解稳定 digest 不是加密授权本身，而是把同一动作绑定成同一身份。

**可以按需再学：**

- descriptor、metaclass 和 CPython 字节码；
- asyncio scheduler 的内部实现；
- Textual widget 生命周期和 CSS；
- SQLite 查询优化；
- Pydantic 高级自定义 schema。

## 下一步

继续[预备课 C](prerequisite-03-first-offline-journey.md)，把这些构件放回一条完整的离线 Agent 旅程。
