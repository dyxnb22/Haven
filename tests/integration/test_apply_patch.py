"""repo.apply_patch 经过完整通道：一次审批，一次事务。

多文件变更过去是 N 次顺序编辑——N 次审批、N 次变过期的机会，而且无法审阅整体
变更。补丁工具将完整差异放入一次绑定摘要的审批中，并以原子方式提交（ADR 0019）。
"""

from pathlib import Path

import pytest

from haven.adapters.memory_session import MemorySessionStore
from haven.adapters.workspace_fs import FsWorkspace
from haven.application.approvals import ApprovalResponder
from haven.application.emitter import EventEmitter
from haven.application.state import RunContext
from haven.application.tool_facts import ToolFactsCollector
from haven.application.tool_mutations import MutationToolExecutor
from haven.application.tool_pipeline import ToolPipeline
from haven.contracts.events import ApprovalRequested, ToolCompleted
from haven.contracts.model import ToolCallProposal
from haven.contracts.tools import RepoApplyPatchArgs, RepoEditArgs
from haven.domain.approval import ApprovalRequest
from haven.domain.budget import Budget
from haven.domain.enums import ApprovalDecision, EffectState, PermissionMode, RunStatus, StopReason
from haven.domain.ids import RunId
from haven.ports.workspace import PatchRollbackError
from tests.integration.harness import Harness, finish, make_repo, text, tool


def patch_ops(*ops: dict) -> list[dict]:
    return list(ops)


class TestPatchJourney:
    async def test_multi_file_patch_lands_with_one_approval(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        turns = [
            [tool("c1", "repo.read", path="src/calc.py"), finish("tool_calls")],
            [
                tool(
                    "c2",
                    "repo.apply_patch",
                    operations=patch_ops(
                        {
                            "kind": "edit",
                            "path": "src/calc.py",
                            "old_string": "return a - b  # BUG: should be +",
                            "new_string": "return a + b",
                        },
                        {"kind": "create", "path": "src/util.py", "content": "HELPER = True\n"},
                        {"kind": "move", "src": "README.md", "dest": "docs.md"},
                    ),
                    summary="fix add, add helper, rename readme",
                ),
                finish("tool_calls"),
            ],
            [tool("c3", "repo.diff"), finish("tool_calls")],
            [tool("c4", "repo.check", recipe_id="verify-calc"), finish("tool_calls")],
            [text("Patched."), finish()],
        ]
        h = Harness(repo, turns)
        outcome = await h.service.run("Fix add and reorganize")

        assert outcome.status is RunStatus.SUCCEEDED
        assert "return a + b" in (repo / "src" / "calc.py").read_text()
        assert (repo / "src" / "util.py").read_text() == "HELPER = True\n"
        assert (repo / "docs.md").is_file() and not (repo / "README.md").exists()

        approvals = [
            e for e in h.sink.events_of("approval.requested") if isinstance(e, ApprovalRequested)
        ]
        patch_approvals = [a for a in approvals if a.tool_name == "repo.apply_patch"]
        assert len(patch_approvals) == 1, "the whole patch is one approval"
        # 卡片展示完整的、可供审查的 diff。
        assert "return a + b" in patch_approvals[0].preview
        assert "HELPER" in patch_approvals[0].preview

    async def test_the_result_reports_net_file_effects(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        turns = [
            [tool("c1", "repo.read", path="src/calc.py"), finish("tool_calls")],
            [
                tool(
                    "c2",
                    "repo.apply_patch",
                    operations=patch_ops(
                        {
                            "kind": "edit",
                            "path": "src/calc.py",
                            "old_string": "return a - b  # BUG: should be +",
                            "new_string": "return a + b",
                        },
                    ),
                ),
                finish("tool_calls"),
            ],
            [text("Done (unverified)."), finish()],
        ]
        h = Harness(repo, turns)
        await h.service.run("Fix add")

        completed = [
            e
            for e in h.sink.events_of("tool.completed")
            if isinstance(e, ToolCompleted) and e.tool_name == "repo.apply_patch"
        ]
        assert completed and completed[0].status == "ok"


class TestPatchRefusals:
    async def test_editing_an_unread_file_is_refused(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        turns = [
            [
                tool(
                    "c1",
                    "repo.apply_patch",
                    operations=patch_ops(
                        {
                            "kind": "edit",
                            "path": "src/calc.py",
                            "old_string": "return a - b  # BUG: should be +",
                            "new_string": "return a + b",
                        },
                    ),
                ),
                finish("tool_calls"),
            ],
            [text("Could not."), finish()],
        ]
        h = Harness(repo, turns)
        await h.service.run("Fix add")

        completed = [
            e
            for e in h.sink.events_of("tool.completed")
            if isinstance(e, ToolCompleted) and e.tool_name == "repo.apply_patch"
        ]
        assert completed and completed[0].error_code == "invalid_arguments"
        assert "return a + b" not in (repo / "src" / "calc.py").read_text()

    async def test_a_protected_path_anywhere_denies_the_whole_patch(self, tmp_path: Path) -> None:
        from haven.contracts.events import PolicyDecided

        repo = make_repo(tmp_path)
        turns = [
            [tool("c1", "repo.read", path="src/calc.py"), finish("tool_calls")],
            [
                tool(
                    "c2",
                    "repo.apply_patch",
                    operations=patch_ops(
                        {
                            "kind": "edit",
                            "path": "src/calc.py",
                            "old_string": "return a - b  # BUG: should be +",
                            "new_string": "return a + b",
                        },
                        {"kind": "create", "path": ".haven.toml", "content": "[owned]\n"},
                    ),
                ),
                finish("tool_calls"),
            ],
            [text("Denied, stopping."), finish()],
        ]
        h = Harness(repo, turns)
        await h.service.run("Fix add and take over the config")

        denies = [
            e
            for e in h.sink.events_of("policy.decided")
            if isinstance(e, PolicyDecided) and e.decision == "deny"
        ]
        assert any(d.reason_code == "protected_path" for d in denies)
        assert "return a + b" not in (repo / "src" / "calc.py").read_text(), (
            "one bad op must sink the whole patch, not just itself"
        )

    async def test_read_only_mode_denies_the_patch(self, tmp_path: Path) -> None:
        from haven.contracts.events import PolicyDecided

        repo = make_repo(tmp_path)
        turns = [
            [
                tool(
                    "c1",
                    "repo.apply_patch",
                    operations=patch_ops(
                        {"kind": "create", "path": "note.txt", "content": "hi\n"},
                    ),
                ),
                finish("tool_calls"),
            ],
            [text("Read-only; cannot write."), finish()],
        ]
        h = Harness(repo, turns, mode=PermissionMode.READ_ONLY)
        await h.service.run("Write a note")

        denies = [
            e
            for e in h.sink.events_of("policy.decided")
            if isinstance(e, PolicyDecided) and e.decision == "deny"
        ]
        assert any(d.reason_code == "read_only_mode" for d in denies)
        assert not (repo / "note.txt").exists()


class _TamperingApprover(ApprovalResponder):
    """批准所有内容，但先修改文件——制造 TOCTOU 窗口。"""

    def __init__(self, repo: Path, target: str) -> None:
        self._repo = repo
        self._target = target

    async def respond(self, request: ApprovalRequest) -> ApprovalDecision:
        if request.tool_name == "repo.apply_patch":
            path = self._repo / self._target
            path.write_text(path.read_text() + "# changed while you decided\n")
        return ApprovalDecision.APPROVED


class TestPatchToctou:
    async def test_a_file_changed_during_approval_fails_the_whole_patch_closed(
        self, tmp_path: Path
    ) -> None:
        repo = make_repo(tmp_path)
        turns = [
            [tool("c1", "repo.read", path="src/calc.py"), finish("tool_calls")],
            [
                tool(
                    "c2",
                    "repo.apply_patch",
                    operations=patch_ops(
                        {
                            "kind": "edit",
                            "path": "src/calc.py",
                            "old_string": "return a - b  # BUG: should be +",
                            "new_string": "return a + b",
                        },
                        {"kind": "create", "path": "src/util.py", "content": "HELPER = True\n"},
                    ),
                ),
                finish("tool_calls"),
            ],
            [text("Stale, stopping."), finish()],
        ]
        h = Harness(repo, turns, approver=_TamperingApprover(repo, "src/calc.py"))
        await h.service.run("Fix add")

        completed = [
            e
            for e in h.sink.events_of("tool.completed")
            if isinstance(e, ToolCompleted) and e.tool_name == "repo.apply_patch"
        ]
        assert completed and completed[0].error_code == "stale_preimage"
        assert not (repo / "src" / "util.py").exists(), "nothing may land from a stale patch"


class TestPatchEffectUnknown:
    async def test_failed_compensation_stops_the_run_and_marks_every_effect_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = make_repo(tmp_path)
        original = (repo / "src" / "calc.py").read_text()
        turns = [
            [tool("c1", "repo.read", path="src/calc.py"), finish("tool_calls")],
            [
                tool(
                    "c2",
                    "repo.apply_patch",
                    operations=patch_ops(
                        {
                            "kind": "edit",
                            "path": "src/calc.py",
                            "old_string": "return a - b  # BUG: should be +",
                            "new_string": "return a + b",
                        },
                    ),
                ),
                finish("tool_calls"),
            ],
        ]
        h = Harness(repo, turns)

        async def fail_after_partial_commit(_preview: object) -> None:
            raise PatchRollbackError("commit and compensation both failed")

        monkeypatch.setattr(h.workspace, "apply_patch", fail_after_partial_commit)
        outcome = await h.service.run("Fix add with one atomic patch")

        assert outcome.status is RunStatus.EFFECT_UNKNOWN
        assert outcome.stop_reason is StopReason.EFFECT_UNKNOWN
        executions = await h.store.load_executions(outcome.run_id)
        assert executions and all(
            record.effect_state is EffectState.EFFECT_UNKNOWN for record in executions
        )
        assert (repo / "src" / "calc.py").read_text() == original


class TestPatchPreservesLineEndings:
    """补丁的"运行前内容"必须字节级忠实，否则回滚会悄悄改掉换行风格。

    提交循环原来用 `Path.read_text`，它以通用换行模式打开，把 CRLF 折成 LF。这份
    文本同时喂给回滚和运行 diff（进而喂给 `haven rewind`），折过一次之后，还原
    出来的就是另一个文件。
    """

    @staticmethod
    def _crlf_repo(tmp_path: Path) -> Path:
        repo = make_repo(tmp_path)
        (repo / "src" / "calc.py").write_bytes(
            b"def add(a, b):\r\n    return a - b  # BUG: should be +\r\n"
        )
        return repo

    async def test_the_registered_pre_image_keeps_crlf(self, tmp_path: Path) -> None:
        repo = self._crlf_repo(tmp_path)
        h = Harness(
            repo,
            [
                [tool("c1", "repo.read", path="src/calc.py"), finish("tool_calls")],
                [
                    tool(
                        "c2",
                        "repo.apply_patch",
                        operations=patch_ops(
                            {
                                "kind": "edit",
                                "path": "src/calc.py",
                                "old_string": "return a - b",
                                "new_string": "return a + b",
                            }
                        ),
                    ),
                    finish("tool_calls"),
                ],
                [tool("c3", "repo.diff"), finish("tool_calls")],
                [tool("c4", "repo.check", recipe_id="verify-calc"), finish("tool_calls")],
                [text("Patched."), finish()],
            ],
        )
        outcome = await h.service.run("Fix add with one atomic patch")
        assert outcome.status is RunStatus.SUCCEEDED

        originals = h.workspace.original_contents()
        assert originals["src/calc.py"].text == (
            "def add(a, b):\r\n    return a - b  # BUG: should be +\r\n"
        ), "运行前内容带着 CRLF；haven rewind 才能把它按原样写回去"

    async def test_a_deleted_file_keeps_crlf_in_its_registered_original(
        self, tmp_path: Path
    ) -> None:
        """删除分支登记的是同一份"运行前内容"，`haven rewind` 靠它把文件找回来。"""
        repo = self._crlf_repo(tmp_path)
        (repo / "src" / "util.py").write_bytes(b"def sub(a, b):\r\n    return a - b\r\n")
        h = Harness(
            repo,
            [
                [tool("c1", "repo.read", path="src/calc.py"), finish("tool_calls")],
                [tool("c2", "repo.read", path="src/util.py"), finish("tool_calls")],
                [
                    tool(
                        "c3",
                        "repo.apply_patch",
                        operations=patch_ops(
                            {
                                "kind": "edit",
                                "path": "src/calc.py",
                                "old_string": "return a - b",
                                "new_string": "return a + b",
                            },
                            {"kind": "delete", "path": "src/util.py"},
                        ),
                    ),
                    finish("tool_calls"),
                ],
                [tool("c4", "repo.diff"), finish("tool_calls")],
                [tool("c5", "repo.check", recipe_id="verify-calc"), finish("tool_calls")],
                [text("Removed."), finish()],
            ],
        )
        outcome = await h.service.run("Drop the unused helper")
        assert outcome.status is RunStatus.SUCCEEDED
        assert not (repo / "src" / "util.py").exists()

        originals = h.workspace.original_contents()
        assert originals["src/util.py"].text == "def sub(a, b):\r\n    return a - b\r\n", (
            "被删文件的运行前内容带着 CRLF，rewind 才能按原样恢复它"
        )

    async def test_rollback_restores_crlf_byte_for_byte(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """补丁失败后的回滚也必须是字节级还原。"""
        repo = self._crlf_repo(tmp_path)
        (repo / "src" / "util.py").write_bytes(b"def sub(a, b):\r\n    return a - b\r\n")
        first_bytes = (repo / "src" / "calc.py").read_bytes()
        second_bytes = (repo / "src" / "util.py").read_bytes()
        h = Harness(
            repo,
            [
                [tool("c1", "repo.read", path="src/calc.py"), finish("tool_calls")],
                [tool("c2", "repo.read", path="src/util.py"), finish("tool_calls")],
                [
                    tool(
                        "c3",
                        "repo.apply_patch",
                        operations=patch_ops(
                            {
                                "kind": "edit",
                                "path": "src/calc.py",
                                "old_string": "return a - b",
                                "new_string": "return a + b",
                            },
                            {
                                "kind": "edit",
                                "path": "src/util.py",
                                "old_string": "return a - b",
                                "new_string": "return a + b",
                            },
                        ),
                    ),
                    finish("tool_calls"),
                ],
                [text("Should not get here."), finish()],
            ],
        )

        from haven.adapters import workspace_patch as patch_module

        real_replace = patch_module.os.replace
        calls = {"n": 0}

        def flaky_replace(src: str, dst: str) -> None:
            # 第一次 replace 是暂存文件落位；第二次让它炸，触发回滚。
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("disk fell over mid-commit")
            real_replace(src, dst)

        monkeypatch.setattr(patch_module.os, "replace", flaky_replace)
        await h.service.run("Fix add with one atomic patch")

        completed = [
            e
            for e in h.sink.events_of("tool.completed")
            if isinstance(e, ToolCompleted) and e.tool_name == "repo.apply_patch"
        ]
        assert completed and completed[0].error_code == "internal"
        assert (repo / "src" / "calc.py").read_bytes() == first_bytes, "回滚把换行风格改掉了"
        assert (repo / "src" / "util.py").read_bytes() == second_bytes


class TestAnUnexpectedPatchFailureIsEffectUnknown:
    """补丁在落盘之后炸开时，运行必须停下来，而不是继续写。

    `execute_patch` 把每个文件的效果记在 `f"{call_id}#{index}"` 下，而流水线
    的 `_side_effect_is_undetermined` 此前只拿裸 `call_id` 去比对——于是补丁的
    每一个 EFFECT_UNKNOWN 记录都查不出来，工具结果带着 `effect_unknown=False`
    流回代理循环，而磁盘状态其实已经无法证明。单文件编辑工具一直是对的，所以
    这个缺口只在补丁上出现。
    """

    @staticmethod
    async def _run(root: Path) -> tuple[object, list[tuple[str, str]]]:
        (root / "a.txt").write_text("one\n", encoding="utf-8")
        workspace = FsWorkspace(root)
        store = MemorySessionStore()
        emitter = EventEmitter(store, ())
        ctx = RunContext(
            run_id=RunId("r"), goal="g", mode=PermissionMode.INTERACTIVE, budget=Budget()
        )
        ctx.files_read["a.txt"] = workspace.path_facts("a.txt").digest or ""
        mutations = MutationToolExecutor(workspace=workspace, store=store, emitter=emitter)
        pipeline = ToolPipeline(
            workspace=workspace,
            executor=None,
            store=store,
            emitter=emitter,
            approvals=None,
            registry=None,
            recipes={},
            mode=PermissionMode.INTERACTIVE,
        )
        real_apply = workspace.apply_patch

        async def apply_then_explode(preview, **kwargs):  # type: ignore[no-untyped-def]
            await real_apply(preview, **kwargs)
            raise OSError("disk fell over after the write")

        workspace.apply_patch = apply_then_explode  # type: ignore[method-assign]
        call = ToolCallProposal(call_id="c1", tool_name="repo.apply_patch", arguments_json="{}")
        args = RepoApplyPatchArgs(
            operations=(
                {"kind": "edit", "path": "a.txt", "old_string": "one", "new_string": "two"},
            )
        )
        _, preview = await ToolFactsCollector(workspace, {}, None).collect(ctx, call, args)
        with pytest.raises(OSError, match="disk fell over"):
            await mutations.execute_patch(ctx, call, args, "ticket", preview)
        records = [
            (record.call_id, record.effect_state.value)
            for record in await store.load_executions(ctx.run_id)
        ]
        return await pipeline._side_effect_is_undetermined(ctx.run_id, call.call_id), records

    async def test_the_undetermined_flag_reaches_the_pipeline(self, tmp_path: Path) -> None:
        undetermined, records = await self._run(tmp_path)
        assert records == [("c1#0", "effect_unknown")], records
        assert undetermined is True, (
            "补丁的 EFFECT_UNKNOWN 记录必须能被流水线认出来，否则运行会继续往下写"
        )


class TestAFailureAfterTheWriteIsEffectUnknown:
    """落盘之后的记账失败，也必须把执行记录收口成 EFFECT_UNKNOWN。

    `execute_write` 的 `try/except BaseException` 原本只包住写入本身；
    `emitter.emit`（会 await 每一个 sink）、`ctx.ledger` 的更新和
    `update_execution_state(CONFIRMED)` 都在守卫之外。一次 sink 故障就会让
    "文件已写、记录仍是 STARTED、证据账本没有这一条"的状态流回代理循环，
    而运行会继续往下写。
    """

    @staticmethod
    async def _run(root: Path) -> tuple[list[tuple[str, str]], bool]:
        (root / "a.txt").write_text("one\n", encoding="utf-8")
        workspace = FsWorkspace(root)
        store = MemorySessionStore()
        emitter = EventEmitter(store, ())
        ctx = RunContext(
            run_id=RunId("r"), goal="g", mode=PermissionMode.INTERACTIVE, budget=Budget()
        )
        ctx.files_read["a.txt"] = workspace.path_facts("a.txt").digest or ""
        mutations = MutationToolExecutor(workspace=workspace, store=store, emitter=emitter)
        pipeline = ToolPipeline(
            workspace=workspace,
            executor=None,
            store=store,
            emitter=emitter,
            approvals=None,
            registry=None,
            recipes={},
            mode=PermissionMode.INTERACTIVE,
        )

        # 让证据事件发出失败——这正是守卫之外第一个 await。
        async def explode(*args: object, **kwargs: object) -> None:
            raise OSError("sink is down")

        emitter.emit = explode  # type: ignore[method-assign]
        call = ToolCallProposal(call_id="c1", tool_name="repo.edit", arguments_json="{}")
        args = RepoEditArgs(path="a.txt", old_string="one", new_string="two")
        _, preview = await ToolFactsCollector(workspace, {}, None).collect(ctx, call, args)
        with pytest.raises(OSError, match="sink is down"):
            await mutations.execute_write(ctx, call, args, "ticket", preview)
        records = [
            (record.call_id, record.effect_state.value)
            for record in await store.load_executions(ctx.run_id)
        ]
        return records, await pipeline._side_effect_is_undetermined(ctx.run_id, call.call_id)

    async def test_the_record_is_marked_undetermined(self, tmp_path: Path) -> None:
        records, undetermined = await self._run(tmp_path)
        assert records == [("c1", "effect_unknown")], records
        assert undetermined is True, (
            "写入已落盘而记账失败，必须让流水线看到 effect_unknown 并停下运行"
        )

    async def test_the_file_is_still_on_disk(self, tmp_path: Path) -> None:
        """这个测试不假装写入没发生——它正是为了那个已发生的写入。"""
        await self._run(tmp_path)
        assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "two\n"
