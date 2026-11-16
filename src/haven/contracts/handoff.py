"""用户显式维护的交接笔记；任何文字都不是权限或检查证据。"""

from typing import Literal

from pydantic import Field, model_validator

from haven.contracts.base import StrictModel

HANDOFF_TOTAL_CHARS = 2000
HandoffSource = Literal["manual", "plan_draft"]


class HandoffNotes(StrictModel):
    completed: str = Field(default="", max_length=600)
    decisions: str = Field(default="", max_length=600)
    attempts: str = Field(default="", max_length=600)
    next_steps: str = Field(default="", max_length=600)
    pending_checks: str = Field(default="", max_length=600)

    @model_validator(mode="after")
    def limit_total(self) -> "HandoffNotes":
        if sum(len(value) for value in self.model_dump().values()) > HANDOFF_TOTAL_CHARS:
            raise ValueError(f"handoff notes exceed {HANDOFF_TOTAL_CHARS} characters in total")
        return self


class HandoffSnapshot(StrictModel):
    source_run_id: str
    event_seq: int = Field(ge=1)
    at: str
    source: HandoffSource
    checkpoint_seq: int = Field(ge=0)
    notes: HandoffNotes
