"""Composition root: the only module that knows both adapters and use cases.

Interfaces (CLI/TUI) receive fully wired services from here; they never import
adapters directly. Tests swap in ScriptedModel and MemorySessionStore instead.
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
    #: The single-writer lease when this process may mutate the workspace;
    #: None in read-only mode or when another live process holds it (in which
    #: case `lease_warning` says so and the mode was downgraded).
    lease: WorkspaceLease | None = None
    lease_warning: str = ""

    async def close(self) -> None:
        if self.lease is not None:
            self.lease.release()
        await self.store.close()
        # Close the provider's HTTP client too; a ModelPort need not define
        # aclose (ScriptedModel does not), so this is best-effort by protocol.
        closer = getattr(self.model, "aclose", None)
        if closer is not None:
            await closer()


def select_launcher(platform: str | None = None) -> SandboxLauncher | None:
    """Pick the OS sandbox backend, or None when the platform has none.

    None is not a degraded mode: the policy denies repo.exec outright, because
    an unconfined general exec is the one capability this project will not add.
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
    workspace_root = resolve_workspace(workspace_path)
    config = load_config(workspace_root, tier)

    # Single-writer lease: within one process every write is preimage-pinned
    # and re-verified, but a second Haven process on the same workspace could
    # mutate files between another run's approval and execution. The first
    # writable process takes the lease; a contender is downgraded to
    # read-only with an explicit warning rather than refused outright.
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
        model = OpenAICompatibleModel(
            base_url=config.provider.base_url,
            api_key=api_key,
            model=config.provider.model,
            requires_tool_call_reasoning=profile_for(
                config.provider.model
            ).requires_tool_call_reasoning,
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


#: Instruction filenames Haven honors, in the order they are concatenated.
#: AGENTS.md is the standard; CLAUDE.md is read for cross-tool compatibility.
_GUIDANCE_FILENAMES = ("AGENTS.md", "CLAUDE.md")
#: How many workspace subdirectories may contribute a scoped AGENTS.md. Bounded
#: so a large monorepo cannot blow up the context, and so guidance stays a
#: small, auditable set rather than an unbounded crawl.
_MAX_SCOPED_GUIDANCE = 6


async def _read_guidance(workspace: FsWorkspace) -> str:
    """Merge scoped instruction files, root first, all untrusted.

    Codex and opencode both layer a project's root guidance with more specific
    files nearer the code; Haven now does the same, bounded: the root
    AGENTS.md/CLAUDE.md, then a small number of subdirectory AGENTS.md files,
    each under its own header so the model can tell scope apart. It stays
    untrusted data that cannot change permissions, and the whole merge is
    capped at MAX_GUIDANCE_CHARS.
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
    # Each file is individually bounded so one large file cannot crowd out the
    # others before the overall cap applies.
    return result.content[: MAX_GUIDANCE_CHARS // 2].strip()


def _scoped_guidance_paths(root: Path) -> list[str]:
    """Subdirectory AGENTS.md paths, nearest-root first, skipping the noise
    directories a run never wants guidance from.

    Prunes skipped directories during the walk (rather than filtering results)
    so a large node_modules or .venv is never descended into at all.
    """
    skip = {".git", ".haven", "node_modules", ".venv", "venv", "__pycache__", ".tox", "dist"}
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in skip)
        if "AGENTS.md" in filenames:
            rel = Path(dirpath).relative_to(root) / "AGENTS.md"
            if rel.as_posix() != "AGENTS.md":  # root already read
                found.append(rel.as_posix())
    # Nearest the root first (fewest path segments), stable within a depth.
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
    return OpenAICompatibleModel(
        base_url=config.provider.base_url,
        api_key=api_key,
        model=config.provider.model,
        requires_tool_call_reasoning=profile_for(
            config.provider.model
        ).requires_tool_call_reasoning,
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
