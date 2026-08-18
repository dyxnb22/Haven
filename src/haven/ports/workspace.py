"""工作区端口：在一个仓库内进行有界、规范化的文件访问。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class PathFacts:
    """关于一个路径提议、经程序验证的事实。"""

    raw: str
    normalized: str
    within_workspace: bool
    is_protected: bool
    exists: bool
    is_file: bool
    is_dir: bool
    size_bytes: int
    digest: str | None


@dataclass(frozen=True, slots=True)
class ListEntry:
    name: str
    is_dir: bool
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ListResult:
    path: str
    entries: tuple[ListEntry, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class SearchMatch:
    path: str
    line_number: int
    line: str


@dataclass(frozen=True, slots=True)
class SearchResult:
    matches: tuple[SearchMatch, ...]
    files_scanned: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class ReadResult:
    path: str
    content: str
    start_line: int
    end_line: int
    total_lines: int
    truncated: bool
    digest: str


@dataclass(frozen=True, slots=True)
class EditPreview:
    path: str
    diff: str
    preimage_digest: str
    postimage_digest: str
    insertions: int
    deletions: int


@dataclass(frozen=True, slots=True)
class EditOutcome:
    path: str
    preimage_digest: str
    postimage_digest: str


@dataclass(frozen=True, slots=True)
class PatchOpSpec:
    """多文件补丁中的一个操作，以与端口无关的形式表示。"""

    kind: str  # 操作类型："edit" | "create" | "delete" | "move"
    path: str = ""  # 适用于 edit / create / delete
    src: str = ""  # 适用于 move
    dest: str = ""  # 适用于 move
    old: str = ""  # 适用于 edit
    new: str = ""  # 适用于 edit
    occurrence: int | None = None
    replace_all: bool = False
    content: str = ""  # 适用于 create


@dataclass(frozen=True, slots=True)
class PatchEffect:
    """计划补丁产生的一个文件级效果，用于日志和证据。

    形状与单操作工具一致，因此恢复分类器可以复用现有规则：中断的补丁会按组成
    其的效果写入日志，每个效果都可以单独从磁盘得到证明。
    """

    tool_shape: str  # 工具形态："repo.edit" | "repo.create" | "repo.delete" | "repo.move"
    path: str
    preimage_digest: str
    expected_postimage: str
    dest_path: str = ""


@dataclass(frozen=True, slots=True)
class PatchPreview:
    """补丁的确定性计划：一份可审阅的差异、它绑定的精确前像集合，以及将写入日志
    的逐文件效果。"""

    diff: str
    #: 规范化路径 -> 补丁触及的每个预先存在文件的摘要。
    preimages: dict[str, str]
    effects: tuple[PatchEffect, ...]
    #: 规范化路径 -> 补丁完成后仍存在的每个文件的完整内容。
    #: 仅供服务端使用，绝不展示给模型。
    final_contents: dict[str, str]
    insertions: int
    deletions: int


@dataclass(frozen=True, slots=True)
class RunDiff:
    diff: str
    files: tuple[str, ...] = field(default_factory=tuple)
    insertions: int = 0
    deletions: int = 0
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    """工作区某一时刻的视图，用于归因进程写入。

    `digests` 覆盖每个普通文件，因此任何文件的变更——文本或二进制——都能检测到。
    `contents` 只保存可生成差异的子集（在编辑大小上限内且为有效 UTF-8），因此
    运行差异可以渲染具体变更。
    """

    digests: dict[str, str] = field(default_factory=dict)
    contents: dict[str, str] = field(default_factory=dict)
    #: 受保护路径（`.git`、`.haven`、`.haven.toml`）的摘要，与排除这些路径
    #: 的 `digests` 分开保存。即使操作系统沙箱无法阻止进程触碰它们，工具层
    #: 也必须能够检测到篡改（Landlock 无法在可写工作区中挖出一个不可写的
    #: `.git`）。
    protected_digests: dict[str, str] = field(default_factory=dict)


class WorkspaceError(Exception):
    """工作区违规时抛出；始终映射为稳定的工具错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class PatchRollbackError(Exception):
    """补丁在提交过程中失败，并且回滚也失败：目录树处于确定性代码无法撤销的
    部分状态。

    有意不继承 WorkspaceError：流水线会将 WorkspaceError 映射为干净的 FAILED
    结果，而此错误必须暴露为未知效果，以便恢复流程阻止继续并让人类协调处理。
    """


class WorkspacePort(Protocol):
    @property
    def root(self) -> Path: ...

    @property
    def workspace_digest(self) -> str: ...

    def path_facts(self, raw_path: str) -> PathFacts: ...

    async def list_dir(self, path: str, max_entries: int) -> ListResult: ...

    async def search(self, pattern: str, path: str, max_results: int) -> SearchResult: ...

    async def read_file(self, path: str, start_line: int, max_lines: int) -> ReadResult: ...

    async def preview_edit(
        self,
        path: str,
        old: str,
        new: str,
        *,
        occurrence: int | None = None,
        replace_all: bool = False,
    ) -> EditPreview: ...

    async def apply_edit(
        self,
        path: str,
        old: str,
        new: str,
        expected_preimage: str,
        *,
        occurrence: int | None = None,
        replace_all: bool = False,
    ) -> EditOutcome: ...

    async def preview_create(self, path: str, content: str) -> EditPreview: ...

    async def apply_create(self, path: str, content: str) -> EditOutcome: ...

    async def preview_delete(self, path: str) -> EditPreview: ...

    async def apply_delete(self, path: str, expected_preimage: str) -> EditOutcome: ...

    async def preview_move(self, src: str, dest: str) -> tuple[EditPreview, EditPreview]: ...

    async def apply_move(
        self, src: str, dest: str, expected_preimage: str
    ) -> tuple[EditOutcome, EditOutcome]: ...

    async def preview_patch(
        self, ops: tuple[PatchOpSpec, ...], files_read: dict[str, str]
    ) -> PatchPreview: ...

    async def apply_patch(self, plan: PatchPreview) -> tuple[EditOutcome, ...]: ...

    async def run_diff(self) -> RunDiff: ...

    def original_contents(self) -> dict[str, str]: ...

    def restore_originals(self, originals: dict[str, str]) -> None: ...

    def capture_snapshot(self) -> WorkspaceSnapshot: ...

    def register_run_original(self, path: str, content: str) -> None: ...
