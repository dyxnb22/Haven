"""Shared strict-model base for all boundary DTOs."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """All external inputs are strict: no coercion, no extra fields, frozen."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
