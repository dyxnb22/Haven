"""Unit tests for ephemeral reproducer recipe proposal (ADR 0043)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from haven.application.approval_cards import ApprovalCardRenderer
from haven.contracts.tools import RecipeProposeArgs
from haven.domain.enums import PermissionMode, PolicyDecision, RiskLevel
from haven.domain.policy import ToolFacts, evaluate_policy


class TestRecipeProposeSchema:
    def test_valid_args(self) -> None:
        args = RecipeProposeArgs(
            recipe_id="repro-bug-1",
            argv=("python", "test_bug.py"),
            timeout_seconds=30.0,
            description="reproduces zero division",
        )
        assert args.recipe_id == "repro-bug-1"
        assert args.argv == ("python", "test_bug.py")
        assert args.timeout_seconds == 30.0
        assert args.description == "reproduces zero division"

    def test_invalid_recipe_id_chars(self) -> None:
        with pytest.raises(ValidationError):
            RecipeProposeArgs(recipe_id="repro bug with spaces!", argv=("python", "test.py"))

    def test_empty_argv_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RecipeProposeArgs(recipe_id="repro", argv=())

    def test_timeout_bounds(self) -> None:
        with pytest.raises(ValidationError):
            RecipeProposeArgs(recipe_id="repro", argv=("python", "test.py"), timeout_seconds=0.0)
        with pytest.raises(ValidationError):
            RecipeProposeArgs(recipe_id="repro", argv=("python", "test.py"), timeout_seconds=1000.0)


class TestRecipeProposePolicy:
    def test_read_only_mode_denied(self) -> None:
        facts = ToolFacts(tool_name="recipe.propose", within_workspace=True)
        outcome = evaluate_policy(PermissionMode.READ_ONLY, facts)
        assert outcome.decision is PolicyDecision.DENY
        assert outcome.reason_code == "read_only_mode"

    def test_outside_workspace_denied(self) -> None:
        facts = ToolFacts(tool_name="recipe.propose", within_workspace=False)
        outcome = evaluate_policy(PermissionMode.INTERACTIVE, facts)
        assert outcome.decision is PolicyDecision.DENY
        assert outcome.reason_code == "outside_workspace"

    def test_protected_path_denied(self) -> None:
        facts = ToolFacts(
            tool_name="recipe.propose", within_workspace=True, touches_protected_path=True
        )
        outcome = evaluate_policy(PermissionMode.INTERACTIVE, facts)
        assert outcome.decision is PolicyDecision.DENY
        assert outcome.reason_code == "protected_path"

    def test_interactive_mode_asks_with_high_risk(self) -> None:
        facts = ToolFacts(tool_name="recipe.propose", within_workspace=True, sandbox_available=True)
        outcome = evaluate_policy(PermissionMode.INTERACTIVE, facts)
        assert outcome.decision is PolicyDecision.ASK
        assert outcome.reason_code == "propose_recipe_requires_approval"
        assert outcome.risk is RiskLevel.HIGH


class TestApprovalCardRendering:
    def test_card_rendering_contains_command_and_id(self) -> None:
        renderer = ApprovalCardRenderer(recipes={}, sandbox_description=lambda: "sandboxed")
        args = RecipeProposeArgs(
            recipe_id="test-repro",
            argv=("python", "reproduce.py", "--fast"),
            timeout_seconds=45.0,
            description="asserts bug reproduction",
        )
        summary, preview = renderer.render("recipe.propose", args, None)
        assert "test-repro" in summary
        assert "reproduce.py" in preview
        assert "45.0s" in preview
        assert "asserts bug reproduction" in preview
