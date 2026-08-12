"""Command classification: decides approval friction, never capability."""

from haven.domain.exec_policy import ExecClass, classify_argv


class TestSafeRead:
    def test_read_only_commands_are_safe(self) -> None:
        for argv in (
            ("ls", "-la"),
            ("cat", "README.md"),
            ("head", "-n", "5", "a.txt"),
            ("tail", "a.txt"),
            ("wc", "-l", "a.txt"),
            ("rg", "pattern"),
            ("grep", "-r", "pattern", "."),
        ):
            assert classify_argv(argv) is ExecClass.SAFE_READ, argv

    def test_read_only_git_subcommands_are_safe(self) -> None:
        for argv in (
            ("git", "status"),
            ("git", "log", "--oneline"),
            ("git", "diff", "HEAD"),
            ("git", "show", "abc123"),
        ):
            assert classify_argv(argv) is ExecClass.SAFE_READ, argv

    def test_absolute_paths_are_classified_by_basename(self) -> None:
        assert classify_argv(("/bin/ls",)) is ExecClass.SAFE_READ


class TestArity:
    def test_bare_git_is_not_safe(self) -> None:
        """A longer prefix must not be inferred from a shorter one."""
        assert classify_argv(("git",)) is ExecClass.OTHER

    def test_writing_git_subcommands_are_not_safe(self) -> None:
        for argv in (("git", "push"), ("git", "commit", "-m", "x"), ("git", "clean", "-fd")):
            assert classify_argv(argv) is ExecClass.OTHER, argv

    def test_find_is_safe_without_action_flags(self) -> None:
        assert classify_argv(("find", ".", "-name", "*.py")) is ExecClass.SAFE_READ

    def test_find_with_action_flags_is_not_safe(self) -> None:
        for flag in ("-delete", "-exec", "-execdir", "-ok", "-okdir"):
            assert classify_argv(("find", ".", flag)) is ExecClass.OTHER, flag


class TestShellPassthrough:
    def test_shells_are_passthrough(self) -> None:
        for shell in ("sh", "bash", "zsh", "dash", "ksh", "fish"):
            assert classify_argv((shell, "-c", "echo hi")) is ExecClass.SHELL_PASSTHROUGH, shell

    def test_shell_by_absolute_path_is_passthrough(self) -> None:
        assert classify_argv(("/bin/bash", "-c", "x")) is ExecClass.SHELL_PASSTHROUGH

    def test_interpreters_with_inline_code_are_passthrough(self) -> None:
        for argv in (
            ("python", "-c", "import os"),
            ("python3.12", "-c", "import os"),
            ("node", "-e", "1"),
            ("ruby", "-e", "1"),
            ("perl", "-e", "1"),
            ("deno", "eval", "1"),
        ):
            assert classify_argv(argv) is ExecClass.SHELL_PASSTHROUGH, argv

    def test_interpreter_running_a_file_is_not_passthrough(self) -> None:
        """Running a script from the repo is ordinary work, not inline code."""
        assert classify_argv(("python", "script.py")) is ExecClass.OTHER


class TestFallback:
    def test_unknown_program_is_other(self) -> None:
        assert classify_argv(("make", "build")) is ExecClass.OTHER

    def test_empty_argv_is_other(self) -> None:
        assert classify_argv(()) is ExecClass.OTHER
