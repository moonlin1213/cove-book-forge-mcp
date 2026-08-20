import inspect

import pytest
from pydantic import ValidationError

from cove_book_forge.config.models import ModelConfig
from cove_book_forge.providers import (
    JsonGeneration,
    ModelProvider,
    ProviderCapabilities,
    ProviderUsage,
    TextGeneration,
)


class ExampleProvider:
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
            text=f"{system_prompt}:{user_prompt}",
            model="example",
            usage=ProviderUsage(request_count=1),
        )

    async def generate_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_output_tokens: int,
        temperature: float | None = None,
        json_schema: dict[str, object] | None = None,
    ) -> JsonGeneration:
        return JsonGeneration(
            value={"ok": True},
            model="example",
            usage=ProviderUsage(request_count=1),
        )

    async def healthcheck(self) -> None:
        return None


def test_provider_contracts_are_strict_frozen_and_reject_extra_fields() -> None:
    capabilities = ProviderCapabilities(
        json_mode=True,
        json_schema=True,
        max_context_tokens=128_000,
        max_output_tokens=8_192,
        reasoning_parameters=("effort",),
    )
    usage = ProviderUsage(
        input_tokens=11,
        output_tokens=7,
        total_tokens=18,
        request_count=1,
    )
    text = TextGeneration(text="answer", model="example", usage=usage)
    structured = JsonGeneration(value={"answer": 42}, model="example", usage=usage)

    assert capabilities.reasoning_parameters == ("effort",)
    assert text.usage.total_tokens == 18
    assert structured.value == {"answer": 42}

    with pytest.raises(ValidationError):
        ProviderCapabilities.model_validate({"json_mode": 1})
    with pytest.raises(ValidationError):
        ProviderUsage.model_validate({"input_tokens": 0, "unexpected": True})
    with pytest.raises(ValidationError):
        TextGeneration.model_validate(
            {"text": "answer", "model": "example", "usage": usage, "x": 1}
        )
    with pytest.raises(ValidationError):
        structured.model = "changed"


@pytest.mark.parametrize(
    ("input_tokens", "output_tokens", "total_tokens", "request_count"),
    [
        (-1, 0, -1, 0),
        (0, -1, -1, 0),
        (0, 0, 0, -1),
        (2, 3, 6, 1),
    ],
)
def test_provider_usage_rejects_negative_or_inconsistent_counts(
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    request_count: int,
) -> None:
    with pytest.raises(ValidationError):
        ProviderUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            request_count=request_count,
        )


def test_model_provider_protocol_exposes_async_generation_boundary() -> None:
    provider = ExampleProvider()

    assert isinstance(provider, ModelProvider)
    assert inspect.iscoroutinefunction(ModelProvider.generate_text)
    assert inspect.iscoroutinefunction(ModelProvider.generate_json)
    assert inspect.iscoroutinefunction(ModelProvider.healthcheck)


def test_common_model_settings_have_bounded_backward_compatible_defaults() -> None:
    config = ModelConfig(provider="openai-compatible", model="local-model")

    assert config.request_timeout_seconds == 60.0
    assert config.default_max_output_tokens == 4_096
    assert config.json_mode is None

    with pytest.raises(ValidationError):
        ModelConfig(
            provider="openai-compatible",
            model="local-model",
            request_timeout_seconds=0,
        )
    with pytest.raises(ValidationError):
        ModelConfig(
            provider="openai-compatible",
            model="local-model",
            default_max_output_tokens=0,
        )
