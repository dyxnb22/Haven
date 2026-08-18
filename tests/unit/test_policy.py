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
        """task.plan 只接触运行状态，因此 read_only 模式仍可以使用它。"""
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

    def test_delete_requires_approval_and_is_denied_read_only(self) -> None:
        ask = evaluate_policy(PermissionMode.INTERACTIVE, facts(tool_name="repo.delete"))
        assert ask.decision is PolicyDecision.ASK
        assert ask.reason_code == "delete_requires_approval"
        deny = evaluate_policy(PermissionMode.READ_ONLY, facts(tool_name="repo.delete"))
        assert deny.decision is PolicyDecision.DENY
        assert deny.reason_code == "read_only_mode"

    def test_move_requires_approval_and_is_denied_read_only(self) -> None:
        ask = evaluate_policy(PermissionMode.INTERACTIVE, facts(tool_name="repo.move"))
        assert ask.decision is PolicyDecision.ASK
        assert ask.reason_code == "move_requires_approval"
        deny = evaluate_policy(PermissionMode.READ_ONLY, facts(tool_name="repo.move"))
        assert deny.decision is PolicyDecision.DENY

    def test_delete_on_protected_path_denied(self) -> None:
        outcome = evaluate_policy(
            PermissionMode.INTERACTIVE,
            facts(tool_name="repo.delete", touches_protected_path=True),
        )
        assert outcome.decision is PolicyDecision.DENY
        assert outcome.reason_code == "protected_path"

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
        """失败即关闭：不存在无沙箱的回退路径。"""
        outcome = evaluate_policy(
            PermissionMode.INTERACTIVE, self.exec_facts(sandbox_available=False)
        )
        assert outcome.decision is PolicyDecision.DENY
        assert outcome.reason_code == "sandbox_unavailable"

    def test_missing_sandbox_fact_fails_closed(self) -> None:
        """未收集到的事实绝不能被理解为拥有权限。"""
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
        """新工具必须被分类，不能静默落入 ASK。"""
        from haven.contracts.tools import ARGS_MODELS

        assert set(ARGS_MODELS) == KNOWN_TOOLS

    def test_every_registered_tool_is_fully_wired_in_the_pipeline(self) -> None:
        """流水线的事实表和执行分发表都必须准确覆盖已注册工具集合。结合上面的策略
        检查，这固定了添加工具的四个位置（参数模型、策略分类、事实处理器、执行
        处理器）：遗漏任意一个都会在这里失败，而不是运行时静默落空。"""
        from typing import Any, cast

        from haven.application.registry import ToolRegistry
        from haven.application.tool_pipeline import ToolPipeline
        from haven.contracts.tools import ARGS_MODELS

        pipeline = ToolPipeline(
            workspace=cast(Any, None),
            executor=cast(Any, None),
            store=cast(Any, None),
            emitter=cast(Any, None),
            approvals=cast(Any, None),
            registry=ToolRegistry(),
            recipes={},
            mode=PermissionMode.INTERACTIVE,
        )
        assert set(pipeline._facts_handlers) == set(ARGS_MODELS)  # noqa: SLF001
        assert set(pipeline._execute_handlers) == set(ARGS_MODELS)  # noqa: SLF001

    def test_every_ask_tool_has_an_approval_card(self) -> None:
        """策略要求交给人类的工具必须渲染审批卡片。没有这项保证，新 ASK 工具会带着
        空摘要进入审批模态框，人类会被要求授权一行空白内容。"""
        from typing import Any, cast

        from haven.application.registry import ToolRegistry
        from haven.application.tool_pipeline import ToolPipeline

        pipeline = ToolPipeline(
            workspace=cast(Any, None),
            executor=cast(Any, None),
            store=cast(Any, None),
            emitter=cast(Any, None),
            approvals=cast(Any, None),
            registry=ToolRegistry(),
            recipes={},
            mode=PermissionMode.INTERACTIVE,
        )
        askable = EFFECT_TOOLS | EXEC_TOOLS
        assert set(pipeline._card_handlers) == askable  # noqa: SLF001

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
        """唯一的自动允许例外，固定其范围以防悄悄扩大。"""
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
