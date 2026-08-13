"""An ADR that another ADR overturns must point at its correction.

Found by audit: ADR 0009 argued that misclassifying an exec command "costs a
skipped prompt, not an escape". ADR 0026 refuted that premise outright — and
nothing in 0009 said so. A reader of 0009 alone, human or agent, came away with
a wrong security model, because the correction lived in a file they had no
reason to open.

Forward references are cheap to write and easy to forget, so this is a gate
rather than a convention.
"""

from scripts.check_adr_links import backlink_problems, strong_references


class TestFindingStrongReferences:
    def test_an_amendment_is_a_strong_reference(self) -> None:
        assert strong_references("Status: Accepted (amends ADR 0009)") == {9}

    def test_supersedes_and_corrects_count_in_the_active_voice(self) -> None:
        assert strong_references("this supersedes ADR 0001") == {1}
        assert strong_references("corrects ADR 0009") == {9}

    def test_the_passive_voice_is_the_backlink_not_a_claim(self) -> None:
        """ "Corrected *by* ADR 0026" is written by the document being
        overturned — that sentence is the pointer this gate wants, so it must
        not also demand one in return. Read as a claim it inverts the
        relationship, which is exactly how this gate first failed."""
        assert strong_references("Corrected by ADR 0026 (2026-08-13)") == set()
        assert strong_references("the scope was reversed by ADR 0009") == set()
        assert strong_references("Amended by ADR 0017: the exec profile") == set()

    def test_a_plain_mention_is_not_a_strong_reference(self) -> None:
        """Citing an ADR for context must not demand a backlink, or every ADR
        would have to link every other one."""
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
