import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, TypeVar

import httpx
import pytest

from cove_book_forge.config.models import ModelConfig
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.providers import ProviderUsage
from cove_book_forge.providers.openai_compatible import OpenAICompatibleProvider

T = TypeVar("T")


def run(coro: Awaitable[T]) -> T:
    return asyncio.run(coro)


def response_body(
    content: str = "answer",
    *,
    model: object = "served-model",
    finish_reason: object = "stop",
    usage: object = None,
) -> dict[str, object]:
    if usage is None:
        usage = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": usage,
    }


def make_provider(
    config: ModelConfig,
    handler: Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]],
    *,
    api_key: str | None = "provider-key",
) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        config=config,
        api_key=api_key,
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.parametrize(
    ("provider_name", "base_url", "expected_url"),
    [
        ("openai", None, "https://api.openai.com/v1/chat/completions"),
        ("deepseek", None, "https://api.deepseek.com/chat/completions"),
        (
            "openai-compatible",
            "https://gateway.example/api/v9/",
            "https://gateway.example/api/v9/chat/completions",
        ),
    ],
)
def test_text_generation_uses_exact_api_prefix_payload_and_authorization(
    provider_name: str,
    base_url: str | None,
    expected_url: str,
) -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "method": request.method,
                "url": str(request.url),
                "authorization": request.headers.get("authorization"),
                "content_type": request.headers.get("content-type"),
                "payload": json.loads(request.content),
            }
        )
        return httpx.Response(200, json=response_body())

    config = ModelConfig(
        provider=provider_name,
        model="configured-model",
        base_url=base_url,
    )
    provider = make_provider(config, handler)

    result = run(
        provider.generate_text(
            "You are a careful reader.",
            "Summarize the chapter.",
            max_output_tokens=321,
            temperature=0.25,
        )
    )

    assert result.text == "answer"
    assert result.model == "served-model"
    assert result.usage == ProviderUsage(
        input_tokens=11,
        output_tokens=7,
        total_tokens=18,
        request_count=1,
    )
    assert seen == [
        {
            "method": "POST",
            "url": expected_url,
            "authorization": "Bearer provider-key",
            "content_type": "application/json",
            "payload": {
                "model": "configured-model",
                "messages": [
                    {"role": "system", "content": "You are a careful reader."},
                    {"role": "user", "content": "Summarize the chapter."},
                ],
                "max_tokens": 321,
                "temperature": 0.25,
            },
        }
    ]


def test_empty_api_key_omits_authorization_header() -> None:
    authorization: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authorization.append(request.headers.get("authorization"))
        return httpx.Response(200, json=response_body())

    provider = make_provider(
        ModelConfig(provider="deepseek", model="reader"),
        handler,
        api_key="   ",
    )

    run(provider.generate_text("system", "user", max_output_tokens=10))

    assert authorization == [None]


def test_json_generation_requests_one_object_without_claiming_schema_support() -> None:
    payloads: list[Mapping[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=response_body(
                '{"themes":["attention"],"score":5}',
                model="json-model",
                usage={"prompt_tokens": 3, "completion_tokens": 4},
            ),
        )

    provider = make_provider(
        ModelConfig(
            provider="deepseek",
            model="deepseek-chat",
            default_max_output_tokens=8192,
        ),
        handler,
    )
    schema = {"type": "object", "required": ["themes"]}

    result = run(
        provider.generate_json(
            "Keep the caller system prompt.",
            "Analyze this text.",
            max_output_tokens=100,
            json_schema=schema,
        )
    )

    assert result.value == {"themes": ["attention"], "score": 5}
    assert result.usage.total_tokens == 7
    assert provider.capabilities.json_mode is True
    assert provider.capabilities.json_schema is False
    assert provider.capabilities.max_output_tokens is None
    assert payloads[0]["response_format"] == {"type": "json_object"}
    assert "json_schema" not in payloads[0]
    assert "schema" not in payloads[0]
    system_message = payloads[0]["messages"][0]["content"]
    assert system_message.startswith("Keep the caller system prompt.")
    assert "exactly one valid JSON object" in system_message


@pytest.mark.parametrize(
    ("provider_name", "base_url", "json_mode", "expected_native_mode"),
    [
        pytest.param("openai", None, None, True, id="openai-default-native"),
        pytest.param("deepseek", None, None, True, id="deepseek-default-native"),
        pytest.param(
            "openai-compatible",
            "https://gateway.example/v1",
            None,
            False,
            id="generic-default-prompt-only",
        ),
        pytest.param(
            "openai-compatible",
            "https://gateway.example/v1",
            True,
            True,
            id="generic-explicit-native",
        ),
        pytest.param("openai", None, False, False, id="openai-explicit-prompt-only"),
        pytest.param("deepseek", None, False, False, id="deepseek-explicit-prompt-only"),
    ],
)
def test_json_mode_capability_controls_native_request_shape(
    provider_name: str,
    base_url: str | None,
    json_mode: bool | None,
    expected_native_mode: bool,
) -> None:
    payloads: list[Mapping[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=response_body('{"ok":true}'))

    provider = make_provider(
        ModelConfig(
            provider=provider_name,
            model="reader",
            base_url=base_url,
            json_mode=json_mode,
        ),
        handler,
    )

    result = run(provider.generate_json("system", "user", max_output_tokens=10))

    assert result.value == {"ok": True}
    assert provider.capabilities.json_mode is expected_native_mode
    assert ("response_format" in payloads[0]) is expected_native_mode
    if expected_native_mode:
        assert payloads[0]["response_format"] == {"type": "json_object"}
    system_message = payloads[0]["messages"][0]["content"]
    assert "exactly one valid JSON object" in system_message


@pytest.mark.parametrize("max_output_tokens", [0, -1])
@pytest.mark.parametrize("generation_kind", ["text", "json"])
def test_non_positive_max_output_tokens_fail_closed_before_transport(
    max_output_tokens: int,
    generation_kind: str,
) -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, json=response_body())

    provider = make_provider(ModelConfig(provider="openai", model="reader"), handler)

    with pytest.raises(ForgeException) as caught:
        if generation_kind == "text":
            run(
                provider.generate_text(
                    "system",
                    "user",
                    max_output_tokens=max_output_tokens,
                )
            )
        else:
            run(
                provider.generate_json(
                    "system",
                    "user",
                    max_output_tokens=max_output_tokens,
                )
            )

    assert caught.value.code is ForgeErrorCode.MODEL_OUTPUT_INVALID
    assert caught.value.details == {}
    assert request_count == 0
    assert provider.usage == ProviderUsage()


def test_successful_generations_accumulate_usage_and_use_configured_model_fallback() -> None:
    bodies = [
        response_body("first", model="", usage={"prompt_tokens": 2, "completion_tokens": 1}),
        response_body(
            '{"second":true}',
            model=None,
            usage={"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        ),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=bodies.pop(0))

    provider = make_provider(ModelConfig(provider="openai", model="configured"), handler)

    first = run(provider.generate_text("system", "first", max_output_tokens=10))
    second = run(provider.generate_json("system", "second", max_output_tokens=10))

    assert first.model == "configured"
    assert second.model == "configured"
    assert provider.usage == ProviderUsage(
        input_tokens=7,
        output_tokens=4,
        total_tokens=11,
        request_count=2,
    )


def test_healthcheck_uses_models_endpoint_once_and_does_not_change_usage() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url)))
        return httpx.Response(200, json={"object": "list", "data": []})

    provider = make_provider(ModelConfig(provider="deepseek", model="reader"), handler)

    result = run(provider.healthcheck())

    assert result is None
    assert seen == [("GET", "https://api.deepseek.com/models")]
    assert provider.usage == ProviderUsage()


def test_each_public_method_performs_exactly_one_http_request() -> None:
    count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        if request.method == "GET":
            return httpx.Response(200, json={"object": "list", "data": []})
        content = '{"ok":true}' if count == 2 else "text"
        return httpx.Response(200, json=response_body(content))

    provider = make_provider(ModelConfig(provider="openai", model="reader"), handler)

    run(provider.generate_text("system", "user", max_output_tokens=10))
    assert count == 1
    run(provider.generate_json("system", "user", max_output_tokens=10))
    assert count == 2
    run(provider.healthcheck())
    assert count == 3


def test_openai_compatible_requires_an_explicit_base_url() -> None:
    config = ModelConfig(provider="openai-compatible", model="reader")

    with pytest.raises(ForgeException) as caught:
        OpenAICompatibleProvider(config=config, api_key=None)

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


@pytest.mark.parametrize(
    ("finish_reason", "omit_finish_reason"),
    [
        pytest.param("unused", True, id="missing"),
        pytest.param(None, False, id="null"),
        pytest.param(42, False, id="non-string"),
        pytest.param("", False, id="empty"),
        pytest.param("   ", False, id="blank"),
        pytest.param("length", False, id="truncated"),
        pytest.param("tool_calls", False, id="tool-continuation"),
        pytest.param("content_filter", False, id="filtered"),
        pytest.param("refusal", False, id="refusal"),
        pytest.param("unknown", False, id="unknown"),
    ],
)
def test_text_generation_accounts_for_usage_before_rejecting_invalid_finish_reason(
    finish_reason: object,
    omit_finish_reason: bool,
) -> None:
    body = response_body(finish_reason=finish_reason)
    if omit_finish_reason:
        choice = body["choices"][0]
        assert isinstance(choice, dict)
        choice.pop("finish_reason")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    provider = make_provider(ModelConfig(provider="openai", model="reader"), handler)

    with pytest.raises(ForgeException) as caught:
        run(provider.generate_text("system", "user", max_output_tokens=10))

    assert caught.value.code is ForgeErrorCode.MODEL_OUTPUT_INVALID
    assert provider.usage == ProviderUsage(
        input_tokens=11,
        output_tokens=7,
        total_tokens=18,
        request_count=1,
    )


@pytest.mark.parametrize(
    "base_url",
    [
        pytest.param("https://gateway.example/v1?tenant=private", id="query"),
        pytest.param("https://gateway.example/v1#private", id="fragment"),
    ],
)
def test_openai_compatible_rejects_query_or_fragment_before_request(base_url: str) -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, json=response_body())

    config = ModelConfig(
        provider="openai-compatible",
        model="reader",
        base_url=base_url,
    )

    with pytest.raises(ForgeException) as caught:
        make_provider(config, handler)

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
    assert request_count == 0
    assert base_url not in repr(caught.value.as_result())


@pytest.mark.parametrize(
    ("body", "expected_usage"),
    [
        pytest.param("not-json", ProviderUsage(), id="non-json-response"),
        pytest.param({}, ProviderUsage(), id="missing-usage"),
        pytest.param({"choices": []}, ProviderUsage(), id="missing-usage-and-choices"),
        pytest.param(
            {"choices": [{}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
            ProviderUsage(input_tokens=1, output_tokens=1, total_tokens=2, request_count=1),
            id="malformed-choice-with-trusted-usage",
        ),
        pytest.param(
            {
                "choices": [{"message": {"content": ""}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
            ProviderUsage(input_tokens=1, output_tokens=1, total_tokens=2, request_count=1),
            id="empty-content-with-trusted-usage",
        ),
        pytest.param(
            {
                "choices": [{"message": {"content": "answer"}, "finish_reason": "length"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
            ProviderUsage(input_tokens=1, output_tokens=1, total_tokens=2, request_count=1),
            id="incomplete-with-trusted-usage",
        ),
        pytest.param(
            response_body(usage={"prompt_tokens": -1, "completion_tokens": 1}),
            ProviderUsage(),
            id="negative-usage",
        ),
        pytest.param(
            response_body(usage={"prompt_tokens": True, "completion_tokens": 1}),
            ProviderUsage(),
            id="boolean-usage",
        ),
        pytest.param(
            response_body(usage={"prompt_tokens": 1, "completion_tokens": "1"}),
            ProviderUsage(),
            id="string-usage",
        ),
        pytest.param(
            response_body(usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 3}),
            ProviderUsage(),
            id="inconsistent-total",
        ),
    ],
)
def test_text_generation_records_only_trusted_usage_before_later_validation(
    body: object,
    expected_usage: ProviderUsage,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(body, str):
            return httpx.Response(200, text=body)
        return httpx.Response(200, json=body)

    provider = make_provider(ModelConfig(provider="openai", model="reader"), handler)

    with pytest.raises(ForgeException) as caught:
        run(provider.generate_text("system", "user", max_output_tokens=10))

    assert caught.value.code is ForgeErrorCode.MODEL_OUTPUT_INVALID
    assert caught.value.retryable is False
    assert caught.value.details == {}
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert provider.usage == expected_usage


@pytest.mark.parametrize("content", ["not json", "[]", "42", "null", '"text"'])
def test_json_generation_accounts_for_usage_before_rejecting_non_object_roots(
    content: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response_body(content))

    provider = make_provider(ModelConfig(provider="deepseek", model="reader"), handler)

    with pytest.raises(ForgeException) as caught:
        run(provider.generate_json("system", "user", max_output_tokens=10))

    assert caught.value.code is ForgeErrorCode.MODEL_OUTPUT_INVALID
    assert provider.usage == ProviderUsage(
        input_tokens=11,
        output_tokens=7,
        total_tokens=18,
        request_count=1,
    )


def test_provider_failures_do_not_leak_key_prompt_body_or_url() -> None:
    secret_key = "super-secret-key"
    private_prompt = "private book passage"
    private_body = "upstream secret response"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=private_body)

    config = ModelConfig(
        provider="openai-compatible",
        model="reader",
        base_url="https://private-gateway.example/v1",
    )
    provider = make_provider(config, handler, api_key=secret_key)

    with pytest.raises(ForgeException) as caught:
        run(provider.generate_text("private system", private_prompt, max_output_tokens=10))

    rendered = " ".join(
        (
            str(caught.value),
            repr(caught.value),
            repr(caught.value.details),
            repr(caught.value.as_result()),
        )
    )
    for private_value in (
        secret_key,
        private_prompt,
        private_body,
        "private-gateway",
        "private system",
    ):
        assert private_value not in rendered
