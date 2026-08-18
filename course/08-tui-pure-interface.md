# 模块 08——作为纯接口的终端 UI

[English](archive/en/08-tui-pure-interface.md) | **中文**

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

## UI 只做接口，而且由依赖规则强制保证

ADR 0001 和 CI 中的 import-linter 共同约束：TUI 把用户意图转成服务调用，从共享事件流渲染视图状态。
它不能拥有 policy、executor 或 provider。

如果 TUI 自己决定权限，系统就会有两套必须保持一致的安全模型；正确的架构是只有一套，而且它不在 UI 里。

## Presenter 是纯 reducer

阅读 `presenter.py`。核心是：

```text
reduce(state, event) -> state
```

它把上一个 `PresenterState` 和一个 `ApplicationEvent` 纯粹地变成下一个状态：没有 I/O、没有 widget、
没有时钟。真正的 Textual 壳 `app.py` 只负责接收事件，再把 reducer 产生的状态画出来。

这个形状有三个具体收益：

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

## 练习

1. **手动 reducer。** 参考 `test_presenter.py`，依次把 `RunCreated`、`StepStarted`、
   `ToolProposed`、`RunFinished` 送入 `reduce`，断言最终 `PresenterState`。不需要启动 Textual。
2. **恶意输入。** 给 presenter 传入包含 `\x1b[2J`（清屏）的模型文本，断言它不会出现在
   `PresenterState`。
3. **证明等价。** 阅读 golden trace 测试，用本模块的术语解释 TUI 与 headless 为什么必须生成相同 Trace。
4. **拒绝审批。** 扩展 `test_tui_journey.py`，加入在审批 modal 拒绝的运行，并断言文件保持不变。

## 自测

- Presenter 是纯 reducer，具体换来了哪三种能力？
- 为什么仓库文本对 renderer 也是不可信输入，而不只是对模型不可信？
- `haven replay` 不调用模型，怎样重建屏幕？

## 延伸阅读

- ADR 0001；`docs/ARCHITECTURE.md` 的 runtime event flow。
- 提交 `d90f873`（`feat(tui)`）和 `032f70d`（`test(golden)`）。
