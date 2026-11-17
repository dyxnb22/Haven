# 模块 08——作为纯接口的终端 UI

> 文件：`src/haven/interfaces/tui/presenter.py`、`src/haven/interfaces/tui/app.py`、
> `src/haven/interfaces/cli.py`、`src/haven/application/emitter.py`
> 测试：`tests/tui/test_presenter.py`、`tests/tui/test_tui_journey.py`、
> `tests/tui/test_tui_robustness.py`、`tests/golden/`
> ADR：[0001——语言与范围](../docs/adr/0001-language-and-scope.md)

## 学习目标

- 让 UI 只负责把用户意图交给服务、把事件渲染成视图，不持有业务逻辑；
- 把 presenter 写成事件流上的纯 reducer；
- 把模型和仓库文本视为 renderer 的不可信输入，而不只是模型的不可信输入；
- 在没有真实终端、没有网络的情况下，确定性测试 hostile input 和取消行为。

## 本课路线

- **前置：** 模块 01 的 Trace，模块 07 的 replay；不要求熟悉 Textual。
- **建议用时：** 45 分钟。
- **阅读顺序：** 先直接调用纯 `reduce`，再读 presenter 测试，最后看 `app.py` 怎样接线；不要从
  widget 生命周期开始。
- **最小验证：**

```bash
uv run pytest -q tests/tui/test_presenter.py tests/golden/test_golden_trace.py
```

## UI 只做接口，而且由依赖规则强制保证

ADR 0001 和 CI 中的 import-linter 共同约束：TUI 把用户意图转成服务调用，从共享事件流渲染视图状态。
它不能拥有 policy、executor 或 provider。

TUI 若自行决定权限，系统就会出现两套安全模型；权限逻辑必须留在应用与领域层。

## Presenter 是纯 reducer

阅读 `presenter.py`。核心是：

```python
def reduce(state: PresenterState, envelope: EventEnvelope) -> PresenterState:
    event = envelope.event
    ...
```

它把 `PresenterState` 和 `EventEnvelope`（包含 `ApplicationEvent` 与序号）变成下一个状态，不做 I/O，
也不接触 widget 或时钟。Textual 壳 `app.py` 只接收事件并渲染 reducer 的结果。

它带来三个结果：

- **无需终端也能测试。** `test_presenter.py` 直接构造事件调用 `reduce`，不需要 Textual、异步或屏幕；
- **重放天然可用。** live 事件和 journal 事件经过同一个 reducer，`haven replay` 可以重建屏幕；
- **一个事件流，三个消费者。** headless CLI、TUI 和 replay 都订阅 `emitter.py` 产生的
  `ApplicationEvent`。golden trace 测试断言 TUI 和 headless 的 Trace 完全一致；若 UI 真的在做决策，
  这种一致性不可能长期成立。

## 到屏幕的文本同样不可信

仓库内容和模型输出最终都会显示在终端，因此它们对 renderer 来说也是不可信输入。`presenter.py`
会在显示前剥离 ANSI 与控制字符，并限制长度。`test_tui_robustness.py` 会投喂 ANSI bomb、Unicode/emoji、
十万行 diff、20×6 小终端和连续按键，断言应用不崩溃、转义序列不穿透。

恶意仓库不应该能清屏、改颜色、伪造一条看起来像系统提示的行。这里防的是显示完整性，不是用 UI 代替
policy 或 OS sandbox。

## 背压与取消

`app.py` 用有界队列连接运行时和 UI。流式增量属于瞬态信息，压力过大时可以丢弃；权威事件必须施加
背压，不能丢。Ctrl-C 会先取消运行再退出，并把取消传递给模型请求和子进程；取消的运行仍然要以一个命名
`StopReason` 结束，并持久化 `run.finished`。

## 审批是绑定到一个动作的 modal

审批对话框展示精确 diff 和授权它的 digest。用户在这里确认的只是当前待处理动作，不能顺手批准别的动作。
`test_tui_journey.py` 完全离线地驱动整个流程：提交任务、批准 edit 和 check、确认成功，并确认文件确实变化。

## 延伸阅读

- ADR 0001；`docs/ARCHITECTURE.md` 的 runtime event flow。
- 提交 `d90f873`（`feat(tui)`）和 `032f70d`（`test(golden)`）。
