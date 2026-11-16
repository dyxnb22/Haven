"""绑定本次运行的模型，复用同一 Provider 的客户端。"""

import json
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import asdict

from haven.application.model_router import ModelRouter
from haven.application.profiles import ModelProfile, provider_profile_for
from haven.contracts.model_call import ModelSelection, ModelSpec
from haven.domain.digest import sha256_text
from haven.domain.pricing import Pricing
from haven.ports.model import ModelPort


class ModelCatalog:
    def __init__(
        self,
        *,
        default: ModelSpec,
        presets: dict[str, ModelSpec],
        base_url: str,
        pricing_override: Pricing | None,
        create: Callable[[str, ModelProfile], ModelPort],
    ) -> None:
        self._presets = dict(presets)
        self._base_url = base_url.rstrip("/")
        pricing = None if pricing_override is None else asdict(pricing_override)
        self._provider_digest = sha256_text(json.dumps([self._base_url, pricing], sort_keys=True))
        self._default = ModelSelection(spec=default, provider_digest=self._provider_digest)
        self._create = create
        self._ports: dict[str, ModelPort] = {}
        self._clients = AsyncExitStack()

    def select(self, name: str | None) -> ModelSelection:
        if not name:
            return self._default
        if name not in self._presets:
            raise ValueError(f"unknown model preset {name!r}; see haven config explain")
        return ModelSelection(
            spec=self._presets[name], preset_name=name, provider_digest=self._provider_digest
        )

    def restore(self, saved: ModelSelection | None) -> ModelSelection:
        if saved is None:
            return self._default
        if saved.provider_digest != self._provider_digest:
            raise ValueError(
                "provider endpoint or configured pricing changed since this run; "
                "restore the original settings before resuming"
            )
        return saved

    def profile_for(self, name: str) -> ModelProfile:
        return provider_profile_for(name, self._base_url)

    def supports_prefix(self, selection: ModelSelection) -> bool:
        # 下一轮有可能切换到另一轨，只有所有候选都能接收前缀时才发送 prefix。
        return all(
            self.profile_for(name).prefix_continuation_enabled(self._base_url)
            for name in selection.spec.model_names
        )

    def bind(self, selection: ModelSelection) -> ModelPort:
        spec = selection.spec
        model = self._port(spec.model)
        if spec.fast_model:
            return ModelRouter(self._port(spec.fast_model), model, fast_turns=spec.fast_turns)
        return model

    def _port(self, name: str) -> ModelPort:
        if name not in self._ports:
            port = self._create(name, self.profile_for(name))
            if callable(closer := getattr(port, "aclose", None)):
                self._clients.push_async_callback(closer)
            self._ports[name] = port
        return self._ports[name]

    async def close(self) -> None:
        self._ports.clear()
        await self._clients.aclose()
