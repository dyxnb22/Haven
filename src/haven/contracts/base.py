"""所有边界 DTO 共用的严格模型基类。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """所有外部输入都采用严格模式：不做强制转换、不允许额外字段，并且冻结。"""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
