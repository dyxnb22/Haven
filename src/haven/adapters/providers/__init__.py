"""模型提供商 adapters。提供商的线协议格式永远不会离开此包。"""

from haven.adapters.providers.openai_compatible import OpenAICompatibleModel
from haven.adapters.providers.scripted import ScriptedModel

__all__ = ["OpenAICompatibleModel", "ScriptedModel"]
