from collections.abc import Mapping

import pytest

from cove_book_forge.config.models import ModelConfig
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.providers import (
    JsonGeneration,
    ProviderCapabilities,
    ProviderRegistry,
    ProviderUsage,
    TextGeneration,
)


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
