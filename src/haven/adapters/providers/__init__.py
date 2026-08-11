"""Model provider adapters. Provider wire formats never leave this package."""

from haven.adapters.providers.openai_compatible import OpenAICompatibleModel
from haven.adapters.providers.scripted import ScriptedModel

__all__ = ["OpenAICompatibleModel", "ScriptedModel"]
