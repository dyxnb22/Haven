"""组合根组装：build_services 打开的资源也必须负责关闭。"""

from collections.abc import AsyncIterator
from pathlib import Path

from haven.application.approvals import AutoApprover
from haven.bootstrap import build_services
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
    scratch = services.run_service._scratch_dir  # noqa: SLF001
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

    services = await build_services(
        tmp_path,
        mode=PermissionMode.READ_ONLY,
        approvals=AutoApprover("reject_all"),
        sinks=[],
        model=Bare(),
        store_path=tmp_path / "haven.db",
    )
    await services.close()
