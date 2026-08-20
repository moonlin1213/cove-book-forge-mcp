import asyncio
from collections.abc import Mapping
from pathlib import Path

import httpx
import pytest

from cove_book_forge.config import ModelConfig
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.providers import (
    JsonGeneration,
    ProviderCapabilities,
    ProviderRegistry,
    ProviderUsage,
    TextGeneration,
)
from cove_book_forge.providers import transport as transport_module
from cove_book_forge.providers.anthropic import AnthropicProvider
from cove_book_forge.providers.openai_compatible import OpenAICompatibleProvider


class FakeProvider:
    def __init__(self, *, model: str, credential_present: bool) -> None:
        self.model = model
        self.credential_present = credential_present

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(json_mode=True)

    @property
    def usage(self) -> ProviderUsage:
        return ProviderUsage()

    async def generate_text(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_output_tokens: int,
        temperature: float | None = None,
    ) -> TextGeneration:
        return TextGeneration(
            text=user_prompt,
            model=self.model,
            usage=ProviderUsage(request_count=1),
        )

    async def generate_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_output_tokens: int,
        temperature: float | None = None,
        json_schema: Mapping[str, object] | None = None,
    ) -> JsonGeneration:
        return JsonGeneration(
            value={"prompt": user_prompt},
            model=self.model,
            usage=ProviderUsage(request_count=1),
        )

    async def healthcheck(self) -> None:
        return None


def fake_provider_factory(config: ModelConfig, api_key: str | None) -> FakeProvider:
    return FakeProvider(model=config.model, credential_present=api_key is not None)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


def _registry_provider(
    registry: ProviderRegistry,
    *,
    model: str,
    base_url: str = "https://gateway.example/v1",
    api_key_env: str | None = None,
    max_concurrency: int = 1,
    requests_per_minute: int = 20,
) -> OpenAICompatibleProvider:
    provider = registry.create(
        ModelConfig(
            provider="openai-compatible",
            model=model,
            base_url=base_url,
            api_key_env=api_key_env,
            max_concurrency=max_concurrency,
            requests_per_minute=requests_per_minute,
        )
    )
    assert isinstance(provider, OpenAICompatibleProvider)
    return provider


async def _maximum_healthcheck_concurrency(
    first: OpenAICompatibleProvider,
    second: OpenAICompatibleProvider,
) -> int:
    active = 0
    maximum = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        started.set()
        await release.wait()
        active -= 1
        return httpx.Response(200, json={"data": []})

    local_transport = httpx.MockTransport(handler)
    first._requester._transport = local_transport
    second._requester._transport = local_transport
    calls = [
        asyncio.create_task(first.healthcheck()),
        asyncio.create_task(second.healthcheck()),
    ]
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(*calls)
    return maximum


@pytest.mark.parametrize("name", ["openai", "openai-compatible", "deepseek", "anthropic"])
def test_registry_resolves_each_exact_builtin_without_importing_adapter(name: str) -> None:
    registry = ProviderRegistry()

    assert callable(registry.resolve(name))


def test_builtin_names_are_case_sensitive_and_unknown_provider_is_public_config_error() -> None:
    config = ModelConfig(provider="OpenAI", model="example")

    with pytest.raises(ForgeException) as caught:
        ProviderRegistry().create(config)

    assert caught.value.code is ForgeErrorCode.CONFIG_INVALID
    assert caught.value.as_result() == {
        "ok": False,
        "error": {
            "code": "CONFIG_INVALID",
            "message": "Configuration is invalid.",
            "retryable": False,
            "details": {"field": "model.provider"},
        },
    }


def test_registry_creates_an_explicit_custom_provider_without_retaining_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRIVATE_MODEL_KEY", "very-private-value")
    config = ModelConfig(
        provider="my-local-gateway",
        model="reader-model",
        api_key_env="PRIVATE_MODEL_KEY",
    )
    registry = ProviderRegistry({"my-local-gateway": fake_provider_factory})

    provider = registry.create(config)

    assert isinstance(provider, FakeProvider)
    assert provider.model == "reader-model"
    assert provider.credential_present is True
    assert "very-private-value" not in repr(provider)
    assert "very-private-value" not in config.model_dump_json()


def test_registry_copies_caller_factories_instead_of_using_mutable_global_state() -> None:
    factories = {"my-local-gateway": fake_provider_factory}
    registry = ProviderRegistry(factories)
    factories.clear()

    provider = registry.create(ModelConfig(provider="my-local-gateway", model="reader-model"))

    assert isinstance(provider, FakeProvider)
    assert provider.credential_present is False


def test_registry_builtin_instances_share_one_concurrency_limit_across_models() -> None:
    registry = ProviderRegistry()
    first = _registry_provider(registry, model="reader-a")
    second = _registry_provider(registry, model="reader-b")

    assert asyncio.run(_maximum_healthcheck_concurrency(first, second)) == 1


def test_registry_builtin_instances_share_one_sixty_second_request_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(transport_module.time, "monotonic", clock)
    monkeypatch.setattr(transport_module.asyncio, "sleep", clock.sleep)
    registry = ProviderRegistry()
    first = _registry_provider(registry, model="reader-a", requests_per_minute=2)
    second = _registry_provider(registry, model="reader-b", requests_per_minute=2)
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, json={"data": []})

    local_transport = httpx.MockTransport(handler)
    first._requester._transport = local_transport
    second._requester._transport = local_transport

    async def exercise() -> None:
        await first.healthcheck()
        await second.healthcheck()
        await first.healthcheck()

    asyncio.run(exercise())

    assert request_count == 3
    assert clock.sleeps == [60.0]


def test_registry_limits_are_isolated_by_registry_route_and_credential_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIRST_GATEWAY_KEY", "first-private-value")
    monkeypatch.setenv("SECOND_GATEWAY_KEY", "second-private-value")
    registry = ProviderRegistry()
    first = _registry_provider(
        registry,
        model="reader-a",
        api_key_env="FIRST_GATEWAY_KEY",
    )
    second = _registry_provider(
        registry,
        model="reader-b",
        base_url="https://other-gateway.example/v1",
        api_key_env="SECOND_GATEWAY_KEY",
    )

    assert asyncio.run(_maximum_healthcheck_concurrency(first, second)) == 2
    assert "first-private-value" not in repr(registry.__dict__)
    assert "second-private-value" not in repr(registry.__dict__)


def test_registry_limits_are_isolated_by_limit_settings_and_registry() -> None:
    registry = ProviderRegistry()
    first = _registry_provider(registry, model="reader-a", max_concurrency=1)
    different_limit = _registry_provider(registry, model="reader-b", max_concurrency=2)
    separate_registry = _registry_provider(ProviderRegistry(), model="reader-c")

    assert asyncio.run(_maximum_healthcheck_concurrency(first, different_limit)) == 2
    assert asyncio.run(_maximum_healthcheck_concurrency(first, separate_registry)) == 2


def test_registry_shared_limits_preserve_per_instance_usage() -> None:
    registry = ProviderRegistry()
    first = _registry_provider(registry, model="reader-a")
    second = _registry_provider(registry, model="reader-b")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "served",
                "choices": [
                    {
                        "message": {"content": "answer"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3},
            },
        )

    local_transport = httpx.MockTransport(handler)
    first._requester._transport = local_transport
    second._requester._transport = local_transport

    asyncio.run(first.generate_text("system", "first", max_output_tokens=10))
    asyncio.run(second.generate_text("system", "second", max_output_tokens=10))

    expected = ProviderUsage(
        input_tokens=2,
        output_tokens=3,
        total_tokens=5,
        request_count=1,
    )
    assert first.usage == expected
    assert second.usage == expected


@pytest.mark.parametrize("provider_name", ["openai", "deepseek", "anthropic"])
def test_cloud_builtin_requires_configured_credential_before_adapter_construction(
    provider_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructor_calls = 0

    def forbidden_construction(*_args: object, **_kwargs: object) -> None:
        nonlocal constructor_calls
        constructor_calls += 1
        raise AssertionError("adapter construction must follow credential validation")

    monkeypatch.setattr(OpenAICompatibleProvider, "__init__", forbidden_construction)
    monkeypatch.setattr(AnthropicProvider, "__init__", forbidden_construction)
    config = ModelConfig(
        provider=provider_name,
        model="private-model-name",
    )

    with pytest.raises(ForgeException) as caught:
        ProviderRegistry().create(config)

    assert caught.value.code is ForgeErrorCode.MODEL_AUTH_FAILED
    assert caught.value.details == {}
    assert constructor_calls == 0
    rendered = " ".join((str(caught.value), repr(caught.value), repr(caught.value.as_result())))
    assert provider_name not in rendered
    assert "private-model-name" not in rendered


@pytest.mark.parametrize("provider_name", ["openai", "deepseek", "anthropic"])
def test_resolved_builtin_factory_cannot_bypass_credential_policy(
    provider_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructor_calls = 0

    def forbidden_construction(*_args: object, **_kwargs: object) -> None:
        nonlocal constructor_calls
        constructor_calls += 1
        raise AssertionError("resolved factory must validate credentials first")

    monkeypatch.setattr(OpenAICompatibleProvider, "__init__", forbidden_construction)
    monkeypatch.setattr(AnthropicProvider, "__init__", forbidden_construction)
    factory = ProviderRegistry().resolve(provider_name)

    with pytest.raises(ForgeException) as caught:
        factory(ModelConfig(provider=provider_name, model="private-model"), None)

    assert caught.value.code is ForgeErrorCode.MODEL_AUTH_FAILED
    assert constructor_calls == 0


@pytest.mark.parametrize("value", [None, "", "   "])
def test_configured_missing_or_empty_api_key_is_a_safe_auth_error(
    value: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if value is None:
        monkeypatch.delenv("MISSING_MODEL_KEY", raising=False)
    else:
        monkeypatch.setenv("MISSING_MODEL_KEY", value)
    config = ModelConfig(
        provider="my-local-gateway",
        model="reader-model",
        api_key_env="MISSING_MODEL_KEY",
    )
    registry = ProviderRegistry({"my-local-gateway": fake_provider_factory})

    with pytest.raises(ForgeException) as caught:
        registry.create(config)

    assert caught.value.code is ForgeErrorCode.MODEL_AUTH_FAILED
    assert caught.value.details == {}
    rendered = str(caught.value.as_result())
    assert "MISSING_MODEL_KEY" not in rendered
    if value:
        assert value not in rendered


def test_public_custom_provider_example_can_be_registered_and_used_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(Path(__file__).parent.parent)
    from examples.custom_provider import custom_provider_factory

    config = ModelConfig(provider="deterministic-local", model="example-reader")
    provider = ProviderRegistry({"deterministic-local": custom_provider_factory}).create(config)

    text = asyncio.run(provider.generate_text("system", "chapter text", max_output_tokens=100))
    structured = asyncio.run(
        provider.generate_json(
            "system",
            "chapter text",
            max_output_tokens=100,
            json_schema={"type": "object"},
        )
    )
    asyncio.run(provider.healthcheck())

    assert text.text == "chapter text"
    assert text.model == "example-reader"
    assert structured.value == {"text": "chapter text"}
    assert structured.model == "example-reader"
    assert provider.capabilities.max_output_tokens is None
    assert provider.usage.request_count == 2
    assert provider.usage.total_tokens == (
        provider.usage.input_tokens + provider.usage.output_tokens
    )
