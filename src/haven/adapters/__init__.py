"""提供商、文件系统、进程和持久化的具体 adapters。

每个模块实现 `haven.ports` 中的一个 port；只有 `bootstrap.py`（组合根）可以将它们
接入服务：

    providers/            ModelPort：OpenAI 兼容流式 adapter，以及测试/评估使用的
                          确定性 ScriptedModel
    workspace_fs.py       真实文件系统上的 WorkspacePort：规范化路径、preimage 摘要、
                          原子写入、补丁事务，以及用于 diff/rewind 的运行级原始内容
    process_executor.py   ExecutorPort：固定 argv 的配方和沙箱命令、清理后的环境、
                          有界输出
    sandbox/              SandboxLauncher 后端：Seatbelt（macOS）和 Landlock（Linux），
                          在 bootstrap 中选择
    sqlite_session.py     SQLite/aiosqlite（WAL）上的 SessionStorePort，包含只追加事件日志
                          和 schema 迁移
    memory_session.py     内存中的同一契约，用于测试和评估
    git_baseline.py       记录运行开始时仓库的 Git 状态
    workspace_lease.py    跨进程的提示性单写者租约
"""
