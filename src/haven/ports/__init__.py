"""核心层拥有的端口；适配器实现这些协议。

每个端口都是应用层对外部世界的全部认知：

    model.py       流式获取一次补全（ModelPort）+ ProviderError 分类
    workspace.py   有界文件访问：读取/搜索/编辑/补丁预览与应用、路径事实、
                   运行作用域内的差异
    executor.py    运行已注册配方/沙箱命令
    sandbox.py     包装 argv，使操作系统限制子进程
    session.py     持久存储：运行、事件、检查点、审批、执行日志、构件
    event_sink.py  发出的事件封装前往的目标（UI、JSONL、重放）
    clock.py       可注入的时间

替换适配器（例如在测试中将 SQLite 换成内存存储）可以改变性能和持久化方式，但
永远不会改变权限或证据规则——这些规则位于领域层。
"""

from haven.ports.clock import ClockPort
from haven.ports.event_sink import EventSinkPort
from haven.ports.executor import CheckOutcome, ExecutorPort
from haven.ports.model import ModelPort, ProviderError, ProviderErrorCode
from haven.ports.session import ExecutionRecord, RunRecord, SessionStorePort
from haven.ports.workspace import (
    EditOutcome,
    EditPreview,
    ListEntry,
    ListResult,
    PathFacts,
    ReadResult,
    RunDiff,
    SearchMatch,
    SearchResult,
    WorkspaceError,
    WorkspacePort,
)

__all__ = [
    "CheckOutcome",
    "ClockPort",
    "EditOutcome",
    "EditPreview",
    "EventSinkPort",
    "ExecutionRecord",
    "ExecutorPort",
    "ListEntry",
    "ListResult",
    "ModelPort",
    "PathFacts",
    "ProviderError",
    "ProviderErrorCode",
    "ReadResult",
    "RunDiff",
    "RunRecord",
    "SearchMatch",
    "SearchResult",
    "SessionStorePort",
    "WorkspaceError",
    "WorkspacePort",
]
