# 模块 07——持久化执行：检查点、日志、恢复与重放

[English](archive/en/07-durable-execution.md) | **中文**

> 文件：`src/haven/adapters/sqlite_session.py`、`src/haven/application/recovery_service.py`、
> `src/haven/application/replay_service.py`、`src/haven/contracts/checkpoint.py`、`src/haven/config.py`
> 测试：`tests/recovery/`、`tests/contract/test_session_store.py`
> ADR：[0004——持久化执行与恢复](../docs/adr/0004-durable-execution-and-recovery.md)

## 学习目标

- 区分用于快速恢复的检查点和追加写入的日志，并知道谁是权威；
- 判断中断副作用属于“未执行”“已确认”还是“不明确”；
- 把运行重放为日志的纯投影，而不是再次运行；
- 设计只能收紧、不能放宽安全边界的配置层级。

## 检查点和日志不是一回事

只有检查点不够：进程可能恰好死在“副作用已经发生”和“程序记录它已经发生”之间。Haven 同时保留两类记录：

- **检查点**（`checkpoints` 表、`contracts/checkpoint.py`）：带版本和校验和的运行状态快照，包括
  目标、状态、预算/用量、transcript、证据、已读文件、计划，以及运行开始前文件原件的摘要。它服务于
  快速恢复，加载时会校验；schema 或 checksum 不匹配就默认失败。
- **日志**（`events` 表）：每个事件带 digest 的追加审计记录，可用于重放。`executions` 表是执行日志，
  记录每个副作用的生命周期：`started → confirmed | failed | effect_unknown`。

两者冲突时，以日志为准。检查点是方便恢复的缓存，日志才是事实依据。

## 最重要的规则：不明确，就不要自动重放

阅读 `RecoveryService.inspect`。恢复时，它会找出所有已经 `started` 却没有 `confirmed` 的副作用，
再用磁盘当前 digest 和记录中的 preimage/postimage 比较：

| 当前文件 | 分类 | 动作 |
|---|---|---|
| 等于 preimage | 未执行 | 可以自动校正并继续恢复 |
| 等于 postimage | 已确认 | 可以自动校正，不要重复 |
| 两者都不是 | 副作用不明确 | 阻塞，等待人类决定 |

不明确的副作用必须由人类明确处理：

```bash
haven reconcile RUN_ID CALL_ID --as confirmed|not_run|abandon
```

Haven 不猜，因为重复一个可能已经完成的写入，比停下来问人更糟。

> 如果你无法证明副作用没有发生，就不要重复它。

测试覆盖未执行、已确认、不明确、放弃，以及工作区身份不匹配。恢复前还会验证 workspace identity，
防止一个仓库的检查点被拿到另一个仓库继续使用。

## 重放是投影，不是重新运行

`ReplayService` 只把日志中的事件重新交给 sink，不调用模型，也不调用任何工具。TUI 是同一事件流上的
纯 reducer，因此 `haven replay` 能重建等价屏幕。这也解释了 golden trace 测试为什么可以断言 TUI
和 headless 产生相同 Trace：两者都是日志消费者。

## 值得照搬的持久化细节

阅读 `sqlite_session.py`：

- 使用 WAL；数据位于平台 data dir（可由 `HAVEN_DATA_DIR` 覆盖），始终在工作区外，`repo.*` 工具不能
  碰到会话数据库；
- `(run_id, seq)` 唯一；每个事件存储 content digest，加载时再次验证；
- 审批消费是条件 `UPDATE`：同时匹配 id、digest 和 `consumed_at IS NULL`，一次性保证落在数据库层；
- `memory_session.py` 实现同一个 port，契约测试对内存和 SQLite 两种实现运行同一套测试，防止快速替身
  和真实实现渐渐分叉。

## 只能收紧的配置

`config.py` 按顺序合并：内置安全默认值 → 用户配置 → provider 环境变量与 CLI 选择的预算层级 → 项目
`.haven.toml`。项目文件最后应用，但只能降低预算和登记 recipe，不能提高限制、改变 provider 或放宽
agent policy。

recipe 可以声明固定的进程能力，例如网络和可读取的根目录，因为它来自用户配置而非模型输入。secret
只能来自环境变量，系统只报告存在或缺失，绝不打印值。

## 练习

1. **模拟崩溃。** 构造一个 edit 已 `started` 但未 `confirmed` 的运行，分别让文件匹配 preimage、
   postimage，以及两者都不匹配；观察恢复、自动校正和阻塞。
2. **破坏检查点。** 在测试中修改 checksum，断言加载器默认失败。
3. **重放。** 运行一个脚本流程，再执行 `haven replay RUN_ID`，确认不会调用模型或工具。
4. **只收紧。** 写一个试图把 `max_steps` 调高到默认值以上的 `.haven.toml`，用
   `haven config explain` 证明它没有生效。

## 自测

- 检查点和日志各自解决什么问题？谁是权威？
- 用一句话说出“不明确副作用”的处理规则。
- 一次性审批的保证在物理上位于哪里？为什么条件 `UPDATE` 是正确原语？

## 延伸阅读

- ADR 0004：恢复设计。
- 提交 `8e91e69`（持久化）、`7ff184e`（恢复与重放）、`d55ff10`（配置）。
