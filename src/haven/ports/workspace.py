"""工作区端口：在一个仓库内进行有界、规范化的文件访问。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class PathFacts:
    """关于一个路径提议、经程序验证的事实。"""

    #: 规范化前的用户输入路径。
    raw: str
    #: 后续所有 I/O 使用的规范化工作区相对路径。
    normalized: str
    #: 规范化后路径是否仍位于工作区根目录内。
    within_workspace: bool
    #: 路径是否位于受保护的控制面组件下。
    is_protected: bool
    #: 规范化路径当前是否存在。
    exists: bool
    #: 现有路径是否为普通文件。
    is_file: bool
    #: 现有路径是否为目录。
    is_dir: bool
    #: 文件大小，单位为字节；目录和不存在的路径为零。
    size_bytes: int
    #: 普通文件的内容摘要；其他情况为 None。
    digest: str | None


@dataclass(frozen=True, slots=True)
class ListEntry:
    """目录列表中的一个条目。"""

    #: 相对于被列出目录的条目名称。
    name: str
    #: 此条目是否为目录而非普通文件。
    is_dir: bool
    #: 文件大小，单位为字节；目录为零。
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ListResult:
    """目录列表结果；`truncated` 表示达到条目上限。"""

    #: 结果所表示的规范化目录路径。
    path: str
    #: 按确定性顺序返回的条目。
    entries: tuple[ListEntry, ...]
    #: 因 max_entries 无法返回全部条目时为 True。
    truncated: bool


@dataclass(frozen=True, slots=True)
class SearchMatch:
    """搜索命中的文件路径、行号和整行文本。"""

    #: 包含匹配内容的规范化工作区相对路径。
    path: str
    #: 匹配行的 1-based 行号。
    line_number: int
    #: 完整的匹配行，长度受搜索实现限制。
    line: str


@dataclass(frozen=True, slots=True)
class SearchResult:
    """搜索结果及扫描数量；结果可能因上限而截断。"""

    #: 按确定性遍历顺序排列的匹配行。
    matches: tuple[SearchMatch, ...]
    #: 检查过的文件数，包括没有匹配的文件。
    files_scanned: int
    #: 因结果/字节上限或搜索截止时间无法返回全部匹配时为 True。
    truncated: bool


@dataclass(frozen=True, slots=True)
class ReadResult:
    """文件分段读取结果及其实际行范围和内容摘要。"""

    #: 已读取的规范化工作区相对路径。
    path: str
    #: 请求的行切片，按 UTF-8 文本解码。
    content: str
    #: 实际返回的 1-based 行范围。
    start_line: int
    #: 实际返回的 1-based 最后一行；文件为空时为零。
    end_line: int
    #: 完整文件的总行数。
    total_lines: int
    #: max_lines 未覆盖整个文件时为 True。
    truncated: bool
    #: 完整文件的摘要，用作编辑前像。
    digest: str


@dataclass(frozen=True, slots=True)
class EditPreview:
    """写入发生前生成的变更预览和前后内容摘要。"""

    #: 将要修改的规范化工作区相对路径。
    path: str
    #: 展示给用户审批的有界统一差异。
    diff: str
    #: 提议编辑前完整文件的摘要。
    preimage_digest: str
    #: 应用提议编辑后完整文件的摘要。
    postimage_digest: str
    #: 有界差异中的新增行数。
    insertions: int
    #: 有界差异中的删除行数。
    deletions: int


@dataclass(frozen=True, slots=True)
class EditOutcome:
    """写入完成后返回的实际前后内容摘要。"""

    #: 操作修改的规范化工作区相对路径。
    path: str
    #: 写入前一刻观察到的摘要。
    preimage_digest: str
    #: 写入后一刻观察到的摘要。
    postimage_digest: str


@dataclass(frozen=True, slots=True)
class PatchOpSpec:
    """多文件补丁中的一个操作，以与端口无关的形式表示。"""

    #: 操作类型："edit" | "create" | "delete" | "move"。
    kind: str
    #: edit/create/delete 操作使用的路径。
    path: str = ""
    #: move 操作的源路径。
    src: str = ""
    #: move 操作的目标路径。
    dest: str = ""
    #: edit 操作要匹配的旧文本。
    old: str = ""
    #: edit 操作要写入的新文本。
    new: str = ""
    #: edit 操作的 1-based 匹配序号；None 表示必须唯一匹配。
    occurrence: int | None = None
    #: edit 是否有意替换所有匹配项。
    replace_all: bool = False
    #: create 操作使用的新文件完整内容。
    content: str = ""


@dataclass(frozen=True, slots=True)
class PatchEffect:
    """计划补丁产生的一个文件级效果，用于日志和证据。

    形状与单操作工具一致，因此恢复分类器可以复用现有规则：中断的补丁会按组成
    其的效果写入日志，每个效果都可以单独从磁盘得到证明。
    """

    #: 工具形态："repo.edit" | "repo.create" | "repo.delete" | "repo.move"。
    tool_shape: str
    #: 此效果影响的规范化源路径。
    path: str
    #: 操作前摘要；新文件为空。
    preimage_digest: str
    #: 操作后的预期摘要，恢复时使用。
    expected_postimage: str
    #: move 效果的目标路径；其他操作形态为空。
    dest_path: str = ""


@dataclass(frozen=True, slots=True)
class PatchPreview:
    """补丁的确定性计划：一份可审阅的差异、它绑定的精确前像集合，以及将写入日志
    的逐文件效果。"""

    #: 供人审批的有界统一差异。
    diff: str
    #: 规范化路径 -> 补丁触及的每个预先存在文件的摘要。
    preimages: dict[str, str]
    #: 按执行顺序记录的文件级效果。
    effects: tuple[PatchEffect, ...]
    #: 规范化路径 -> 补丁完成后仍存在的每个文件的完整内容。
    #: 仅供服务端使用，绝不展示给模型。
    final_contents: dict[str, str]
    #: 完整补丁中的新增行数。
    insertions: int
    #: 完整补丁中的删除行数。
    deletions: int


@dataclass(frozen=True, slots=True)
class RunDiff:
    """工作区相对运行基线的累计差异。"""

    #: 相对于运行原始内容基线的有界统一差异。
    diff: str
    #: 存在净变化的规范化路径。
    files: tuple[str, ...] = field(default_factory=tuple)
    #: 完整差异中的新增行数。
    insertions: int = 0
    #: 完整差异中的删除行数。
    deletions: int = 0
    #: MAX_DIFF_BYTES 截断差异载荷时为 True。
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    """工作区某一时刻的视图，用于归因进程写入。

    `digests` 覆盖每个普通文件，因此任何文件的变更——文本或二进制——都能检测到。
    `contents` 只保存可生成差异的子集（在编辑大小上限内且为有效 UTF-8），因此
    运行差异可以渲染具体变更。
    """

    #: 每个普通文件（包括二进制文件）的规范化路径 -> 摘要。
    digests: dict[str, str] = field(default_factory=dict)
    #: 为生成人类可读进程差异而保留的规范化路径 -> 可读文本。
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


class EffectUnknownError(Exception):
    """工作区已经开始产生副作用，但确定性代码无法证明最终磁盘状态。"""


class PatchRollbackError(EffectUnknownError):
    """补丁在提交过程中失败，并且回滚也失败：目录树处于确定性代码无法撤销的
    部分状态。

    有意不继承 WorkspaceError：流水线会将 WorkspaceError 映射为干净的 FAILED
    结果，而此错误必须暴露为未知效果，以便恢复流程阻止继续并让人类协调处理。
    """


class WorkspacePort(Protocol):
    """工作区抽象；实现负责路径规范化、保护路径和文件 I/O。"""

    @property
    def root(self) -> Path:
        """返回已解析的工作区根目录。"""
        ...

    @property
    def workspace_digest(self) -> str:
        """返回绑定审批、检查点和恢复身份的工作区摘要。"""
        ...

    def path_facts(self, raw_path: str) -> PathFacts:
        """规范化路径并返回越界、保护状态及当前文件事实。"""
        ...

    async def list_dir(self, path: str, max_entries: int) -> ListResult:
        """按确定性顺序列出目录，并在达到上限时标记 truncated。"""
        ...

    async def search(self, pattern: str, path: str, max_results: int) -> SearchResult:
        """在工作区内搜索正则匹配，并返回有界结果及扫描数量。"""
        ...

    async def read_file(self, path: str, start_line: int, max_lines: int) -> ReadResult:
        """按 1-based 行号读取有界文本，同时返回完整文件摘要。"""
        ...

    async def preview_edit(
        self,
        path: str,
        old: str,
        new: str,
        *,
        occurrence: int | None = None,
        replace_all: bool = False,
    ) -> EditPreview:
        """计算编辑预览和前后摘要；此操作不修改磁盘。"""
        ...

    async def apply_edit(
        self,
        path: str,
        old: str,
        new: str,
        expected_preimage: str,
        *,
        occurrence: int | None = None,
        replace_all: bool = False,
    ) -> EditOutcome:
        """校验 expected_preimage 后原子应用单文件编辑，过期时失败关闭。"""
        ...

    async def preview_create(self, path: str, content: str) -> EditPreview:
        """为不存在的文件生成创建预览，不覆盖已有路径。"""
        ...

    async def apply_create(self, path: str, content: str) -> EditOutcome:
        """按创建预览原子写入新文件，目标已存在时失败。"""
        ...

    async def preview_delete(self, path: str) -> EditPreview:
        """生成删除预览并固定待删除文件的前像摘要。"""
        ...

    async def apply_delete(self, path: str, expected_preimage: str) -> EditOutcome:
        """校验前像后删除文件；并发修改时拒绝删除。"""
        ...

    async def preview_move(self, src: str, dest: str) -> tuple[EditPreview, EditPreview]:
        """生成移动两端的预览；目标路径必须不存在且两端都在工作区内。"""
        ...

    async def apply_move(
        self, src: str, dest: str, expected_preimage: str
    ) -> tuple[EditOutcome, EditOutcome]:
        """校验源文件前像后执行不覆盖目标的原子移动。"""
        ...

    async def preview_patch(
        self, ops: tuple[PatchOpSpec, ...], files_read: dict[str, str]
    ) -> PatchPreview:
        """按顺序规划多文件补丁，并绑定读取前像和逐文件效果。"""
        ...

    async def apply_patch(self, plan: PatchPreview) -> tuple[EditOutcome, ...]:
        """提交补丁计划；任一失败都尝试回滚，无法回滚则报告未知效果。"""
        ...

    async def run_diff(self) -> RunDiff:
        """返回相对于本次运行原始内容的累计有界差异。"""
        ...

    def original_contents(self) -> dict[str, str | None]:
        """返回首次触碰时的原始文本；None 表示路径原本不存在。"""
        ...

    def restore_originals(self, originals: dict[str, str | None]) -> None:
        """恢复运行级差异基线；调用方负责更高层授权。"""
        ...

    def capture_snapshot(self) -> WorkspaceSnapshot:
        """捕获用于归因进程外部写入的完整工作区摘要快照。"""
        ...

    def register_run_original(self, path: str, content: str | None) -> None:
        """登记文件的运行前内容，供 run_diff 和 rewind 使用。"""
        ...
