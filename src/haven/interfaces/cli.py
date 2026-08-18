"""Haven CLI：应用组装、稳定命令注册和公共输出适配器。

稳定的退出码：
0 成功 | 2 用法错误 | 3 策略/权限错误 | 4 提供商错误
5 工具错误 | 6 预算耗尽或已停止 | 7 需要恢复
"""

from pathlib import Path

import typer

from haven import __version__
from haven.interfaces.cli_support.common import (
    EXIT_OK,
    EXIT_POLICY,
    EXIT_PROVIDER,
    EXIT_RECOVERY,
    EXIT_STOPPED,
    EXIT_TOOL,
    EXIT_USAGE,
)
from haven.interfaces.cli_support.diagnostic_commands import (
    debug_context,
    doctor,
    verify_provider,
)
from haven.interfaces.cli_support.eval_commands import eval_command
from haven.interfaces.cli_support.recovery_commands import reconcile, resume, rewind
from haven.interfaces.cli_support.run_commands import continue_, run
from haven.interfaces.cli_support.session_commands import (
    export,
    gc,
    replay,
    sessions_list,
    sessions_show,
)
from haven.interfaces.cli_support.sinks import ConsoleSink, JsonlEventSink, NullSink
from haven.interfaces.cli_support.workspace_commands import config_explain, discover, init

__all__ = [
    "ConsoleSink",
    "EXIT_OK",
    "EXIT_POLICY",
    "EXIT_PROVIDER",
    "EXIT_RECOVERY",
    "EXIT_STOPPED",
    "EXIT_TOOL",
    "EXIT_USAGE",
    "JsonlEventSink",
    "NullSink",
    "app",
]

app = typer.Typer(
    name="haven",
    help="Evidence-driven, replayable local TUI Coding Agent.",
    no_args_is_help=False,
)
sessions_app = typer.Typer(help="Inspect stored runs.")
app.add_typer(sessions_app, name="sessions")


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", "-V", help="Show version and exit."),
) -> None:
    """未提供子命令时启动 Haven TUI。"""
    if version:
        typer.echo(f"haven {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        from haven.interfaces.tui.app import HavenApp

        HavenApp(workspace=Path.cwd()).run()


@app.command()
def tui(
    path: Path = typer.Argument(Path("."), help="Workspace directory."),
) -> None:
    """在工作区中启动交互式 TUI。"""
    from haven.interfaces.tui.app import HavenApp

    HavenApp(workspace=path.resolve()).run()


# 注册顺序保持原 CLI 的帮助输出稳定；实现模块不依赖此组装模块。
app.command()(run)
app.command("continue")(continue_)
app.command()(doctor)
sessions_app.command("list")(sessions_list)
sessions_app.command("show")(sessions_show)
app.command()(gc)
app.command()(replay)
app.command()(resume)
app.command()(rewind)
app.command()(reconcile)
app.command()(export)
app.command()(discover)
app.command()(init)
app.command("config")(config_explain)
app.command("debug-context")(debug_context)
app.command("verify-provider")(verify_provider)
app.command("eval")(eval_command)


if __name__ == "__main__":
    app()
