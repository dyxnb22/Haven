"""Classification of a proposed command line.

This decides how much approval friction a command gets, never what it is able
to *write*: write capability is bounded by the OS sandbox the command runs in,
so a misclassification there costs a skipped prompt, not an escape.

Reads are the exception, and the reason `SAFE_READ` is narrower than its name
suggests. The sandbox confines writes and hides `$HOME`, but it deliberately
leaves the rest of the filesystem readable so ordinary interpreters start, and
`repo.exec` validates `cwd` — not the paths inside `argv`. An auto-allowed
`cat` of an absolute path therefore reads a file the human never approved, and
its output goes back into the transcript, i.e. to the model provider. On Linux
`/proc/<parent-pid>/environ` even reaches the parent process's environment,
around the child's scrubbed one.

So friction here is calibrated to the *operands*, not only the program: a
read-only command stays silent while it stays inside the workspace, and asks
the moment an operand points outside it.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import PurePosixPath


class ExecClass(StrEnum):
    SAFE_READ = "safe_read"
    SHELL_PASSTHROUGH = "shell_passthrough"
    OTHER = "other"


#: Command prefixes that only observe. Keyed by prefix so a subcommand can be
#: classified separately from its parent (`git status` vs `git push`).
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

#: Interpreters paired with the flags that make them run inline source.
_INLINE_CODE_FLAGS: dict[str, frozenset[str]] = {
    "python": frozenset({"-c"}),
    "node": frozenset({"-e", "--eval", "-p", "--print"}),
    "deno": frozenset({"eval"}),
    "ruby": frozenset({"-e"}),
    "perl": frozenset({"-e", "-E"}),
}

#: `find` walks the tree harmlessly until one of these makes it act.
_FIND_ACTION_FLAGS = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir"})

_MAX_PREFIX_LEN = 2


def _program(argv0: str) -> str:
    """Basename, so /bin/ls and ls classify alike."""
    return PurePosixPath(argv0).name


def _interpreter_family(program: str) -> str | None:
    """Map python3.12 -> python; leave unrelated names alone."""
    for family in _INLINE_CODE_FLAGS:
        if program == family or program.startswith(family):
            return family
    return None


def _operand_escapes_workspace(operand: str) -> bool:
    """Could this operand name something outside the workspace?

    Deliberately conservative and syntactic: an operand that is not a path at
    all (a grep pattern, a git ref, a `-n` value) simply never looks absolute,
    so testing every operand costs nothing. The failure direction is one extra
    approval prompt, never a silent read.
    """
    candidate = operand
    if candidate.startswith("-"):
        # A bare flag names nothing, but `--file=/etc/shadow` hides a path.
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
    """Classify one proposed command. Pure and total."""
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
    # Longest prefix first, so ("git","status") wins over any ("git",) entry.
    for length in range(_MAX_PREFIX_LEN, 0, -1):
        if normalized[:length] in _SAFE_PREFIXES:
            # Silent only while the read stays inside the workspace; an operand
            # pointing outside it is approved like any other exec.
            if _operands_escape_workspace(normalized[length:]):
                return ExecClass.OTHER
            return ExecClass.SAFE_READ
    return ExecClass.OTHER
