"""一个 ADR 被另一个 ADR 推翻时，必须指向其修正记录。

审计时发现：ADR 0009 认为错误分类 exec 命令“只会漏掉提示，不会造成逃逸”。
ADR 0026 彻底反驳了这一前提，但 0009 没有说明。只阅读 0009 的人，无论人类还是
代理，都会得到错误的安全模型，因为修正内容位于他们没有理由打开的文件中。

前向引用写起来便宜，却很容易忘记，因此这里设置门禁，而不是只约定规范。
"""

from scripts.check_adr_links import backlink_problems, strong_references


class TestFindingStrongReferences:
    def test_an_amendment_is_a_strong_reference(self) -> None:
        assert strong_references("Status: Accepted (amends ADR 0009)") == {9}

    def test_supersedes_and_corrects_count_in_the_active_voice(self) -> None:
        assert strong_references("this supersedes ADR 0001") == {1}
        assert strong_references("corrects ADR 0009") == {9}

    def test_the_passive_voice_is_the_backlink_not_a_claim(self) -> None:
        """“Corrected *by* ADR 0026” 是被推翻的文档写下的内容——这句话就是本门禁
        要求的指针，因此不应反过来要求链接。若将其当成主张，会颠倒关系，这正是
        本门禁第一次失败的原因。"""
        assert strong_references("Corrected by ADR 0026 (2026-08-13)") == set()
        assert strong_references("the scope was reversed by ADR 0009") == set()
        assert strong_references("Amended by ADR 0017: the exec profile") == set()

    def test_a_plain_mention_is_not_a_strong_reference(self) -> None:
        """为上下文引用 ADR 不应要求反向链接，否则每个 ADR 都必须链接所有其他 ADR。"""
        assert strong_references("as described in ADR 0008, the prefix is stable") == set()
        assert strong_references("extends ADR 0012/0013") == set()

    def test_several_references_are_all_found(self) -> None:
        text = "amends ADR 0009 and supersedes ADR 0010"
        assert strong_references(text) == {9, 10}


class TestBacklinks:
    def test_a_missing_backlink_is_reported(self) -> None:
        docs = {
            9: "the original reasoning, with no forward pointer",
            26: "Status: Accepted (corrects ADR 0009)",
        }
        problems = backlink_problems(docs)
        assert len(problems) == 1
        assert "0009" in problems[0] and "0026" in problems[0]

    def test_a_present_backlink_passes(self) -> None:
        docs = {
            9: "Amended by ADR 0026: the premise below is wrong.",
            26: "Status: Accepted (corrects ADR 0009)",
        }
        assert backlink_problems(docs) == []

    def test_a_reference_to_a_missing_adr_is_reported(self) -> None:
        assert backlink_problems({26: "corrects ADR 0099"}) != []


def test_the_real_adrs_all_backlink() -> None:
    from scripts.check_adr_links import collect_problems

    assert collect_problems() == []
