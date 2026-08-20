import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import TypeVar

import httpx
import pytest

from cove_book_forge.config import ModelConfig
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.providers import ProviderUsage
from cove_book_forge.providers.anthropic import AnthropicProvider

T = TypeVar("T")


def run(coro: Awaitable[T]) -> T:
    return asyncio.run(coro)


def response_body(
    content: object = None,
    *,
    model: object = "served-claude",
    stop_reason: object = "end_turn",
    usage: object = None,
) -> dict[str, object]:
    if content is None:
        content = [{"type": "text", "text": "answer"}]
    if usage is None:
        usage = {"input_tokens": 11, "output_tokens": 7}
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage,
    }


def make_provider(
    config: ModelConfig,
    handler: Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]],
    *,
    api_key: str | None = "anthropic-key",
) -> AnthropicProvider:
    return AnthropicProvider(
        config=config,
        api_key=api_key,
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.parametrize(
    ("base_url", "expected_url"),
    [
        (None, "https://api.anthropic.com/v1/messages"),
        ("https://claude.example/proxy/", "https://claude.example/proxy/v1/messages"),
    ],
)
def test_text_generation_uses_exact_base_headers_and_anthropic_payload(
    base_url: str | None,
    expected_url: str,
) -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "method": request.method,
                "url": str(request.url),
                "api_key": request.headers.get("x-api-key"),
                "version": request.headers.get("anthropic-version"),
                "content_type": request.headers.get("content-type"),
                "payload": json.loads(request.content),
            }
        )
        return httpx.Response(200, json=response_body())

    provider = make_provider(
        ModelConfig(provider="anthropic", model="configured-claude", base_url=base_url),
        handler,
    )

    result = run(
        provider.generate_text(
            "You are a careful reader.",
            "Summarize the chapter.",
            max_output_tokens=321,
            temperature=0.25,
        )
    )

    assert result.text == "answer"
    assert result.model == "served-claude"
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
            "api_key": "anthropic-key",
            "version": "2023-06-01",
            "content_type": "application/json",
            "payload": {
                "model": "configured-claude",
                "max_tokens": 321,
                "system": "You are a careful reader.",
                "messages": [{"role": "user", "content": "Summarize the chapter."}],
                "temperature": 0.25,
            },
        }
    ]


def test_empty_api_key_is_omitted_and_temperature_is_optional() -> None:
    seen: list[tuple[str | None, Mapping[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.headers.get("x-api-key"), json.loads(request.content)))
        return httpx.Response(200, json=response_body())

    provider = make_provider(
        ModelConfig(provider="anthropic", model="claude"),
        handler,
        api_key="   ",
    )

    run(provider.generate_text("system", "user", max_output_tokens=10))

    api_key, payload = seen[0]
    assert api_key is None
    assert "temperature" not in payload


@pytest.mark.parametrize(
    "base_url",
    [
        pytest.param("https://claude.example/api?tenant=private", id="query"),
        pytest.param("https://claude.example/api#private", id="fragment"),
    ],
)
def test_query_or_fragment_api_base_is_rejected_before_request(base_url: str) -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, json=response_body())

    with pytest.raises(ForgeException) as caught:
        make_provider(
            ModelConfig(provider="anthropic", model="claude", base_url=base_url),
            handler,
        )

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


def test_text_blocks_are_collected_in_order_and_non_text_blocks_are_ignored() -> None:
    body = response_body(
        [
            {"type": "text", "text": "first"},
            {"type": "tool_use", "id": "tool_1", "name": "lookup", "input": {}},
            {"type": "text", "text": ""},
            {"type": "text", "text": " second"},
        ],
        model="",
        usage={"input_tokens": 2, "output_tokens": 3},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    provider = make_provider(
        ModelConfig(provider="anthropic", model="configured-claude"),
        handler,
    )

    result = run(provider.generate_text("system", "user", max_output_tokens=10))

    assert result.text == "first second"
    assert result.model == "configured-claude"


@pytest.mark.parametrize("stop_reason", ["end_turn", "stop_sequence"])
def test_protocol_complete_stop_reasons_succeed(stop_reason: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response_body(stop_reason=stop_reason))

    provider = make_provider(ModelConfig(provider="anthropic", model="claude"), handler)

    result = run(provider.generate_text("system", "user", max_output_tokens=10))

    assert result.text == "answer"
    assert provider.usage.request_count == 1


def test_successful_calls_accumulate_usage_while_healthcheck_does_not() -> None:
    calls: list[tuple[str, str]] = []
    bodies = [
        response_body(
            [{"type": "text", "text": "first"}],
            usage={"input_tokens": 2, "output_tokens": 1},
        ),
        response_body(
            [{"type": "text", "text": '{"second":true}'}],
            usage={"input_tokens": 5, "output_tokens": 3},
        ),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json=bodies.pop(0))

    provider = make_provider(ModelConfig(provider="anthropic", model="claude"), handler)

    first = run(provider.generate_text("system", "first", max_output_tokens=10))
    second = run(provider.generate_json("system", "second", max_output_tokens=10))
    result = run(provider.healthcheck())

    assert first.usage.total_tokens == 3
    assert second.usage.total_tokens == 8
    assert result is None
    assert calls == [
        ("POST", "https://api.anthropic.com/v1/messages"),
        ("POST", "https://api.anthropic.com/v1/messages"),
        ("GET", "https://api.anthropic.com/v1/models"),
    ]
    assert provider.usage == ProviderUsage(
        input_tokens=7,
        output_tokens=4,
        total_tokens=11,
        request_count=2,
    )


def test_json_generation_uses_prompt_instruction_without_openai_fields_or_schema_claim() -> None:
    payloads: list[Mapping[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=response_body([{"type": "text", "text": '{"theme":"attention"}'}]),
        )

    provider = make_provider(
        ModelConfig(
            provider="anthropic",
            model="claude",
            default_max_output_tokens=8192,
            json_mode=True,
        ),
        handler,
    )

    result = run(
        provider.generate_json(
            "Keep the system prompt.",
            "Analyze this.",
            max_output_tokens=20,
            json_schema={"type": "object"},
        )
    )

    assert result.value == {"theme": "attention"}
    assert provider.capabilities.json_mode is False
    assert provider.capabilities.json_schema is False
    assert provider.capabilities.max_output_tokens is None
    assert "exactly one valid JSON object" in str(payloads[0]["system"])
    assert "response_format" not in payloads[0]
    assert "json_schema" not in payloads[0]
    assert "schema" not in payloads[0]


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

    provider = make_provider(ModelConfig(provider="anthropic", model="claude"), handler)

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


@pytest.mark.parametrize(
    ("stop_reason", "omit_stop_reason"),
    [
        pytest.param("unused", True, id="missing"),
        pytest.param(None, False, id="null"),
        pytest.param(7, False, id="non-string"),
        pytest.param("", False, id="empty"),
        pytest.param("   ", False, id="blank"),
        pytest.param("max_tokens", False, id="truncated"),
        pytest.param("tool_use", False, id="tool-continuation"),
        pytest.param("pause_turn", False, id="paused-continuation"),
        pytest.param("refusal", False, id="refusal"),
        pytest.param("content_filter", False, id="filtered"),
        pytest.param("unknown", False, id="unknown"),
    ],
)
def test_trusted_usage_is_recorded_before_invalid_or_truncated_stop_reason(
    stop_reason: object,
    omit_stop_reason: bool,
) -> None:
    body = response_body(stop_reason=stop_reason)
    if omit_stop_reason:
        body.pop("stop_reason")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    provider = make_provider(ModelConfig(provider="anthropic", model="claude"), handler)

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
    ("body", "expected_usage"),
    [
        pytest.param("not-json", ProviderUsage(), id="non-json-response"),
        pytest.param({}, ProviderUsage(), id="missing-usage"),
        pytest.param(
            response_body(content={"type": "text", "text": "answer"}),
            ProviderUsage(input_tokens=11, output_tokens=7, total_tokens=18, request_count=1),
            id="non-list-content-with-trusted-usage",
        ),
        pytest.param(
            response_body(content=["not-a-block"]),
            ProviderUsage(input_tokens=11, output_tokens=7, total_tokens=18, request_count=1),
            id="invalid-block-with-trusted-usage",
        ),
        pytest.param(
            response_body(content=[{}]),
            ProviderUsage(input_tokens=11, output_tokens=7, total_tokens=18, request_count=1),
            id="missing-block-type-with-trusted-usage",
        ),
        pytest.param(
            response_body(content=[{"type": 1, "text": "answer"}]),
            ProviderUsage(input_tokens=11, output_tokens=7, total_tokens=18, request_count=1),
            id="invalid-block-type-with-trusted-usage",
        ),
        pytest.param(
            response_body(content=[{"type": "text"}]),
            ProviderUsage(input_tokens=11, output_tokens=7, total_tokens=18, request_count=1),
            id="missing-text-with-trusted-usage",
        ),
        pytest.param(
            response_body(content=[{"type": "text", "text": 7}]),
            ProviderUsage(input_tokens=11, output_tokens=7, total_tokens=18, request_count=1),
            id="invalid-text-with-trusted-usage",
        ),
        pytest.param(
            response_body(content=[{"type": "text", "text": "   "}]),
            ProviderUsage(input_tokens=11, output_tokens=7, total_tokens=18, request_count=1),
            id="blank-text-with-trusted-usage",
        ),
        pytest.param(
            response_body(usage=None) | {"usage": None},
            ProviderUsage(),
            id="missing-usage",
        ),
        pytest.param(
            response_body(usage={"input_tokens": -1, "output_tokens": 1}),
            ProviderUsage(),
            id="negative-usage",
        ),
        pytest.param(
            response_body(usage={"input_tokens": True, "output_tokens": 1}),
            ProviderUsage(),
            id="boolean-usage",
        ),
        pytest.param(
            response_body(usage={"input_tokens": 1, "output_tokens": "1"}),
            ProviderUsage(),
            id="string-usage",
        ),
    ],
)
def test_malformed_content_records_only_trusted_usage(
    body: object,
    expected_usage: ProviderUsage,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(body, str):
            return httpx.Response(200, text=body)
        return httpx.Response(200, json=body)

    provider = make_provider(ModelConfig(provider="anthropic", model="claude"), handler)

    with pytest.raises(ForgeException) as caught:
        run(provider.generate_text("system", "user", max_output_tokens=10))

    assert caught.value.code is ForgeErrorCode.MODEL_OUTPUT_INVALID
    assert caught.value.retryable is False
    assert caught.value.details == {}
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert provider.usage == expected_usage


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        "42",
        "null",
        '"text"',
        "NaN",
        "Infinity",
        "-Infinity",
        '```json\n{"ok":true}\n```',
        'Here is the result: {"ok":true}',
    ],
)
def test_json_generation_accepts_only_a_direct_standard_json_object(content: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=response_body([{"type": "text", "text": content}]),
        )

    provider = make_provider(ModelConfig(provider="anthropic", model="claude"), handler)

    with pytest.raises(ForgeException) as caught:
        run(provider.generate_json("system", "user", max_output_tokens=10))

    assert caught.value.code is ForgeErrorCode.MODEL_OUTPUT_INVALID
    assert provider.usage == ProviderUsage(
        input_tokens=11,
        output_tokens=7,
        total_tokens=18,
        request_count=1,
    )


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (401, ForgeErrorCode.MODEL_AUTH_FAILED, False),
        (403, ForgeErrorCode.MODEL_AUTH_FAILED, False),
        (429, ForgeErrorCode.MODEL_RATE_LIMITED, True),
        (418, ForgeErrorCode.MODEL_UNAVAILABLE, False),
        (500, ForgeErrorCode.MODEL_UNAVAILABLE, True),
    ],
)
def test_http_failure_mapping_is_safe_and_does_not_mutate_usage(
    status: int,
    code: ForgeErrorCode,
    retryable: bool,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="private upstream response")

    provider = make_provider(
        ModelConfig(
            provider="anthropic",
            model="claude",
            base_url="https://private-claude.example/api",
        ),
        handler,
        api_key="secret-key",
    )

    with pytest.raises(ForgeException) as caught:
        run(provider.generate_text("private system", "private book text", max_output_tokens=10))

    assert caught.value.code is code
    assert caught.value.retryable is retryable
    assert provider.usage == ProviderUsage()
    rendered = " ".join(
        (
            str(caught.value),
            repr(caught.value),
            repr(caught.value.details),
            repr(caught.value.as_result()),
        )
    )
    for private_value in (
        "private upstream response",
        "private-claude",
        "secret-key",
        "private system",
        "private book text",
    ):
        assert private_value not in rendered


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError("secret connection detail"),
        httpx.ReadTimeout("secret timeout detail"),
    ],
)
def test_network_failure_mapping_is_safe_and_does_not_mutate_usage(
    failure: httpx.HTTPError,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise failure

    provider = make_provider(ModelConfig(provider="anthropic", model="claude"), handler)

    with pytest.raises(ForgeException) as caught:
        run(provider.generate_text("system", "user", max_output_tokens=10))

    assert caught.value.code is ForgeErrorCode.MODEL_UNAVAILABLE
    assert caught.value.retryable is True
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "secret" not in repr(caught.value.as_result())
    assert provider.usage == ProviderUsage()


def test_each_public_method_performs_exactly_one_request() -> None:
    count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        content = '{"ok":true}' if count == 2 else "answer"
        return httpx.Response(
            200,
            json=response_body([{"type": "text", "text": content}]),
        )

    provider = make_provider(ModelConfig(provider="anthropic", model="claude"), handler)

    run(provider.generate_text("system", "text", max_output_tokens=10))
    assert count == 1
    run(provider.generate_json("system", "json", max_output_tokens=10))
    assert count == 2
    run(provider.healthcheck())
    assert count == 3
