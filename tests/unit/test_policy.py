from haven.domain import (
    EFFECT_TOOLS,
    KNOWN_TOOLS,
    READ_ONLY_TOOLS,
    STATE_TOOLS,
    PermissionMode,
    PolicyDecision,
    ToolFacts,
    evaluate_policy,
)


def facts(**overrides: object) -> ToolFacts:
    base: dict[str, object] = {"tool_name": "repo.read"}
    base.update(overrides)
    return ToolFacts(**base)  # type: ignore[arg-type]


class TestReadOnlyTools:
    def test_read_only_tools_allowed_in_interactive(self) -> None:
        for tool in ("repo.list", "repo.search", "repo.read", "repo.diff"):
            outcome = evaluate_policy(PermissionMode.INTERACTIVE, facts(tool_name=tool))
            assert outcome.decision is PolicyDecision.ALLOW

    def test_read_only_tools_allowed_in_read_only_mode(self) -> None:
        outcome = evaluate_policy(PermissionMode.READ_ONLY, facts(tool_name="repo.search"))
        assert outcome.decision is PolicyDecision.ALLOW


class TestStateTools:
    def test_plan_allowed_in_both_modes(self) -> None:
        """task.plan touches only run state, so read_only mode may still use it."""
        for mode in (PermissionMode.INTERACTIVE, PermissionMode.READ_ONLY):
            outcome = evaluate_policy(mode, facts(tool_name="task.plan"))
            assert outcome.decision is PolicyDecision.ALLOW
            assert outcome.reason_code == "state_tool"


class TestEffectTools:
    def test_edit_requires_approval_in_interactive(self) -> None:
        outcome = evaluate_policy(PermissionMode.INTERACTIVE, facts(tool_name="repo.edit"))
        assert outcome.decision is PolicyDecision.ASK
        assert outcome.reason_code == "write_requires_approval"

    def test_edit_denied_in_read_only_mode(self) -> None:
        outcome = evaluate_policy(PermissionMode.READ_ONLY, facts(tool_name="repo.edit"))
        assert outcome.decision is PolicyDecision.DENY
        assert outcome.reason_code == "read_only_mode"

    def test_create_requires_approval_in_interactive(self) -> None:
        outcome = evaluate_policy(PermissionMode.INTERACTIVE, facts(tool_name="repo.create"))
        assert outcome.decision is PolicyDecision.ASK
        assert outcome.reason_code == "create_requires_approval"

    def test_create_denied_in_read_only_mode(self) -> None:
        outcome = evaluate_policy(PermissionMode.READ_ONLY, facts(tool_name="repo.create"))
        assert outcome.decision is PolicyDecision.DENY
        assert outcome.reason_code == "read_only_mode"

    def test_create_outside_workspace_denied(self) -> None:
        outcome = evaluate_policy(
            PermissionMode.INTERACTIVE, facts(tool_name="repo.create", within_workspace=False)
        )
        assert outcome.decision is PolicyDecision.DENY
        assert outcome.reason_code == "outside_workspace"

    def test_create_on_protected_path_denied(self) -> None:
        outcome = evaluate_policy(
            PermissionMode.INTERACTIVE,
            facts(tool_name="repo.create", touches_protected_path=True),
        )
        assert outcome.decision is PolicyDecision.DENY
        assert outcome.reason_code == "protected_path"

    def test_check_with_registered_recipe_asks(self) -> None:
        outcome = evaluate_policy(
            PermissionMode.INTERACTIVE, facts(tool_name="repo.check", recipe_registered=True)
        )
        assert outcome.decision is PolicyDecision.ASK

    def test_check_with_unregistered_recipe_denied(self) -> None:
        outcome = evaluate_policy(
            PermissionMode.INTERACTIVE, facts(tool_name="repo.check", recipe_registered=False)
        )
        assert outcome.decision is PolicyDecision.DENY
        assert outcome.reason_code == "unregistered_recipe"


class TestHardDenies:
    def test_outside_workspace_denied_even_for_read(self) -> None:
        outcome = evaluate_policy(
            PermissionMode.INTERACTIVE, facts(tool_name="repo.read", within_workspace=False)
        )
        assert outcome.decision is PolicyDecision.DENY
        assert outcome.reason_code == "outside_workspace"

    def test_protected_path_denied(self) -> None:
        outcome = evaluate_policy(
            PermissionMode.INTERACTIVE,
            facts(tool_name="repo.edit", touches_protected_path=True),
        )
        assert outcome.decision is PolicyDecision.DENY
        assert outcome.reason_code == "protected_path"

    def test_unknown_tool_denied(self) -> None:
        outcome = evaluate_policy(PermissionMode.INTERACTIVE, facts(tool_name="shell.exec"))
        assert outcome.decision is PolicyDecision.DENY
        assert outcome.reason_code == "unknown_tool"


class TestPolicyCompleteness:
    def test_every_registered_tool_has_a_policy(self) -> None:
        """A new tool must be classified, not silently fall through to ASK."""
        from haven.contracts.tools import ARGS_MODELS

        assert set(ARGS_MODELS) == KNOWN_TOOLS

    def test_tool_categories_are_disjoint(self) -> None:
        assert not (READ_ONLY_TOOLS & EFFECT_TOOLS)
        assert not (READ_ONLY_TOOLS & STATE_TOOLS)
        assert not (EFFECT_TOOLS & STATE_TOOLS)

    def test_no_effect_tool_is_ever_auto_allowed(self) -> None:
        for tool in EFFECT_TOOLS:
            for mode in (PermissionMode.INTERACTIVE, PermissionMode.READ_ONLY):
                outcome = evaluate_policy(mode, facts(tool_name=tool, recipe_registered=True))
                assert outcome.decision is not PolicyDecision.ALLOW, tool
