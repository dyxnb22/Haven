"""标识符的形状契约：前缀、长度、不碰撞。

`new_run_id` / `new_approval_id` 的前缀此前无人把守——把它们整个删掉，1574 条测试
依然全绿（变异测试实测）。前缀不只是好看：`search_runs` 用 `id NOT LIKE 'child-%'`
把子运行从列表里滤掉，而 `run-` 前缀正是"一个运行 id 永远不会长得像子运行"的保证。
"""

from haven.domain.ids import new_approval_id, new_run_id


class TestRunId:
    def test_has_the_documented_prefix_and_length(self) -> None:
        run_id = new_run_id()
        assert run_id.startswith("run-")
        assert len(run_id) == len("run-") + 12

    def test_is_never_mistaken_for_a_child_run(self) -> None:
        """`search_runs` 靠 `child-%` 前缀过滤子运行。

        若运行 id 也以 `child-` 开头，它就会从列表里悄悄消失——而列表是用户和
        `haven gc` 看待历史的唯一入口。
        """
        for _ in range(50):
            assert not new_run_id().startswith("child-")

    def test_two_calls_do_not_collide(self) -> None:
        assert len({new_run_id() for _ in range(200)}) == 200


class TestApprovalId:
    def test_has_the_documented_prefix_and_length(self) -> None:
        approval_id = new_approval_id()
        assert approval_id.startswith("apr-")
        assert len(approval_id) == len("apr-") + 12

    def test_two_calls_do_not_collide(self) -> None:
        assert len({new_approval_id() for _ in range(200)}) == 200


def test_the_two_id_spaces_do_not_overlap() -> None:
    """审批 id 与运行 id 混用会让一次性凭证看起来像一条运行记录。"""
    assert new_run_id() != new_approval_id()
