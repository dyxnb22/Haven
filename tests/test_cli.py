from typer.testing import CliRunner

from haven import __version__
from haven.interfaces.cli import app

runner = CliRunner()


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_default_invocation() -> None:
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "not implemented yet" in result.stdout
