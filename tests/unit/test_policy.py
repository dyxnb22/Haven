from haven.domain import (
    EFFECT_TOOLS,
    EXEC_TOOLS,
    KNOWN_TOOLS,
    READ_ONLY_TOOLS,
    STATE_TOOLS,
    PermissionMode,
    PolicyDecision,
    RiskLevel,
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


class TestExecTool:
    def exec_facts(self, **overrides: object) -> ToolFacts:
        base: dict[str, object] = {
            "tool_name": "repo.exec",
            "exec_class": "other",
            "sandbox_available": True,
        }
        base.update(overrides)
        return ToolFacts(**base)  # type: ignore[arg-type]

    def test_denied_when_no_sandbox_backend(self) -> None:
        """Fail closed: there is no unsandboxed fallback."""
        outcome = evaluate_policy(
            PermissionMode.INTERACTIVE, self.exec_facts(sandbox_available=False)
        )
        assert outcome.decision is PolicyDecision.DENY
        assert outcome.reason_code == "sandbox_unavailable"

    def test_missing_sandbox_fact_fails_closed(self) -> None:
        """An un-collected fact must never read as permission."""
        outcome = evaluate_policy(
            PermissionMode.INTERACTIVE, self.exec_facts(sandbox_available=None)
        )
        assert outcome.decision is PolicyDecision.DENY
        assert outcome.reason_code == "sandbox_unavailable"

    def test_safe_read_command_is_allowed(self) -> None:
        outcome = evaluate_policy(
            PermissionMode.INTERACTIVE, self.exec_facts(exec_class="safe_read")
        )
        assert outcome.decision is PolicyDecision.ALLOW
        assert outcome.reason_code == "safe_read_exec"

    def test_other_command_requires_approval(self) -> None:
        outcome = evaluate_policy(PermissionMode.INTERACTIVE, self.exec_facts())
        assert outcome.decision is PolicyDecision.ASK
        assert outcome.reason_code == "exec_requires_approval"

    def test_shell_passthrough_asks_with_high_risk(self) -> None:
        outcome = evaluate_policy(
            PermissionMode.INTERACTIVE, self.exec_facts(exec_class="shell_passthrough")
        )
        assert outcome.decision is PolicyDecision.ASK
        assert outcome.reason_code == "shell_passthrough_requires_approval"
        assert outcome.risk is RiskLevel.HIGH

    def test_denied_in_read_only_mode_even_when_safe(self) -> None:
        outcome = evaluate_policy(PermissionMode.READ_ONLY, self.exec_facts(exec_class="safe_read"))
        assert outcome.decision is PolicyDecision.DENY
        assert outcome.reason_code == "read_only_mode"

    def test_cwd_outside_workspace_denied_before_classification(self) -> None:
        outcome = evaluate_policy(
            PermissionMode.INTERACTIVE,
            self.exec_facts(exec_class="safe_read", within_workspace=False),
        )
        assert outcome.decision is PolicyDecision.DENY
        assert outcome.reason_code == "outside_workspace"

    def test_protected_cwd_denied(self) -> None:
        outcome = evaluate_policy(
            PermissionMode.INTERACTIVE,
            self.exec_facts(exec_class="safe_read", touches_protected_path=True),
        )
        assert outcome.decision is PolicyDecision.DENY
        assert outcome.reason_code == "protected_path"


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
        groups = (READ_ONLY_TOOLS, EFFECT_TOOLS, STATE_TOOLS, EXEC_TOOLS)
        for index, left in enumerate(groups):
            for right in groups[index + 1 :]:
                assert not (left & right)

    def test_no_effect_tool_is_ever_auto_allowed(self) -> None:
        for tool in EFFECT_TOOLS:
            for mode in (PermissionMode.INTERACTIVE, PermissionMode.READ_ONLY):
                outcome = evaluate_policy(mode, facts(tool_name=tool, recipe_registered=True))
                assert outcome.decision is not PolicyDecision.ALLOW, tool

    def test_exec_is_auto_allowed_only_for_classified_read_only_commands(self) -> None:
        """The single auto-allow exception, pinned so it cannot widen silently."""
        allowed = [
            exec_class
            for exec_class in ("safe_read", "shell_passthrough", "other")
            if evaluate_policy(
                PermissionMode.INTERACTIVE,
                ToolFacts(tool_name="repo.exec", exec_class=exec_class, sandbox_available=True),
            ).decision
            is PolicyDecision.ALLOW
        ]
        assert allowed == ["safe_read"]
