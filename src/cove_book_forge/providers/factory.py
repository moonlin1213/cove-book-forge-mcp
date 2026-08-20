import asyncio
import time
from collections.abc import Callable, Mapping
from importlib import import_module
from types import MappingProxyType
from typing import TypeAlias, cast
from urllib.parse import urlsplit

from cove_book_forge.config.models import ModelConfig
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.providers.base import ModelProvider
from cove_book_forge.providers.credentials import resolve_provider_credential
from cove_book_forge.providers.transport import _RequestLimits

ProviderFactory: TypeAlias = Callable[[ModelConfig, str | None], ModelProvider]


def _lazy_provider_factory(module_name: str, class_name: str) -> ProviderFactory:
    def create(config: ModelConfig, api_key: str | None) -> ModelProvider:
        del api_key
        resolved_api_key = resolve_provider_credential(config)
        provider_class = getattr(import_module(module_name), class_name)
        return cast(ModelProvider, provider_class(config=config, api_key=resolved_api_key))

    return create


_OPENAI_COMPATIBLE_FACTORY = _lazy_provider_factory(
    "cove_book_forge.providers.openai_compatible",
    "OpenAICompatibleProvider",
)
_ANTHROPIC_FACTORY = _lazy_provider_factory(
    "cove_book_forge.providers.anthropic",
    "AnthropicProvider",
)
_BUILTIN_CLASSES = {
    "openai": (
        "cove_book_forge.providers.openai_compatible",
        "OpenAICompatibleProvider",
    ),
    "openai-compatible": (
        "cove_book_forge.providers.openai_compatible",
        "OpenAICompatibleProvider",
    ),
    "deepseek": (
        "cove_book_forge.providers.openai_compatible",
        "OpenAICompatibleProvider",
    ),
    "anthropic": ("cove_book_forge.providers.anthropic", "AnthropicProvider"),
}
_BUILTIN_FACTORIES: Mapping[str, ProviderFactory] = MappingProxyType(
    {
        "openai": _OPENAI_COMPATIBLE_FACTORY,
        "openai-compatible": _OPENAI_COMPATIBLE_FACTORY,
        "deepseek": _OPENAI_COMPATIBLE_FACTORY,
        "anthropic": _ANTHROPIC_FACTORY,
    }
)

_DEFAULT_ROUTES = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com",
    "anthropic": "https://api.anthropic.com",
}


def _route_identity(config: ModelConfig) -> tuple[object, ...]:
    raw_route = (
        str(config.base_url)
        if config.base_url is not None
        else _DEFAULT_ROUTES.get(config.provider, "")
    )
    parsed = urlsplit(raw_route)
    route = (
        parsed.scheme.lower(),
        (parsed.hostname or "").lower(),
        parsed.port,
        parsed.path.rstrip("/"),
    )
    protocol = "anthropic" if config.provider == "anthropic" else "openai-compatible"
    return (
        protocol,
        route,
        config.api_key_env,
        config.max_concurrency,
        config.requests_per_minute,
    )


def _create_builtin(
    config: ModelConfig,
    api_key: str | None,
    request_limits: _RequestLimits,
) -> ModelProvider:
    module_name, class_name = _BUILTIN_CLASSES[config.provider]
    provider_class = getattr(import_module(module_name), class_name)
    return cast(
        ModelProvider,
        provider_class(
            config=config,
            api_key=api_key,
            request_limits=request_limits,
        ),
    )


class ProviderRegistry:
    def __init__(self, custom_factories: Mapping[str, ProviderFactory] | None = None) -> None:
        custom = dict(custom_factories or {})
        conflicts = custom.keys() & _BUILTIN_FACTORIES.keys()
        if conflicts:
            raise ValueError("custom provider factories cannot replace built-in providers")
        self._custom_factories = custom
        self._request_limits: dict[tuple[object, ...], _RequestLimits] = {}

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
        api_key = resolve_provider_credential(config)
        if config.provider in _BUILTIN_FACTORIES:
            identity = _route_identity(config)
            request_limits = self._request_limits.get(identity)
            if request_limits is None:
                request_limits = _RequestLimits(
                    max_concurrency=config.max_concurrency,
                    requests_per_minute=config.requests_per_minute,
                    clock=time.monotonic,
                    sleep=asyncio.sleep,
                )
                self._request_limits[identity] = request_limits
            return _create_builtin(config, api_key, request_limits)
        return factory(config, api_key)
