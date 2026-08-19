"""沙箱端口：操作系统如何限制子进程。

启动器将一条命令转换为包装后的命令。保持这一步为纯转换意味着无需真正运行
任何东西即可在测试中断言配置，并且实际执行的始终是已经受到操作系统策略约束
的程序。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class SandboxSpec:
    """一个受限进程可以接触的内容。"""

    #: 子进程可见的工作区根目录，也是默认的工作目录范围。
    workspace_root: Path
    #: 可写的临时目录，使必须写入某处的工具不需要访问工作区之外的路径。
    scratch_dir: Path
    #: 工作区本身是否允许写入；只读运行会将其设为 False。
    writable: bool
    #: 是否允许子进程访问网络；默认关闭，只有显式授权的配方才会打开。
    allow_network: bool = False
    #: 永远不可读。工作区和临时目录的授权会重新打开运行确实需要的部分，
    #: 这使得位于 $HOME 内的工作区仍能正常工作。
    private_roots: tuple[Path, ...] = ()
    #: 可在系统根目录之外读取的路径——Python 前缀目录，从而保证位于 $HOME
    #: 下的解释器仍可执行。
    extra_readable_roots: tuple[Path, ...] = ()
    #: 与 FsWorkspace.PROTECTED_COMPONENTS 保持同步：Git 历史、本地数据目录
    #: 以及运行绝不能重写的项目配置。
    protected_subpaths: tuple[str, ...] = (".git", ".haven", ".haven.toml")


class SandboxLauncher(Protocol):
    """操作系统沙箱的统一接口；不可用时调用方必须失败关闭。"""

    @property
    def backend(self) -> str:
        """返回稳定的沙箱后端名称，用于事件和诊断。"""
        ...

    def available(self) -> bool:
        """返回当前操作系统是否能够实际启用该后端。"""
        ...

    def wrap(self, argv: tuple[str, ...], spec: SandboxSpec) -> tuple[str, ...]:
        """将目标 argv 转换为带操作系统限制的实际启动 argv。"""
        ...

    def describe(self, spec: SandboxSpec) -> str:
        """返回面向用户的有界限制摘要，不执行任何进程。"""
        ...


def default_private_roots() -> tuple[Path, ...]:
    """用户主目录，凭据实际存放的位置。"""
    try:
        return (Path.home(),)
    except RuntimeError:
        return ()


def default_readable_roots() -> tuple[Path, ...]:
    """当前解释器的前缀目录，使位于 $HOME 下的 virtualenv 仍能被检查配方执行。"""
    roots = {Path(sys.prefix), Path(sys.base_prefix), Path(sys.executable).parent}
    return tuple(sorted(roots))
