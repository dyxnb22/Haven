"""The repo.exec argument contract: argv only, bounded, no shell string."""

import pytest
from pydantic import ValidationError

from haven.application.registry import ToolRegistry, ValidationFailure
from haven.contracts.tools import ARGS_MODELS, TOOL_VERSION, RepoExecArgs, tool_schemas


class TestRegistration:
    def test_exec_is_registered(self) -> None:
        assert ARGS_MODELS["repo.exec"] is RepoExecArgs

    def test_tool_version_reflects_the_changed_tool_set(self) -> None:
        assert TOOL_VERSION == "2"

    def test_schema_is_published_to_the_model(self) -> None:
        assert "repo.exec" in {schema.name for schema in tool_schemas()}

    def test_description_directs_verification_to_repo_check(self) -> None:
        schema = next(s for s in tool_schemas() if s.name == "repo.exec")
        assert "repo.check" in schema.description


class TestValidation:
    def test_argv_is_required_and_non_empty(self) -> None:
        with pytest.raises(ValidationError):
            RepoExecArgs(argv=())

    def test_defaults(self) -> None:
        args = RepoExecArgs(argv=("ls",))
        assert args.cwd == "."
        assert args.timeout_seconds == 60.0
        assert args.summary == ""

    def test_timeout_is_bounded(self) -> None:
        with pytest.raises(ValidationError):
            RepoExecArgs(argv=("ls",), timeout_seconds=1000.0)
        with pytest.raises(ValidationError):
            RepoExecArgs(argv=("ls",), timeout_seconds=0.0)

    def test_argv_length_is_bounded(self) -> None:
        with pytest.raises(ValidationError):
            RepoExecArgs(argv=tuple(str(i) for i in range(65)))

    def test_each_item_is_length_capped(self) -> None:
        with pytest.raises(ValidationError):
            RepoExecArgs(argv=("ls", "x" * 5000))

    def test_a_command_string_is_not_accepted_as_argv(self) -> None:
        """The model must not be able to smuggle a shell line into one item."""
        failure = ToolRegistry().validate("repo.exec", '{"argv": "rm -rf / && echo pwned"}')
        assert isinstance(failure, ValidationFailure)
        assert failure.code == "invalid_arguments"

    def test_registry_accepts_a_json_array(self) -> None:
        args = ToolRegistry().validate("repo.exec", '{"argv": ["ls", "-la"]}')
        assert isinstance(args, RepoExecArgs)
        assert args.argv == ("ls", "-la")
