from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping

import httpx
import pytest

from cove_book_forge.config import ModelConfig
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.providers import ProviderRegistry
from cove_book_forge.providers.anthropic import AnthropicProvider
from cove_book_forge.providers.openai_compatible import OpenAICompatibleProvider
from cove_book_forge.providers.transport import ProviderTransport


def _openai_response(*, model: str, content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-local-fixture",
            "object": "chat.completion",
            "created": 1,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
        },
    )


def _anthropic_response(*, model: str, content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg_local_fixture",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": content}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 2, "output_tokens": 3},
        },
    )


@pytest.mark.parametrize(
    ("provider_name", "base_url", "expected_type", "expected_prefix"),
    [
        ("openai", None, OpenAICompatibleProvider, "https://api.openai.com/v1"),
        (
            "openai-compatible",
            "https://local-gateway.invalid/v1",
            OpenAICompatibleProvider,
            "https://local-gateway.invalid/v1",
        ),
        ("deepseek", None, OpenAICompatibleProvider, "https://api.deepseek.com"),
        ("anthropic", None, AnthropicProvider, "https://api.anthropic.com"),
    ],
)
def test_registry_builtins_complete_text_and_json_without_fallback(
    provider_name: str,
    base_url: str | None,
    expected_type: type[OpenAICompatibleProvider] | type[AnthropicProvider],
    expected_prefix: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INTEGRATION_MODEL_KEY", "offline-test-credential")
    config = ModelConfig(
        provider=provider_name,
        model=f"configured-{provider_name}",
        base_url=base_url,
        api_key_env="INTEGRATION_MODEL_KEY",
    )
    requests: list[tuple[str, str, Mapping[str, object] | None]] = []

    async def local_request(
        _transport: ProviderTransport,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json: Mapping[str, object] | None = None,
    ) -> httpx.Response:
        del headers
        requests.append((method, url, json))
        is_json = bool(
            json
            and (
                "response_format" in json
                or "Return exactly one valid JSON object" in str(json.get("system", ""))
            )
        )
        content = '{"kind":"json"}' if is_json else "offline text"
        served_model = f"served-{provider_name}"
        if provider_name == "anthropic":
            return _anthropic_response(model=served_model, content=content)
        return _openai_response(model=served_model, content=content)

    monkeypatch.setattr(ProviderTransport, "request", local_request)

    provider = ProviderRegistry().create(config)
    text = asyncio.run(
        provider.generate_text("system instructions", "chapter body", max_output_tokens=64)
    )
    structured = asyncio.run(
        provider.generate_json("system instructions", "chapter body", max_output_tokens=64)
    )

    assert type(provider) is expected_type
    assert text.text == "offline text"
    assert text.model == f"served-{provider_name}"
    assert text.usage.model_dump() == {
        "input_tokens": 2,
        "output_tokens": 3,
        "total_tokens": 5,
        "request_count": 1,
    }
    assert structured.value == {"kind": "json"}
    assert structured.model == f"served-{provider_name}"
    assert structured.usage == text.usage
    assert provider.usage.model_dump() == {
        "input_tokens": 4,
        "output_tokens": 6,
        "total_tokens": 10,
        "request_count": 2,
    }
    assert len(requests) == 2
    assert all(method == "POST" for method, _, _ in requests)
    assert all(url.startswith(expected_prefix) for _, url, _ in requests)


def test_registry_invalid_output_error_and_repr_are_closed_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment_name = "PRIVATE_INTEGRATION_MODEL_KEY"
    secret = "private-environment-value"
    system_prompt = "private system instructions"
    user_prompt = "private unpublished chapter"
    raw_response = "private malformed response body"
    private_url = "https://private-gateway.invalid/hidden-api"
    monkeypatch.setenv(environment_name, secret)
    request_count = 0

    async def invalid_response(
        _transport: ProviderTransport,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json: Mapping[str, object] | None = None,
    ) -> httpx.Response:
        nonlocal request_count
        del method, url, headers, json
        request_count += 1
        return httpx.Response(200, content=raw_response.encode())

    monkeypatch.setattr(ProviderTransport, "request", invalid_response)
    provider = ProviderRegistry().create(
        ModelConfig(
            provider="openai-compatible",
            model="private-model-name",
            base_url=private_url,
            api_key_env=environment_name,
        )
    )

    with pytest.raises(ForgeException) as caught:
        asyncio.run(
            provider.generate_text(
                system_prompt,
                user_prompt,
                max_output_tokens=64,
            )
        )

    assert caught.value.code is ForgeErrorCode.MODEL_OUTPUT_INVALID
    assert request_count == 1
    rendered = "\n".join(
        (
            json.dumps(caught.value.as_result(), sort_keys=True),
            repr(caught.value),
            repr(provider),
        )
    )
    for private_value in (
        secret,
        environment_name,
        system_prompt,
        user_prompt,
        raw_response,
        private_url,
    ):
        assert private_value not in rendered
