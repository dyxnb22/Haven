"""Composition-root wiring: what build_services opens, it must also close."""

from collections.abc import AsyncIterator
from pathlib import Path

from haven.application.approvals import AutoApprover
from haven.bootstrap import build_services
from haven.contracts.model import ModelEvent, ModelRequest, StreamFinished
from haven.domain.enums import PermissionMode


class ClosableModel:
    """A model port that records whether it was closed."""

    def __init__(self) -> None:
        self.closed = False

    @property
    def model_name(self) -> str:
        return "fake-model"

    async def _stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        yield StreamFinished(finish_reason="stop")

    def generate_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        return self._stream(request)

    async def aclose(self) -> None:
        self.closed = True


async def test_close_closes_the_provider_client(tmp_path: Path) -> None:
    model = ClosableModel()
    services = await build_services(
        tmp_path,
        mode=PermissionMode.READ_ONLY,
        approvals=AutoApprover("reject_all"),
        sinks=[],
        model=model,
        store_path=tmp_path / "haven.db",
    )
    await services.close()
    assert model.closed, "the provider client outlived AppServices.close()"


async def test_close_is_safe_for_a_model_without_aclose(tmp_path: Path) -> None:
    """A model port need not define aclose; closing must not blow up."""

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
