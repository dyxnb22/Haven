"""绑定验证结果与两侧文件内容的合流审阅快照。"""

from haven.contracts.base import StrictModel


class ReviewSnapshot(StrictModel):
    run_id: str
    binding: str
    files: tuple[str, ...]
    diff: str
    preimages: dict[str, str]
    postimages: dict[str, str]
    applied: bool = False
    discarded: bool = False
    truncated: bool = False
