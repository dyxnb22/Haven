# 安装与升级

**中文** | [English](INSTALL_UPGRADE.md)


Haven 的验收平台是 macOS 和 Linux。准备 Python 3.12 或更新版本、Git 和 uv。
使用带 SQLite `RETURNING` 与 JSON 函数的 Python 解释器。`uv sync` 按仓库的 `uv.lock`
安装依赖。ripgrep 可选，缺少时使用 Python 搜索。

## 干净安装

在源码目录中安装运行依赖并检查工作区。`--no-sync` 让后续命令复用同一环境。

```bash
uv sync --locked --no-default-groups --no-editable
uv run --no-sync haven --version
uv run --no-sync haven doctor --workspace /path/to/repository
haven_check=$(mktemp -d "${TMPDIR:-/tmp}/haven-offline.XXXXXX")
uv run --no-sync haven eval --offline --out "$haven_check"
```

`doctor` 会显示可用的沙箱后端。macOS 使用 Seatbelt 的 `sandbox-exec`，Linux 的
Landlock 需要内核提供 ABI 4 或更新版本。后端不可用时 `repo.exec` 会被策略拒绝。
仓库读取和文件工具继续走常规权限通道，已注册的 `repo.check` 仍可在本地信任模式下
通过审批执行。需要真实模型时，再配置 Provider 密钥。干净配置下 MCP、双轨路由和
临时工作树均为显式选项，离线评估使用脚本模型。

## 升级已有会话

1. 用当前安装的 `haven config explain` 找到 `storage.db`、`storage.artifacts`
   和 `skills.user_dir`。用户配置文件在 `skills.user_dir` 的上一级。
2. 退出 Haven。备份整个数据目录及用户配置目录。数据目录包含数据库、工件和恢复资料。
   `HAVEN_DATA_DIR` 和 `HAVEN_CONFIG_DIR` 可用来指向备份的副本做演练。
3. 安装候选版本，在原工作区路径打开一条已保存运行。`haven sessions list` 或
   `haven sessions show RUN_ID` 首次打开数据目录时会迁移支持的旧库。
   `doctor` 只检查环境。核对历史后，可用 `haven resume RUN_ID --workspace PATH` 恢复
   中断任务。恢复仍会检查工作区身份、原文件工件和不确定的副作用。
4. 需要回退时，先关闭候选版本。让旧安装通过 `HAVEN_DATA_DIR` 和 `HAVEN_CONFIG_DIR`
   读取升级前的备份副本。工作区文件按各自的版本记录恢复。

`v0.1.0` 标签对应 SQLite schema 2，当前候选使用 schema 6。迁移在单个事务中执行。
不支持的 schema 会在写入之前被拒绝，迁移失败时关闭连接并回滚未提交的 schema 变更。
备份保留了旧版能读取的原始检查点与工件，也适用于旧版严格解析器遇到新事件字段的情况。

## 维护者走查

从含 `v0.1.0` 标签的本地仓库执行以下命令。脚本分别构建旧版与当前源码的 wheel，
在两个临时环境安装锁定的运行依赖，并输出各自的版本与 SHA-256。

```bash
uv run --locked python scripts/check_install.py
```

走查包含 `doctor`、离线评估、旧版完成文件修改后中断、新版回放并续跑、旧事件逐字节
比较，以及让旧版重新打开升级前的备份。工作区、数据库、构件和离线报告均位于临时目录。

当前走查覆盖 macOS arm64 的 Seatbelt 和 Linux arm64 容器的 Landlock。
两端均用 Python 3.12 的独立 wheel 安装通过离线评估和 schema 2 到 6 的升级。
具体结果见[路线图实施记录](PERSONAL_ROADMAP.md#实施记录)。
