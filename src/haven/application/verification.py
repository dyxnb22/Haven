"""解析、冻结和核对用户选择的验证范围。

这里只有选择、校验和报告，**不执行任何检查**：真正的执行仍走
`repo.check` 那一条通道，由审批和沙箱把关。本模块回答的是"这一轮要求哪些配方
必须通过"，而不是"怎么把它们跑起来"。
"""

import json
from collections.abc import Mapping

from haven.contracts.tools import RecipeSpec
from haven.contracts.verification import RecipeRequirement, VerificationSelection
from haven.domain.digest import sha256_text
from haven.domain.enums import AgentMode, PermissionMode
from haven.domain.evidence import EvidenceLedger, required_check_states


def recipe_invocation_digest(recipe: RecipeSpec, args: tuple[str, ...] = ()) -> str:
    return sha256_text(
        json.dumps(
            {"recipe": recipe.model_dump(mode="json"), "args": args},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    )


def select_verification(
    name: str | None,
    groups: Mapping[str, tuple[str, ...]],
    recipes: Mapping[str, RecipeSpec],
) -> VerificationSelection | None:
    if not name:
        return None
    if name not in groups:
        raise ValueError(f"unknown verification group {name!r}; choose from {sorted(groups)}")
    required = []
    for recipe_id in groups[name]:
        recipe = recipes.get(recipe_id)
        if recipe is None or recipe.ephemeral:
            raise ValueError(f"required recipe {recipe_id!r} is unavailable or not configured")
        required.append(
            RecipeRequirement(recipe_id=recipe_id, recipe_digest=recipe_invocation_digest(recipe))
        )
    return VerificationSelection(group_name=name, required=tuple(required))


def validate_verification(
    selection: VerificationSelection | None,
    recipes: Mapping[str, RecipeSpec],
    mode: PermissionMode,
    agent_mode: AgentMode,
) -> None:
    if selection is None:
        return
    if agent_mode is not AgentMode.PLAN and mode is PermissionMode.READ_ONLY:
        raise ValueError(
            "a verification group needs writable execution permission; use --write "
            "and an approval policy, or explicitly clear the group for a new turn"
        )
    for required in selection.required:
        recipe = recipes.get(required.recipe_id)
        if (
            recipe is None
            or recipe.ephemeral
            or recipe_invocation_digest(recipe) != required.recipe_digest
        ):
            raise ValueError(
                f"required recipe {required.recipe_id!r} is missing or changed since selection; "
                "restore its configuration to resume, or choose a group for a new run"
            )


def verification_report(
    selection: VerificationSelection | None, ledger: EvidenceLedger
) -> dict[str, object] | None:
    if selection is None:
        return None
    return {
        "group": selection.group_name,
        "checks": [
            {"recipe_id": state.recipe_id, "status": state.status}
            for state in required_check_states(
                selection.to_domain(), ledger.checks, ledger.last_edit_seq
            )
        ],
    }
