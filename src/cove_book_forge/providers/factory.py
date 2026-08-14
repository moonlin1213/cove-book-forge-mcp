import os
from collections.abc import Callable, Mapping
from importlib import import_module
from types import MappingProxyType
from typing import TypeAlias, cast

from cove_book_forge.config.models import ModelConfig
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.providers.base import ModelProvider

ProviderFactory: TypeAlias = Callable[[ModelConfig, str | None], ModelProvider]


def _lazy_provider_factory(module_name: str, class_name: str) -> ProviderFactory:
    def create(config: ModelConfig, api_key: str | None) -> ModelProvider:
        provider_class = getattr(import_module(module_name), class_name)
        return cast(ModelProvider, provider_class(config=config, api_key=api_key))

    return create


_OPENAI_COMPATIBLE_FACTORY = _lazy_provider_factory(
    "cove_book_forge.providers.openai_compatible",
    "OpenAICompatibleProvider",
)
_ANTHROPIC_FACTORY = _lazy_provider_factory(
    "cove_book_forge.providers.anthropic",
    "AnthropicProvider",
)
_BUILTIN_FACTORIES: Mapping[str, ProviderFactory] = MappingProxyType(
    {
        "openai": _OPENAI_COMPATIBLE_FACTORY,
        "openai-compatible": _OPENAI_COMPATIBLE_FACTORY,
        "deepseek": _OPENAI_COMPATIBLE_FACTORY,
        "anthropic": _ANTHROPIC_FACTORY,
    }
)


class ProviderRegistry:
    def __init__(self, custom_factories: Mapping[str, ProviderFactory] | None = None) -> None:
        custom = dict(custom_factories or {})
        conflicts = custom.keys() & _BUILTIN_FACTORIES.keys()
        if conflicts:
            raise ValueError("custom provider factories cannot replace built-in providers")
        self._custom_factories = custom

    def resolve(self, provider_name: str) -> ProviderFactory:
        factory = _BUILTIN_FACTORIES.get(provider_name)
        if factory is None:
            factory = self._custom_factories.get(provider_name)
        if factory is None:
            raise ForgeException(
                ForgeErrorCode.CONFIG_INVALID,
                "Unknown model provider.",
                details={"field": "model.provider"},
            )
        return factory

    def create(self, config: ModelConfig) -> ModelProvider:
        factory = self.resolve(config.provider)
        api_key = self._resolve_api_key(config.api_key_env)
        return factory(config, api_key)

    @staticmethod
    def _resolve_api_key(api_key_env: str | None) -> str | None:
        if api_key_env is None:
            return None
        value = os.environ.get(api_key_env)
        if value is None or not value.strip():
            raise ForgeException(
                ForgeErrorCode.MODEL_AUTH_FAILED,
                "Configured model credential is unavailable.",
            )
        return value
