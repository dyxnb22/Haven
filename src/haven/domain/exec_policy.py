"""对提议命令行进行分类。

本模块决定命令需要多少审批摩擦，但绝不决定它能够*写入什么*：写入能力由命令
运行所在的操作系统沙箱限制，因此分类错误最多导致漏掉一次提示，不会造成逃逸。

读取是例外，也是 `SAFE_READ` 比名称所暗示的范围更窄的原因。沙箱限制写入并隐藏
`$HOME`，但会有意保留其余文件系统的可读性，以便普通解释器启动；而 `repo.exec`
只校验 `cwd`，不校验 `argv` 内的路径。因此，自动允许对绝对路径执行 `cat` 会读取
人类从未批准的文件，输出还会回到对话记录，也就是回到模型提供商。在 Linux 上，
`/proc/<parent-pid>/environ` 甚至可以绕过子进程已清理的环境，触达父进程环境。

因此，这里的摩擦等级依据的是*操作数*，而不只是程序：只读命令在工作区内时保持
静默，一旦操作数指向工作区外就立即要求审批。
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import PurePosixPath


class ExecClass(StrEnum):
    SAFE_READ = "safe_read"
    SHELL_PASSTHROUGH = "shell_passthrough"
    OTHER = "other"


#: 只进行观察的命令前缀。按前缀建立索引，使子命令可以与父命令分别分类
#:（例如 `git status` 和 `git push`）。
_SAFE_PREFIXES: frozenset[tuple[str, ...]] = frozenset(
    {
        ("ls",),
        ("cat",),
        ("head",),
        ("tail",),
        ("wc",),
        ("rg",),
        ("grep",),
        ("git", "status"),
        ("git", "log"),
        ("git", "diff"),
        ("git", "show"),
    }
)

_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish"})

#: 与使其执行内联源码的标志配对的解释器。
_INLINE_CODE_FLAGS: dict[str, frozenset[str]] = {
    "python": frozenset({"-c"}),
    "node": frozenset({"-e", "--eval", "-p", "--print"}),
    "deno": frozenset({"eval"}),
    "ruby": frozenset({"-e"}),
    "perl": frozenset({"-e", "-E"}),
}

#: `find` 在遇到其中任一项之前只会无害地遍历目录树。
_FIND_ACTION_FLAGS = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir"})

_MAX_PREFIX_LEN = 2


def _program(argv0: str) -> str:
    """取基名，使 /bin/ls 和 ls 得到相同分类。"""
    return PurePosixPath(argv0).name


def _interpreter_family(program: str) -> str | None:
    """将 python3.12 映射为 python；其他无关名称保持不变。"""
    for family in _INLINE_CODE_FLAGS:
        if program == family or program.startswith(family):
            return family
    return None


def _operand_escapes_workspace(operand: str) -> bool:
    """此操作数是否可能指向工作区之外的内容？

    这里有意采用保守的语法判断：根本不是路径的操作数（grep 模式、git 引用、
    `-n` 的值）不会被判断为绝对路径，因此检查每个操作数几乎没有成本。判断
    失败时只会多出一次审批提示，绝不会静默读取。
    """
    candidate = operand
    if candidate.startswith("-"):
        # 单独的标志没有命名任何路径，但 `--file=/etc/shadow` 会把路径藏在其中。
        _, separator, value = candidate.partition("=")
        if not separator:
            return False
        candidate = value
    if not candidate:
        return False
    if candidate.startswith(("/", "~")):
        return True
    return ".." in PurePosixPath(candidate).parts


def _operands_escape_workspace(operands: tuple[str, ...]) -> bool:
    return any(_operand_escapes_workspace(item) for item in operands)


def classify_argv(argv: tuple[str, ...]) -> ExecClass:
    """对一条提议命令进行分类。这是纯函数，并且对所有输入都有定义。"""
    if not argv:
        return ExecClass.OTHER

    program = _program(argv[0])

    if program in _SHELLS:
        return ExecClass.SHELL_PASSTHROUGH

    family = _interpreter_family(program)
    if family is not None and set(argv[1:]) & _INLINE_CODE_FLAGS[family]:
        return ExecClass.SHELL_PASSTHROUGH

    if program == "find":
        if set(argv[1:]) & _FIND_ACTION_FLAGS:
            return ExecClass.OTHER
        if _operands_escape_workspace(argv[1:]):
            return ExecClass.OTHER
        return ExecClass.SAFE_READ

    normalized = (program, *argv[1:])
    # 先匹配最长前缀，因此 `("git", "status")` 会优先于任意 `("git",)` 条目。
    for length in range(_MAX_PREFIX_LEN, 0, -1):
        if normalized[:length] in _SAFE_PREFIXES:
            # 只有读取始终位于工作区内时才静默放行；指向工作区外的操作数
            # 会像其他 exec 一样需要审批。
            if _operands_escape_workspace(normalized[length:]):
                return ExecClass.OTHER
            return ExecClass.SAFE_READ
    return ExecClass.OTHER
