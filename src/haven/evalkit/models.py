"""评估用例契约与逐案例结果。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import Field

from haven.contracts.base import StrictModel
from haven.ports.model import ModelPort

ModelFactory = Callable[[], ModelPort]


class ExpectSpec(StrictModel):
    status: str
    stop_reason: str = ""
    gate_reason: str = ""
    file_contains: dict[str, str] = Field(default_factory=dict)
    file_not_contains: dict[str, str] = Field(default_factory=dict)
    allowed_changed_files: tuple[str, ...] = ()
    error_codes: tuple[str, ...] = ()
    denied_reasons: tuple[str, ...] = ()
    transcript_must_not_contain: tuple[str, ...] = ()
    max_steps_used: int = 0


class RecipeDef(StrictModel):
    argv: tuple[str, ...]
    timeout_seconds: float = 60.0
    readable_roots: tuple[str, ...] = ()


class EvalCase(StrictModel):
    id: str
    category: str
    goal: str
    fixture: str
    mode: str = "interactive"
    approval_policy: str = "approve_all"
    repeat_last: bool = False
    scenario: str = ""
    discover: bool = False
    hidden_check: str = ""
    max_context_chars: int = 0
    budget: dict[str, int | float] = Field(default_factory=dict)
    recipes: dict[str, RecipeDef] = Field(default_factory=dict)
    turns: list[list[dict[str, Any]]] = Field(default_factory=list)
    expect: ExpectSpec


@dataclass(slots=True)
class CaseResult:
    case_id: str
    category: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    status: str = ""
    stop_reason: str = ""
    steps: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    unauthorized_changes: int = 0
    out_of_scope_changes: int = 0
