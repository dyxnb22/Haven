"""应用层：通过 ports 编排领域逻辑的用例。

定义系统核心的两个文件位于此处：

    run_service.py    有界代理循环（一次轮次 = context -> model -> tools ->
                      evidence -> checkpoint）
    tool_pipeline.py  每个模型提出的操作都必须经过的唯一执行通道

由以下模块支持：context_builder（模型看到什么）、compaction（被丢弃的历史变成记录
事实）、approvals（流水线与人工/自动策略之间的 broker）、registry（静态工具查找和
严格验证）、recovery_service（崩溃恢复和用户 rewind）、replay_service（日志投影）、
maintenance（haven gc：清理运行和构件）、profiles（按模型的默认值）、emitter（持久化
并分发事件）、state（每次运行可变的 RunContext）。

此层只了解 domain、ports 和 contracts，绝不依赖具体 adapter；bootstrap.py 负责注入它们。
"""

from haven.application.approvals import ApprovalResponder, AutoApprover, QueueApprovalBroker
from haven.application.context_builder import ContextBuilder
from haven.application.emitter import EventEmitter
from haven.application.profiles import DEFAULT_PROFILE, ModelProfile, profile_for
from haven.application.recovery_service import (
    EffectFinding,
    RecoveryReport,
    RecoveryService,
)
from haven.application.registry import ToolRegistry, ValidationFailure
from haven.application.replay_service import ReplayService
from haven.application.run_service import (
    RunOutcome,
    RunService,
    build_run_context_from_checkpoint,
)
from haven.application.state import RunContext
from haven.application.tool_pipeline import ToolExecution, ToolPipeline

__all__ = [
    "DEFAULT_PROFILE",
    "ApprovalResponder",
    "AutoApprover",
    "ContextBuilder",
    "EffectFinding",
    "EventEmitter",
    "ModelProfile",
    "QueueApprovalBroker",
    "RecoveryReport",
    "RecoveryService",
    "ReplayService",
    "RunContext",
    "RunOutcome",
    "RunService",
    "ToolExecution",
    "ToolPipeline",
    "ToolRegistry",
    "ValidationFailure",
    "build_run_context_from_checkpoint",
    "profile_for",
]
