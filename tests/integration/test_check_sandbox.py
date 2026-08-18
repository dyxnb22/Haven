"""沙箱如何应用于 repo.check，以及它为何不同于 repo.exec。

repo.exec 运行模型提议的 argv，因此沙箱是模型与机器之间的唯一屏障，必须存在。
repo.check 在用户已经信任的仓库上运行用户编写的配方 id，因此沙箱属于纵深防御：
后端存在时应用，但不是运行的前置条件（ADR 0013）。这些测试固定两部分行为，
避免这种不对称意外漂移。
"""

from pathlib import Path

from haven.contracts.tools import RecipeSpec
from haven.domain import PermissionMode, PolicyDecision, ToolFacts, evaluate_policy
from haven.ports.sandbox import SandboxSpec, default_readable_roots
from tests.integration.harness import Harness, default_recipes, finish, make_repo, text, tool


async def _spec_for(tmp_path: Path, recipe: RecipeSpec) -> SandboxSpec:
    """运行一次检查，并返回传给启动器的 SandboxSpec。"""
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
    """ADR 0029：检查可以声明它需要读取的依赖缓存。"""

    async def test_a_declared_root_reaches_the_sandbox_spec(self, tmp_path: Path) -> None:
        cache = tmp_path / "m2"
        cache.mkdir()
        recipe = RecipeSpec(id="mvn", argv=("true",), readable_roots=(str(cache),))
        spec = await _spec_for(tmp_path, recipe)

        assert cache.resolve() in spec.extra_readable_roots

    async def test_the_interpreter_prefixes_are_still_granted(self, tmp_path: Path) -> None:
        """声明会追加到现有例外范围，而不是替换它；否则声明 `~/.m2` 会破坏此前运行
        的 Python 配方。"""
        recipe = RecipeSpec(id="mvn", argv=("true",), readable_roots=(str(tmp_path),))
        spec = await _spec_for(tmp_path, recipe)

        granted = set(spec.extra_readable_roots)
        assert set(default_readable_roots()) <= granted

    async def test_a_recipe_that_declares_nothing_is_unchanged(self, tmp_path: Path) -> None:
        """ADR 0029 之前写入的每个配方的配置必须与原来逐字节一致。"""
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
        """已注册配方属于可信配置，因此策略依据是否注册，而不是依据后端是否可用。"""
        outcome = evaluate_policy(
            PermissionMode.INTERACTIVE,
            ToolFacts(tool_name="repo.check", recipe_registered=True, sandbox_available=False),
        )
        assert outcome.decision is PolicyDecision.ASK
        assert outcome.reason_code == "check_requires_approval"
