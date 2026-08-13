"""How the sandbox applies to repo.check, and why it differs from repo.exec.

repo.exec runs model-proposed argv, so the sandbox is the only thing between
the model and the machine and is mandatory. repo.check runs a user-authored
recipe id on a repository the user already trusts, so the sandbox is
defense-in-depth: applied when a backend exists, not a precondition for running
(ADR 0013). These tests pin both halves so the asymmetry cannot drift into an
accident.
"""

from pathlib import Path

from haven.contracts.tools import RecipeSpec
from haven.domain import PermissionMode, PolicyDecision, ToolFacts, evaluate_policy
from haven.ports.sandbox import SandboxSpec, default_readable_roots
from tests.integration.harness import Harness, default_recipes, finish, make_repo, text, tool


async def _spec_for(tmp_path: Path, recipe: RecipeSpec) -> SandboxSpec:
    """Run one check and hand back the SandboxSpec the launcher was given."""
    turns = [
        [tool("c1", "repo.check", recipe_id=recipe.id), finish("tool_calls")],
        [text("Checked."), finish()],
    ]
    h = Harness(make_repo(tmp_path), turns, recipes={recipe.id: recipe})
    await h.service.run("Run the check")
    return next(spec for argv, spec in h.launcher.calls if argv == recipe.argv)


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


class TestDeclaredToolchainRoots:
    """ADR 0029: a check may name the dependency cache it needs to read."""

    async def test_a_declared_root_reaches_the_sandbox_spec(self, tmp_path: Path) -> None:
        cache = tmp_path / "m2"
        cache.mkdir()
        recipe = RecipeSpec(id="mvn", argv=("true",), readable_roots=(str(cache),))
        spec = await _spec_for(tmp_path, recipe)

        assert cache.resolve() in spec.extra_readable_roots

    async def test_the_interpreter_prefixes_are_still_granted(self, tmp_path: Path) -> None:
        """A declaration adds to the existing carve-out; it does not replace it,
        or declaring `~/.m2` would break the Python recipe that ran before."""
        recipe = RecipeSpec(id="mvn", argv=("true",), readable_roots=(str(tmp_path),))
        spec = await _spec_for(tmp_path, recipe)

        granted = set(spec.extra_readable_roots)
        assert set(default_readable_roots()) <= granted

    async def test_a_recipe_that_declares_nothing_is_unchanged(self, tmp_path: Path) -> None:
        """The profile for every recipe written before ADR 0029 must be
        byte-identical to what it was."""
        spec = await _spec_for(tmp_path, RecipeSpec(id="plain", argv=("true",)))

        assert spec.extra_readable_roots == default_readable_roots()


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
