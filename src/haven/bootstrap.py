"""Composition root: the only module that knows both adapters and use cases.

Interfaces (CLI/TUI) receive fully wired services from here; they never import
adapters directly. Tests swap in ScriptedModel and MemorySessionStore instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from haven.adapters.git_baseline import capture_git_baseline
from haven.adapters.process_executor import ProcessExecutor
from haven.adapters.providers.openai_compatible import OpenAICompatibleModel
from haven.adapters.sqlite_session import SqliteSessionStore
from haven.adapters.workspace_fs import FsWorkspace
from haven.application.approvals import ApprovalResponder
from haven.application.context_builder import ContextBuilder
from haven.application.emitter import EventEmitter
from haven.application.recovery_service import RecoveryService
from haven.application.replay_service import ReplayService
from haven.application.run_service import RunService
from haven.config import ResolvedConfig, artifacts_dir, db_path, load_config
from haven.contracts.events import ContextSegment
from haven.contracts.model import ModelRequest
from haven.contracts.tools import tool_schemas
from haven.domain.budget import BudgetUsage
from haven.domain.enums import PermissionMode
from haven.ports.event_sink import EventSinkPort
from haven.ports.model import ModelPort
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

    async def close(self) -> None:
        await self.store.close()


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
) -> AppServices:
    workspace_root = resolve_workspace(workspace_path)
    config = load_config(workspace_root)

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
        model = OpenAICompatibleModel(
            base_url=config.provider.base_url,
            api_key=api_key,
            model=config.provider.model,
        )

    baseline = await capture_git_baseline(workspace_root)
    guidance = await _read_guidance(workspace)

    run_service = RunService(
        model=model,
        workspace=workspace,
        executor=ProcessExecutor(),
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
    )


async def _read_guidance(workspace: FsWorkspace) -> str:
    """AGENTS.md is untrusted guidance; it is included in context but can
    never change permissions."""
    try:
        result = await workspace.read_file("AGENTS.md", 1, 200)
    except WorkspaceError:
        return ""
    return result.content[:MAX_GUIDANCE_CHARS]


async def open_store(store_path: Path | None = None) -> SqliteSessionStore:
    return await SqliteSessionStore.open(store_path or db_path(), artifacts_dir())


def make_workspace(path: Path) -> FsWorkspace:
    return FsWorkspace(path.resolve())


def build_provider(config: ResolvedConfig) -> OpenAICompatibleModel:
    api_key = config.provider.api_key()
    if api_key is None:
        raise BootstrapError(f"no API key found in ${config.provider.api_key_env}")
    return OpenAICompatibleModel(
        base_url=config.provider.base_url,
        api_key=api_key,
        model=config.provider.model,
    )


async def build_context_preview(
    workspace_path: Path | None, goal: str
) -> tuple[ModelRequest, tuple[ContextSegment, ...], ResolvedConfig]:
    """Assemble the first-turn Context for a goal without calling any provider.

    Backs `haven debug-context`: it answers "what would the model see, and why"
    using the real ContextBuilder, config, and workspace guidance.
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
