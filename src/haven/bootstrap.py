"""组合根：唯一同时了解适配器和用例的模块。

接口层（CLI/TUI）从这里接收已经完成组装的服务，绝不直接导入适配器。
测试则在这里换入 ScriptedModel 和 MemorySessionStore。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from haven.adapters.git_baseline import capture_git_baseline
from haven.adapters.process_executor import ProcessExecutor
from haven.adapters.providers.openai_compatible import OpenAICompatibleModel
from haven.adapters.sandbox.landlock import LandlockLauncher
from haven.adapters.sandbox.seatbelt import SeatbeltLauncher
from haven.adapters.sqlite_session import SqliteSessionStore
from haven.adapters.workspace_fs import FsWorkspace
from haven.adapters.workspace_lease import LeaseHeld, WorkspaceLease, acquire_workspace_lease
from haven.application.approvals import ApprovalResponder
from haven.application.context_builder import ContextBuilder
from haven.application.emitter import EventEmitter
from haven.application.profiles import profile_for
from haven.application.recovery_service import RecoveryService
from haven.application.replay_service import ReplayService
from haven.application.run_service import RunService
from haven.config import ResolvedConfig, artifacts_dir, data_dir, db_path, load_config
from haven.contracts.events import ContextSegment
from haven.contracts.model import ModelRequest
from haven.contracts.tools import tool_schemas
from haven.domain.budget import BudgetUsage
from haven.domain.enums import PermissionMode
from haven.ports.event_sink import EventSinkPort
from haven.ports.model import ModelPort
from haven.ports.sandbox import SandboxLauncher
from haven.ports.workspace import WorkspaceError

MAX_GUIDANCE_CHARS = 4_000


class BootstrapError(Exception):
    pass


@dataclass(slots=True)
class AppServices:
    config: ResolvedConfig
    workspace: FsWorkspace
    store: SqliteSessionStore
    emitter: EventEmitter
    run_service: RunService
    recovery: RecoveryService
    replay: ReplayService
    git_branch: str
    git_commit: str
    model_name: str
    model: ModelPort | None = None
    sandbox_backend: str = "none"
    #: 当本进程可以修改工作区时使用的单写者租约；只读模式下或另一个存活进程
    #: 持有租约时为 None（此时 `lease_warning` 会说明情况，并且模式已降级）。
    lease: WorkspaceLease | None = None
    lease_warning: str = ""

    async def close(self) -> None:
        if self.lease is not None:
            self.lease.release()
        await self.store.close()
        # 同时关闭提供商的 HTTP 客户端；ModelPort 不一定定义 aclose
        # （ScriptedModel 就没有），因此这里只按协议尽力执行。
        closer = getattr(self.model, "aclose", None)
        if closer is not None:
            await closer()


def select_launcher(platform: str | None = None) -> SandboxLauncher | None:
    """选择操作系统沙箱后端；平台没有可用后端时返回 None。

    None 并不表示降级运行：策略会直接拒绝 repo.exec，因为本项目不会添加
    不受约束的通用执行能力。
    """
    target = platform if platform is not None else sys.platform
    if target == "darwin":
        return SeatbeltLauncher()
    if target.startswith("linux"):
        return LandlockLauncher()
    return None


def sandbox_backend_name(launcher: SandboxLauncher | None) -> str:
    return launcher.backend if launcher is not None and launcher.available() else "none"


def resolve_workspace(path: Path | None) -> Path:
    workspace = (path or Path.cwd()).resolve()
    if not workspace.is_dir():
        raise BootstrapError(f"workspace does not exist: {workspace}")
    return workspace


async def build_services(
    workspace_path: Path | None,
    *,
    mode: PermissionMode,
    approvals: ApprovalResponder,
    sinks: list[EventSinkPort],
    model: ModelPort | None = None,
    store_path: Path | None = None,
    tier: str | None = None,
) -> AppServices:
    """组装一个工作区的完整服务图；所有接口都通过此函数完成组装。

    顺序很重要：解析工作区 -> 加载分层配置 -> 获取写者租约（获取失败时可能
    降级为只读）-> 构造适配器（文件系统工作区、SQLite 存储，以及除非测试已
    注入模型否则创建提供商）-> 围绕这些适配器组装 RunService、RecoveryService
    和 ReplayService。测试会传入 `model=ScriptedModel(...)` 与临时
    `store_path`；返回的 AppServices.aclose() 会释放所有资源，包括租约。
    """
    workspace_root = resolve_workspace(workspace_path)
    config = load_config(workspace_root, tier)

    # 单写者租约：同一进程内的每次写入都固定并重新验证 preimage，但同一工作区
    # 中的第二个 Haven 进程可能在另一次运行审批和执行之间修改文件。第一个
    # 可写进程取得租约；竞争者会带着明确警告降级为只读，而不是直接拒绝。
    lease: WorkspaceLease | None = None
    lease_warning = ""
    if mode is not PermissionMode.READ_ONLY:
        try:
            lease = acquire_workspace_lease(workspace_root, data_dir() / "leases")
        except LeaseHeld as held:
            mode = PermissionMode.READ_ONLY
            lease_warning = f"{held}; this session is read-only"

    workspace = FsWorkspace(workspace_root)
    store = await SqliteSessionStore.open(store_path or db_path(), artifacts_dir())
    emitter = EventEmitter(store, sinks)

    if model is None:
        api_key = config.provider.api_key()
        if api_key is None:
            await store.close()
            raise BootstrapError(
                f"no API key found in ${config.provider.api_key_env}; "
                "set it or run offline commands only"
            )
        _profile = profile_for(config.provider.model)
        model = OpenAICompatibleModel(
            base_url=config.provider.base_url,
            api_key=api_key,
            model=config.provider.model,
            requires_tool_call_reasoning=_profile.requires_tool_call_reasoning,
            idle_timeout=_profile.stream_idle_timeout_s,
        )

    baseline = await capture_git_baseline(workspace_root)
    guidance = await _read_guidance(workspace)
    launcher = select_launcher()

    run_service = RunService(
        model=model,
        workspace=workspace,
        executor=ProcessExecutor(launcher=launcher),
        store=store,
        emitter=emitter,
        approvals=approvals,
        recipes=dict(config.recipes),
        mode=mode,
        budget=config.budget,
        pricing=config.pricing,
        git_branch=baseline.branch,
        git_commit=baseline.commit,
        project_guidance=guidance,
        launcher=launcher,
        # 前缀续写既需要能力也需要支持该能力的 endpoint；这里是唯一知道已配置
        # base URL 的位置。
        supports_prefix_continuation=profile_for(model.model_name).prefix_continuation_enabled(
            config.provider.base_url
        ),
    )
    return AppServices(
        config=config,
        workspace=workspace,
        store=store,
        emitter=emitter,
        run_service=run_service,
        recovery=RecoveryService(store, workspace),
        replay=ReplayService(store),
        git_branch=baseline.branch,
        git_commit=baseline.commit,
        model_name=model.model_name,
        model=model,
        sandbox_backend=sandbox_backend_name(launcher),
        lease=lease,
        lease_warning=lease_warning,
    )


#: Haven 认可的指令文件名，按拼接顺序排列。AGENTS.md 是标准文件；读取
#: CLAUDE.md 是为了兼容其他工具。
_GUIDANCE_FILENAMES = ("AGENTS.md", "CLAUDE.md")
#: 最多可以贡献作用域 AGENTS.md 的工作区子目录数量。设置上限是为了避免
#: 大型 monorepo 撑爆上下文，也让指导内容保持为一组小而可审计的文件，而
#: 不是无界遍历的结果。
_MAX_SCOPED_GUIDANCE = 6


async def _read_guidance(workspace: FsWorkspace) -> str:
    """合并有作用域的指令文件，先根目录，且全部视为不可信数据。

    Codex 和 opencode 都会把项目根目录的指导与更靠近代码的具体文件逐层
    合并；Haven 现在也采用相同方式，但设有边界：先读取根目录的
    AGENTS.md/CLAUDE.md，再读取少量子目录中的 AGENTS.md。每个文件都有独立
    标题，以便模型区分作用域。这些内容始终是不可信数据，不能改变权限，
    且合并结果总长度上限为 MAX_GUIDANCE_CHARS。
    """
    sections: list[str] = []
    for name in _GUIDANCE_FILENAMES:
        text = await _read_one_guidance(workspace, name)
        if text:
            sections.append(f"# {name} (repository root)\n{text}")

    scoped = 0
    for rel in _scoped_guidance_paths(workspace.root):
        if scoped >= _MAX_SCOPED_GUIDANCE:
            break
        text = await _read_one_guidance(workspace, rel)
        if text:
            sections.append(f"# {rel} (scoped to {rel.rsplit('/', 1)[0]}/)\n{text}")
            scoped += 1

    return "\n\n".join(sections)[:MAX_GUIDANCE_CHARS]


async def _read_one_guidance(workspace: FsWorkspace, rel: str) -> str:
    try:
        result = await workspace.read_file(rel, 1, 200)
    except WorkspaceError:
        return ""
    # 每个文件单独设置上限，避免某个大文件在总上限生效前挤掉其他文件。
    return result.content[: MAX_GUIDANCE_CHARS // 2].strip()


def _scoped_guidance_paths(root: Path) -> list[str]:
    """获取子目录中的 AGENTS.md 路径，按距离根目录从近到远排列，并跳过运行
    时绝不应读取指导的噪声目录。

    遍历时直接剪枝跳过的目录，而不是先遍历再过滤结果，因此不会进入庞大的
    node_modules 或 .venv 目录。
    """
    skip = {".git", ".haven", "node_modules", ".venv", "venv", "__pycache__", ".tox", "dist"}
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in skip)
        if "AGENTS.md" in filenames:
            rel = Path(dirpath).relative_to(root) / "AGENTS.md"
            if rel.as_posix() != "AGENTS.md":  # 根文件已读取
                found.append(rel.as_posix())
    # 先处理最接近根目录的目录（路径段最少），同一深度内保持稳定顺序。
    found.sort(key=lambda p: (p.count("/"), p))
    return found


async def open_store(store_path: Path | None = None) -> SqliteSessionStore:
    return await SqliteSessionStore.open(store_path or db_path(), artifacts_dir())


def make_workspace(path: Path) -> FsWorkspace:
    return FsWorkspace(path.resolve())


def build_provider(config: ResolvedConfig) -> OpenAICompatibleModel:
    api_key = config.provider.api_key()
    if api_key is None:
        raise BootstrapError(f"no API key found in ${config.provider.api_key_env}")
    profile = profile_for(config.provider.model)
    return OpenAICompatibleModel(
        base_url=config.provider.base_url,
        api_key=api_key,
        model=config.provider.model,
        requires_tool_call_reasoning=profile.requires_tool_call_reasoning,
        idle_timeout=profile.stream_idle_timeout_s,
    )


async def build_context_preview(
    workspace_path: Path | None, goal: str
) -> tuple[ModelRequest, tuple[ContextSegment, ...], ResolvedConfig]:
    """在不调用任何提供商的情况下，为目标组装第一轮 Context。

    这是 `haven debug-context` 的实现基础：使用实际的 ContextBuilder、配置和
    工作区指导，回答“模型会看到什么，以及原因是什么”。
    """
    workspace_root = resolve_workspace(workspace_path)
    config = load_config(workspace_root)
    guidance = await _read_guidance(FsWorkspace(workspace_root))
    builder = ContextBuilder(
        goal=goal,
        tools=tool_schemas(),
        budget=config.budget,
        recipe_ids=tuple(config.recipes),
        project_guidance=guidance,
    )
    request, segments = builder.build([], BudgetUsage())
    return request, segments, config
