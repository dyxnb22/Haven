"""Classification of a proposed command line.

This decides how much approval friction a command gets, never what it is able
to do: capability is bounded by the OS sandbox the command runs in. A
misclassification therefore costs a skipped prompt, not an escape, which is why
a conservative table of obviously-read-only commands is enough.
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
        return ExecClass.SAFE_READ

    normalized = (program, *argv[1:])
    # Longest prefix first, so ("git","status") wins over any ("git",) entry.
    for length in range(_MAX_PREFIX_LEN, 0, -1):
        if normalized[:length] in _SAFE_PREFIXES:
            return ExecClass.SAFE_READ
    return ExecClass.OTHER
