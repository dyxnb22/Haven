"""供人类回看进程输出的有界元数据；不属于验证证据。"""

from typing import Annotated

from pydantic import Field, StringConstraints

from haven.contracts.base import StrictModel


class ProcessReport(StrictModel):
    recipe_id: str = Field(default="", max_length=100)
    exit_code: int
    timed_out: bool = False
    cwd: str = Field(max_length=4096)
    output_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    output_truncated: bool = False
    diagnostics: tuple[Annotated[str, StringConstraints(max_length=600)], ...] = Field(
        default=(), max_length=25
    )
