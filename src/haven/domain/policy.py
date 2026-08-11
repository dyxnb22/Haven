"""Deterministic policy: the only authority over model-proposed side effects.

The policy is a pure function. Its inputs are the permission mode and a set of
program-collected facts about the proposal — never model text. Approval can
turn ASK into a single execution ticket; nothing can turn DENY into ALLOW.
"""

from __future__ import annotations

from dataclasses import dataclass

from haven.domain.enums import PermissionMode, PolicyDecision, RiskLevel

#: Tools that only observe the workspace.
READ_ONLY_TOOLS = frozenset({"repo.list", "repo.search", "repo.read", "repo.diff"})

#: Tools with side effects on files or processes.
EFFECT_TOOLS = frozenset({"repo.edit", "repo.create", "repo.check"})

#: Tools that only mutate the run's own in-memory state. They touch nothing on
#: disk and nothing outside the run, so they are allowed even in read_only mode.
STATE_TOOLS = frozenset({"task.plan"})

KNOWN_TOOLS = READ_ONLY_TOOLS | EFFECT_TOOLS | STATE_TOOLS


@dataclass(frozen=True, slots=True)
class ToolFacts:
    """Program-verified facts about one tool proposal.

    Collected by the pipeline from the normalized arguments and the workspace;
    the model has no way to influence these directly.
    """

    tool_name: str
    within_workspace: bool = True
    touches_protected_path: bool = False
    recipe_registered: bool | None = None
    preimage_digest: str | None = None
    path: str | None = None


@dataclass(frozen=True, slots=True)
class PolicyOutcome:
    decision: PolicyDecision
    reason_code: str
    risk: RiskLevel


def evaluate_policy(mode: PermissionMode, facts: ToolFacts) -> PolicyOutcome:
    """Decide allow/ask/deny for one proposal. Pure and total."""
    if facts.tool_name not in KNOWN_TOOLS:
        return PolicyOutcome(PolicyDecision.DENY, "unknown_tool", RiskLevel.HIGH)

    # Hard denies apply in every mode; user approval cannot override them.
    if not facts.within_workspace:
        return PolicyOutcome(PolicyDecision.DENY, "outside_workspace", RiskLevel.HIGH)
    if facts.touches_protected_path:
        return PolicyOutcome(PolicyDecision.DENY, "protected_path", RiskLevel.HIGH)

    if facts.tool_name in STATE_TOOLS:
        return PolicyOutcome(PolicyDecision.ALLOW, "state_tool", RiskLevel.NONE)

    if facts.tool_name in READ_ONLY_TOOLS:
        return PolicyOutcome(PolicyDecision.ALLOW, "read_only_tool", RiskLevel.NONE)

    # Side-effect tools from here on.
    if mode is PermissionMode.READ_ONLY:
        return PolicyOutcome(PolicyDecision.DENY, "read_only_mode", RiskLevel.MEDIUM)

    if facts.tool_name == "repo.check":
        if not facts.recipe_registered:
            return PolicyOutcome(PolicyDecision.DENY, "unregistered_recipe", RiskLevel.HIGH)
        return PolicyOutcome(PolicyDecision.ASK, "check_requires_approval", RiskLevel.MEDIUM)

    if facts.tool_name == "repo.create":
        return PolicyOutcome(PolicyDecision.ASK, "create_requires_approval", RiskLevel.MEDIUM)

    # repo.edit
    return PolicyOutcome(PolicyDecision.ASK, "write_requires_approval", RiskLevel.MEDIUM)
