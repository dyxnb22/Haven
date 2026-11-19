"""终态运行必须释放 emitter 的每 run 状态；暂停的运行不能。

`EventEmitter` 的 `_last_seq` 与 `_locks` 此前从不删除：一次运行漏一个 int 加一个
`asyncio.Lock`，直到进程退出。长期存活的界面进程（TUI）会在一个会话里跑很多次运行，
所以这不是理论问题。释放点必须落在唯一的终态出口 `RunFinalizer.finish` 上——早一步
（暂停时）会让之后用同一个 run_id 继续发事件的恢复流程失去锁。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from haven.contracts.user_input import UserInputResponse
from haven.domain.enums import RunStatus
from tests.integration.harness import Harness, finish, make_repo, text, tool


@pytest.mark.asyncio
async def test_a_finished_run_releases_the_emitters_per_run_state(tmp_path: Path) -> None:
    h = Harness(make_repo(tmp_path), [[text("done"), finish()]])
    outcome = await h.service.run("Explain something")

    assert outcome.status is RunStatus.SUCCEEDED
    # 这次运行确实发过事件（否则后面的断言是空转）
    assert await h.store.load_events(outcome.run_id)
    # 终态之后必须释放，否则一个会话里每次运行都漏一份
    assert outcome.run_id not in h.emitter._last_seq
    assert outcome.run_id not in h.emitter._locks


@pytest.mark.asyncio
async def test_a_paused_run_keeps_its_state_so_it_can_be_answered(tmp_path: Path) -> None:
    """暂停不是终态：`answer_run` 之后还会用同一个 run_id 继续发事件。"""
    h = Harness(
        make_repo(tmp_path),
        [
            [
                tool(
                    "ask-1",
                    "user.ask",
                    questions=[
                        {
                            "id": "scope",
                            "question": "Continue verification?",
                            "options": ["Parser", "CLI"],
                        }
                    ],
                ),
                finish("tool_calls"),
            ]
        ],
    )
    paused = await h.service.run("Ask the user something")

    assert paused.status is RunStatus.WAITING_USER
    assert paused.pending_input is not None
    # 状态必须留着：马上回答它时还要用同一把锁发事件
    assert paused.run_id in h.emitter._last_seq
    assert h.emitter.last_seq(paused.run_id) > 0

    resumed = await h.service.answer_run(
        paused.run_id,
        UserInputResponse(request_id=paused.pending_input.request_id, answers={"scope": "Parser"}),
    )
    # 回答之后这次运行才真正终了，状态随之释放
    assert resumed.status in (RunStatus.SUCCEEDED, RunStatus.FAILED)
    assert paused.run_id not in h.emitter._last_seq
