"""执行器端口：已注册的验证配方和沙箱命令。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from haven.contracts.tools import RecipeSpec
from haven.ports.sandbox import SandboxSpec


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    """验证配方的进程结果及有界输出。

    ``stdout_tail`` 和 ``stderr_tail`` 只保留执行器允许返回的末尾内容；当输出
    被截断时 ``truncated`` 为真。所有耗时字段均以毫秒计，超时由 ``timed_out``
    明确标记，即使命令最终返回了非零退出码。
    """

    #: 产生此结果的已注册 recipe 标识。
    recipe_id: str
    #: 子进程退出状态；非零通常表示检查失败。
    exit_code: int
    #: 进程墙上时钟耗时，单位为毫秒。
    duration_ms: int
    #: 有界的标准输出末尾内容。
    stdout_tail: str
    #: 有界的标准错误末尾内容。
    stderr_tail: str
    #: 任一输出流超过保留上限时为 True。
    truncated: bool
    #: 执行器因达到超时而终止进程时为 True。
    timed_out: bool


@dataclass(frozen=True, slots=True)
class ExecSpec:
    """一条需要在受限环境中运行的命令。

    `argv` 是提议的程序；包装在执行器内部完成，因此系统中只有一个可能忘记执行
    包装的位置。
    """

    #: 分开的程序和参数项；不会解释 shell 语法。
    argv: tuple[str, ...]
    #: 工作区内的工作目录。
    cwd: Path
    #: 进程硬超时时间，单位为秒。
    timeout_seconds: float
    #: 应用于子进程的操作系统级文件系统/网络限制。
    sandbox: SandboxSpec


@dataclass(frozen=True, slots=True)
class ExecOutcome:
    """任意受限命令的进程结果及有界输出。

    输出字段是有界的尾部文本，避免子进程把运行内存或事件日志撑爆；
    ``duration_ms`` 以毫秒计，``timed_out`` 区分超时终止与普通退出。
    """

    #: 子进程退出状态。
    exit_code: int
    #: 进程墙上时钟耗时，单位为毫秒。
    duration_ms: int
    #: 有界的标准输出末尾内容。
    stdout_tail: str
    #: 有界的标准错误末尾内容。
    stderr_tail: str
    #: 任一输出流超过保留上限时为 True。
    truncated: bool
    #: 执行器因达到超时而终止进程时为 True。
    timed_out: bool


class ExecutorPort(Protocol):
    """执行已由策略批准的验证配方或沙箱命令。"""

    async def run_recipe(self, recipe: RecipeSpec, workspace_root: Path) -> CheckOutcome:
        """在配方声明的超时、网络和可读根目录限制下运行检查。"""
        ...

    async def run_exec(self, spec: ExecSpec) -> ExecOutcome:
        """在给定沙箱规格中运行任意已获批准的程序并返回有界输出。"""
        ...
