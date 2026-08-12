"""How the sandbox applies to repo.check, and why it differs from repo.exec.

repo.exec runs model-proposed argv, so the sandbox is the only thing between
the model and the machine and is mandatory. repo.check runs a user-authored
recipe id on a repository the user already trusts, so the sandbox is
defense-in-depth: applied when a backend exists, not a precondition for running
(ADR 0013). These tests pin both halves so the asymmetry cannot drift into an
accident.
"""

from pathlib import Path

from haven.domain import PermissionMode, PolicyDecision, ToolFacts, evaluate_policy
from tests.integration.harness import Harness, default_recipes, finish, make_repo, text, tool


class TestCheckIsWrappedWhenABackendExists:
    async def test_a_check_goes_through_the_launcher(self, tmp_path: Path) -> None:
        turns = [
            [tool("c1", "repo.check", recipe_id="always-pass"), finish("tool_calls")],
            [text("Checked."), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns)
        await h.service.run("Run the check")

        recipe_argv = default_recipes()["always-pass"].argv
        assert any(argv == recipe_argv for argv, _ in h.launcher.calls)


class TestPolicyAsymmetry:
    def test_exec_is_denied_without_a_backend(self) -> None:
        outcome = evaluate_policy(
            PermissionMode.INTERACTIVE,
            ToolFacts(tool_name="repo.exec", exec_class="other", sandbox_available=False),
        )
        assert outcome.decision is PolicyDecision.DENY
        assert outcome.reason_code == "sandbox_unavailable"

    def test_check_is_not_gated_on_the_sandbox(self) -> None:
        """A registered recipe is trusted config, so its policy turns on
        registration, not on backend availability."""
        outcome = evaluate_policy(
            PermissionMode.INTERACTIVE,
            ToolFacts(tool_name="repo.check", recipe_registered=True, sandbox_available=False),
        )
        assert outcome.decision is PolicyDecision.ASK
        assert outcome.reason_code == "check_requires_approval"
