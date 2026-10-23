"""集成测试：双轨混合模型路由器（Fast-Explorer + Deep-Coder）执行旅程（ADR 0042）。"""

from __future__ import annotations

import sys
from pathlib import Path

from haven.adapters.providers.scripted import ScriptedModel
from haven.application.approvals import AutoApprover
from haven.application.model_router import ModelRouter
from haven.bootstrap import build_services
from haven.domain.enums import PermissionMode, RunStatus
from tests.integration.harness import CollectingSink, finish, make_repo, text, tool, usage


class TestModelRouterJourney:
    async def test_dual_track_routing_fast_then_deep(self, tmp_path: Path) -> None:
        """读操作由 fast_model 服务；一旦发生写操作，自动切换到 deep_model。"""
        repo = make_repo(tmp_path)
        (repo / ".haven.toml").write_text(
            f'[recipes.verify-calc]\nargv = ["{sys.executable}", "verify_calc.py"]\n'
        )

        # Fast explorer 负责前期只读探索（2 步）
        fast = ScriptedModel(
            turns=[
                # Step 1: 搜索文件
                [
                    tool("c1", "repo.search", pattern="BUG", path="."),
                    finish("tool_calls"),
                    usage(40, 10),
                ],
                # Step 2: 读取文件内容
                [tool("c2", "repo.read", path="src/calc.py"), finish("tool_calls"), usage(30, 10)],
            ],
            name="fast-explorer-v3",
        )

        # Deep reasoner 负责后续代码修改、差分、自检与终答（4 步）
        deep = ScriptedModel(
            turns=[
                # Step 3: 编辑代码
                [
                    tool(
                        "c3",
                        "repo.edit",
                        path="src/calc.py",
                        old_string="return a - b  # BUG: should be +",
                        new_string="return a + b",
                        summary="fix add() to addition",
                    ),
                    finish("tool_calls"),
                    usage(100, 20),
                ],
                # Step 4: 查看 diff
                [tool("c4", "repo.diff"), finish("tool_calls"), usage(80, 15)],
                # Step 5: 运行 check
                [
                    tool("c5", "repo.check", recipe_id="verify-calc"),
                    finish("tool_calls"),
                    usage(70, 15),
                ],
                # Step 6: 终答
                [text("Bug fixed and verified successfully."), finish("stop"), usage(50, 10)],
            ],
            name="deep-coder-r1",
        )

        sink = CollectingSink()
        services = await build_services(
            repo,
            mode=PermissionMode.INTERACTIVE,
            approvals=AutoApprover("approve_all"),
            sinks=[sink],
            fast_model=fast,
            deep_model=deep,
            fast_turns=2,
            store_path=tmp_path / "haven.db",
        )

        try:
            assert isinstance(services.model, ModelRouter)
            outcome = await services.run_service.run("Fix the bug in add()")
            assert outcome.status == RunStatus.SUCCEEDED

            # Fast explorer 被调用了 2 次（步骤 1, 2）
            assert len(fast.requests_seen) == 2
            # Deep coder 被调用了 4 次（步骤 3, 4, 5, 6）
            assert len(deep.requests_seen) == 4

            # 验证代码已被修复
            assert "return a + b" in (repo / "src" / "calc.py").read_text()
        finally:
            await services.close()

    async def test_always_deep_configuration(self, tmp_path: Path) -> None:
        """当 always_deep 开启时，即使未发生任何写操作，所有轮次也全量由 deep_model 处理。"""
        repo = make_repo(tmp_path)

        fast = ScriptedModel(turns=[], name="fast-explorer")
        deep = ScriptedModel(
            turns=[
                [
                    text("The file calc.py contains add and sub functions."),
                    finish("stop"),
                    usage(30, 10),
                ],
            ],
            name="deep-coder",
        )

        sink = CollectingSink()
        router = ModelRouter(fast_model=fast, deep_model=deep, always_deep=True)
        services = await build_services(
            repo,
            mode=PermissionMode.READ_ONLY,
            approvals=AutoApprover("reject_all"),
            sinks=[sink],
            model=router,
            store_path=tmp_path / "always_deep.db",
        )

        try:
            outcome = await services.run_service.run("Explain what calc.py does")
            assert outcome.status == RunStatus.SUCCEEDED
            assert len(fast.requests_seen) == 0
            assert len(deep.requests_seen) == 1
        finally:
            await services.close()
