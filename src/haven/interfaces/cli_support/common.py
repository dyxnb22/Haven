"""CLI 各命令共享的稳定退出码。"""

from haven.domain.enums import RunStatus, StopReason

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_POLICY = 3
EXIT_PROVIDER = 4
EXIT_TOOL = 5
EXIT_STOPPED = 6
EXIT_RECOVERY = 7


def exit_code_for(status: RunStatus, stop_reason: StopReason) -> int:
    """将运行终态和停止原因映射为 CLI 稳定退出码。"""
    if status is RunStatus.SUCCEEDED:
        return EXIT_OK
    if status is RunStatus.EFFECT_UNKNOWN:
        return EXIT_RECOVERY
    if stop_reason is StopReason.PROVIDER_ERROR:
        return EXIT_PROVIDER
    if stop_reason is StopReason.TOOL_ERROR:
        return EXIT_TOOL
    return EXIT_STOPPED
