"""已加载项目指引的只读快照；文字与来源元数据不携带执行权限。"""

from typing import Literal

from pydantic import Field

from haven.contracts.base import StrictModel
from haven.contracts.project_memory import ProjectNote


class GuidanceSource(StrictModel):
    path: str
    scope: str = "."
    status: Literal["loaded", "missing", "empty", "unavailable", "omitted"]
    included_chars: int = Field(default=0, ge=0)
    truncated: bool = False
    reason: str = ""


class GuidanceSnapshot(StrictModel):
    text: str = ""
    notes: tuple[ProjectNote, ...] = ()
    sources: tuple[GuidanceSource, ...] = ()
    max_chars: int = Field(default=0, ge=0)
    per_file_chars: int = Field(default=0, ge=0)
    per_file_lines: int = Field(default=0, ge=0)
    skipped_scopes: int = Field(default=0, ge=0)
    unreported_sources: int = Field(default=0, ge=0)
