"""命令分类：决定审批摩擦，而不是能力。"""

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


class TestOperandsMustStayInTheWorkspace:
    """读取只有在工作区内时才没有摩擦。

    沙箱阻止写入并隐藏 $HOME，但保留其余文件系统的可读性；repo.exec 校验 cwd，
    而不是 argv 中的路径——因此自动允许 `cat /abs/path` 会读取未经审批的文件，
    并将其反馈给模型提供商。在 Linux 上，这还包括 /proc/<parent>/environ，即父
    进程的完整环境，绕过子进程已清理的环境。
    """

    def test_absolute_operands_require_approval(self) -> None:
        for argv in (
            ("cat", "/proc/self/environ"),
            ("cat", "/etc/passwd"),
            ("head", "-n", "1", "/etc/shadow"),
            ("grep", "-r", "secret", "/var"),
            ("tail", "/opt/app/config.yml"),
            ("wc", "-l", "/etc/hosts"),
            ("find", "/etc", "-name", "*.conf"),
        ):
            assert classify_argv(argv) is ExecClass.OTHER, argv

    def test_parent_traversal_requires_approval(self) -> None:
        for argv in (
            ("cat", "../../etc/passwd"),
            ("cat", "../sibling-repo/.env"),
            ("find", "..", "-name", "id_rsa"),
        ):
            assert classify_argv(argv) is ExecClass.OTHER, argv

    def test_home_shorthand_requires_approval(self) -> None:
        assert classify_argv(("cat", "~/.ssh/id_rsa")) is ExecClass.OTHER

    def test_a_path_hidden_behind_a_flag_value_requires_approval(self) -> None:
        assert classify_argv(("grep", "--file=/etc/passwd", "x")) is ExecClass.OTHER

    def test_workspace_relative_reads_stay_friction_free(self) -> None:
        """常见情况不得退化为需要提示。"""
        for argv in (
            ("cat", "README.md"),
            ("cat", "./src/haven/config.py"),
            ("head", "-n", "20", "src/a.py"),
            ("grep", "-rn", "pattern", "src"),
            ("rg", "--json", "needle"),
            ("ls", "-la"),
            ("find", ".", "-name", "*.py"),
            ("git", "log", "--oneline", "-n", "5"),
            ("git", "show", "HEAD~2"),
        ):
            assert classify_argv(argv) is ExecClass.SAFE_READ, argv

    def test_the_program_path_itself_is_not_an_operand(self) -> None:
        """/bin/ls 是程序名称，而不是它读取的内容。"""
        assert classify_argv(("/bin/ls",)) is ExecClass.SAFE_READ
        assert classify_argv(("/usr/bin/cat", "README.md")) is ExecClass.SAFE_READ


class TestArity:
    def test_bare_git_is_not_safe(self) -> None:
        """不得从较短前缀推断出较长前缀。"""
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
        """运行仓库中的脚本是普通工作，不是内联代码。"""
        assert classify_argv(("python", "script.py")) is ExecClass.OTHER


class TestFallback:
    def test_unknown_program_is_other(self) -> None:
        assert classify_argv(("make", "build")) is ExecClass.OTHER

    def test_empty_argv_is_other(self) -> None:
        assert classify_argv(()) is ExecClass.OTHER
