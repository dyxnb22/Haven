"""组合根组装：build_services 打开的资源也必须负责关闭。"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from haven.application.approvals import AutoApprover
from haven.application.skills import SkillSelectionError
from haven.bootstrap import build_context_preview, build_services
from haven.config import user_skills_dir
from haven.contracts.model import ModelEvent, ModelRequest, StreamFinished
from haven.domain.enums import PermissionMode


class ClosableModel:
    """记录自身是否已关闭的模型端口。"""

    def __init__(self, name: str = "fake-model") -> None:
        self.closed = False
        self.name = name

    @property
    def model_name(self) -> str:
        return self.name

    async def _stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        yield StreamFinished(finish_reason="stop")

    def generate_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        return self._stream(request)

    async def aclose(self) -> None:
        self.closed = True


async def test_close_closes_the_provider_client(tmp_path: Path) -> None:
    model = ClosableModel("deepseek-chat")
    services = await build_services(
        tmp_path,
        mode=PermissionMode.READ_ONLY,
        approvals=AutoApprover("reject_all"),
        sinks=[],
        model=model,
        store_path=tmp_path / "haven.db",
    )
    scratch = services.run_service._scratch_dir
    assert scratch.is_dir()
    assert services.config.pricing.is_known
    assert services.config.sources["pricing"] == "model-profile:deepseek-v4-flash"
    await services.close()
    assert model.closed, "the provider client outlived AppServices.close()"
    assert not scratch.exists(), "an unused RunService leaked its temporary directory"


async def test_close_is_safe_for_a_model_without_aclose(tmp_path: Path) -> None:
    """模型端口不必定义 aclose；关闭服务不能因此抛错。"""

    class Bare:
        @property
        def model_name(self) -> str:
            return "bare"

        def generate_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
            raise NotImplementedError

    model = Bare()
    # 前提可验证：这个端口确实没有 aclose。一旦 ModelPort 将来提供默认实现，
    # 没有这条断言，测试会在毫不知情的情况下变成空转。
    assert not hasattr(model, "aclose")

    services = await build_services(
        tmp_path,
        mode=PermissionMode.READ_ONLY,
        approvals=AutoApprover("reject_all"),
        sinks=[],
        model=model,
        store_path=tmp_path / "haven.db",
    )
    await services.close()  # 能返回即是断言


MINIMAL_SKILL = "---\nname: demo\ndescription: 先写失败测试\n---\n\n技能正文。\n"


def _write_user_skill(name: str = "demo", text: str = MINIMAL_SKILL) -> None:
    """写入用户级技能根目录；conftest 已把 HAVEN_CONFIG_DIR 指向临时目录。"""
    directory = user_skills_dir() / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(text, encoding="utf-8")


def _write_project_skill(root: Path, name: str = "team") -> None:
    directory = root / ".haven" / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(MINIMAL_SKILL.replace("demo", name), encoding="utf-8")


async def test_build_services_exposes_the_discovered_skill_catalog(tmp_path: Path) -> None:
    _write_user_skill()
    _write_project_skill(tmp_path)
    services = await build_services(
        tmp_path,
        mode=PermissionMode.READ_ONLY,
        approvals=AutoApprover("reject_all"),
        sinks=[],
        model=ClosableModel(),
        store_path=tmp_path / "haven.db",
    )
    assert services.skills.names() == ("demo", "team")
    assert services.run_service is not None
    await services.close()


async def test_a_refused_skill_is_reported_not_swallowed(tmp_path: Path) -> None:
    """失败即关闭必须可见：一个坏技能不能让整个发现静默返回空。"""
    _write_user_skill("broken", "---\nname: broken\n---\n\n没有 description\n")
    services = await build_services(
        tmp_path,
        mode=PermissionMode.READ_ONLY,
        approvals=AutoApprover("reject_all"),
        sinks=[],
        model=ClosableModel(),
        store_path=tmp_path / "haven.db",
    )
    assert services.skills.skills == ()
    assert [(r.source, r.reason) for r in services.skills.refused] == [
        ("skills/broken/SKILL.md", "missing 'description'")
    ]
    await services.close()


async def test_build_context_preview_includes_the_catalog_segment(tmp_path: Path) -> None:
    _write_user_skill()
    _write_project_skill(tmp_path)
    _, segments, _ = await build_context_preview(tmp_path, "fix the parser test")
    sources = [s.source for s in segments]
    assert "skill_catalog" in sources
    catalog = next(s for s in segments if s.source == "skill_catalog")
    assert catalog.trust == "untrusted"
    # 目录出现，但正文没有：用户没有选择任何技能。
    assert not any(source.startswith("skill:") for source in sources)


async def test_build_context_preview_with_a_selected_skill_adds_its_body(tmp_path: Path) -> None:
    _write_user_skill()
    request, segments, _ = await build_context_preview(
        tmp_path, "fix the parser test", skill_names=["demo"]
    )
    assert "skill:demo" in [s.source for s in segments]
    assert "技能正文。" in next(m for m in request.messages if "技能正文。" in m.content).content


async def test_build_context_preview_rejects_an_unknown_skill(tmp_path: Path) -> None:
    """预览必须和真实运行一样拒绝，否则 debug-context 会显示一个不可能存在的上下文。"""
    _write_user_skill()
    with pytest.raises(SkillSelectionError, match="unknown skill 'nope'"):
        await build_context_preview(tmp_path, "fix the parser test", skill_names=["nope"])
