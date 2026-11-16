"""验证组的配置、固定命令身份和门禁正反例。"""

from dataclasses import replace
from pathlib import Path

import pytest

from haven.application.verification import recipe_invocation_digest, select_verification
from haven.config import ConfigError, explain, load_config
from haven.contracts.tools import RecipeSpec
from haven.domain.evidence import (
    CheckEvidence,
    DiffEvidence,
    EditEvidence,
    EvidenceLedger,
    RequiredCheck,
    evaluate_evidence_gate,
    required_check_states,
)


def result(seq: int, recipe: str, exit_code: int = 0, digest: str = "fixed") -> CheckEvidence:
    return CheckEvidence(seq, recipe, exit_code, 1, False, digest)


REQUIRED = (RequiredCheck("test", "fixed"), RequiredCheck("lint", "fixed"))


def test_named_groups_are_explicit_and_report_their_members(tmp_path: Path) -> None:
    (tmp_path / ".haven.toml").write_text(
        '[verification_groups]\nquality = ["test", "lint"]\n'
        '[recipes.test]\nargv = ["pytest"]\n[recipes.lint]\nargv = ["ruff", "check", "."]\n'
    )
    config = load_config(tmp_path)
    assert config.verification_groups == {"quality": ("test", "lint")}
    assert select_verification(None, config.verification_groups, config.recipes) is None
    selected = select_verification("quality", config.verification_groups, config.recipes)
    assert selected is not None
    assert [r.recipe_id for r in selected.required] == ["test", "lint"]
    assert ("verification_groups.quality", "test, lint", "config (explicit selection)") in explain(
        config
    )
    with pytest.raises(ValueError, match="unknown verification group"):
        select_verification("typo", config.verification_groups, config.recipes)


@pytest.mark.parametrize(
    "configuration",
    [
        'verification_groups = ["quality"]\n',
        "[verification_groups]\nquality = []\n",
        '[verification_groups]\nquality = ["test", "test"]\n',
        '[verification_groups]\nquality = ["missing"]\n',
        '[verification_groups]\nquality = ["test", true]\n',
        '[verification_groups]\n"with spaces" = ["test"]\n',
    ],
)
def test_invalid_group_configuration_is_rejected(tmp_path: Path, configuration: str) -> None:
    (tmp_path / ".haven.toml").write_text(configuration + '[recipes.test]\nargv = ["pytest"]\n')
    with pytest.raises(ConfigError, match="verification"):
        load_config(tmp_path)


def test_selected_groups_require_every_check_even_without_writes() -> None:
    one = EvidenceLedger().with_check(result(1, "test"))
    gate = evaluate_evidence_gate(one, required_checks=REQUIRED)
    assert not gate.passed and gate.reason_code == "required_check_not_run"
    assert "lint" in gate.detail
    both = one.with_check(result(2, "lint"))
    assert evaluate_evidence_gate(both, required_checks=REQUIRED).passed
    failing = both.with_check(result(3, "lint", 1))
    assert (
        evaluate_evidence_gate(failing, required_checks=REQUIRED).reason_code
        == "required_check_failed"
    )
    retried = failing.with_check(result(4, "lint"))
    assert evaluate_evidence_gate(retried, required_checks=REQUIRED).passed
    assert evaluate_evidence_gate(EvidenceLedger()).reason_code == "no_writes"


def test_a_write_invalidates_all_preceding_required_checks() -> None:
    ledger = (
        EvidenceLedger()
        .with_check(result(1, "test"))
        .with_check(result(2, "lint"))
        .with_edit(EditEvidence(3, "src/a.py", "before", "after"))
        .with_diff(DiffEvidence(4, 1, 1, 1, "diff"))
    )
    assert (
        evaluate_evidence_gate(ledger, required_checks=REQUIRED).reason_code
        == "required_check_stale"
    )
    assert {
        s.status for s in required_check_states(REQUIRED, ledger.checks, ledger.last_edit_seq)
    } == {"stale"}
    assert not evaluate_evidence_gate(
        ledger.with_check(result(5, "test")), required_checks=REQUIRED
    ).passed
    assert evaluate_evidence_gate(
        ledger.with_check(result(5, "test")).with_check(result(6, "lint")),
        required_checks=REQUIRED,
    ).passed


def test_legacy_or_changed_commands_cannot_satisfy_selected_requirements() -> None:
    for digest in ("", "different"):
        ledger = EvidenceLedger().with_check(result(1, "test", digest=digest))
        assert (
            evaluate_evidence_gate(ledger, required_checks=REQUIRED[:1]).reason_code
            == "required_check_mismatch"
        )
    unavailable = evaluate_evidence_gate(
        EvidenceLedger(), required_checks=REQUIRED, verification_available=False
    )
    assert not unavailable.passed and unavailable.terminal


def test_command_digest_covers_definition_and_extra_arguments() -> None:
    recipe = RecipeSpec(id="test", argv=("pytest",), timeout_seconds=30.0)
    digest = recipe_invocation_digest(recipe)
    assert recipe_invocation_digest(recipe.model_copy()) == digest
    assert recipe_invocation_digest(recipe, ("-k", "small_subset")) != digest
    for update in [
        {"argv": ("true",)},
        {"timeout_seconds": 60.0},
        {"allow_network": True},
        {"readable_roots": ("/different",)},
        {"ephemeral": True},
    ]:
        assert recipe_invocation_digest(recipe.model_copy(update=update)) != digest
    changed = replace(result(1, "test"), recipe_digest="another")
    assert required_check_states(REQUIRED, (changed,))[0].status == "mismatch"
