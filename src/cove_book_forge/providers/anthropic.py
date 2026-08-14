import json
from collections.abc import Awaitable, Callable, Mapping
from typing import NoReturn, cast

import httpx
from pydantic import JsonValue

from cove_book_forge.config import ModelConfig
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.providers.base import (
    JsonGeneration,
    ProviderCapabilities,
    ProviderUsage,
    TextGeneration,
)
from cove_book_forge.providers.transport import ProviderTransport

_DEFAULT_BASE = "https://api.anthropic.com"
_ANTHROPIC_VERSION = "2023-06-01"
_JSON_OBJECT_INSTRUCTION = "Return exactly one valid JSON object and no other text."


class AnthropicProvider:
    def __init__(
        self,
        config: ModelConfig,
        api_key: str | None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._config = config
        self._api_key = api_key if api_key and api_key.strip() else None
        self._base_url = self._resolve_base_url(config)
        self._usage = ProviderUsage()
        self._requester = ProviderTransport(
            timeout_seconds=config.request_timeout_seconds,
            max_concurrency=config.max_concurrency,
            requests_per_minute=config.requests_per_minute,
            transport=transport,
            clock=clock,
            sleep=sleep,
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            json_mode=False,
            json_schema=False,
            max_output_tokens=self._config.default_max_output_tokens,
        )

    @property
    def usage(self) -> ProviderUsage:
        return self._usage

    async def generate_text(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_output_tokens: int,
        temperature: float | None = None,
    ) -> TextGeneration:
        response = await self._requester.request(
            "POST",
            f"{self._base_url}/v1/messages",
            headers=self._headers(),
            json=self._generation_payload(
                system_prompt,
                user_prompt,
                max_output_tokens=max_output_tokens,
                temperature=temperature,
            ),
        )
        text, model, usage = self._parse_generation(response)
        result = TextGeneration(text=text, model=model, usage=usage)
        self._record_usage(usage)
        return result

    async def generate_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_output_tokens: int,
        temperature: float | None = None,
        json_schema: Mapping[str, JsonValue] | None = None,
    ) -> JsonGeneration:
        del json_schema
        instructed_system_prompt = f"{system_prompt}\n\n{_JSON_OBJECT_INSTRUCTION}"
        response = await self._requester.request(
            "POST",
            f"{self._base_url}/v1/messages",
            headers=self._headers(),
            json=self._generation_payload(
                instructed_system_prompt,
                user_prompt,
                max_output_tokens=max_output_tokens,
                temperature=temperature,
            ),
        )
        text, model, usage = self._parse_generation(response)
        value = self._parse_json_object(text)
        result = JsonGeneration(value=value, model=model, usage=usage)
        self._record_usage(usage)
        return result

    async def healthcheck(self) -> None:
        await self._requester.request(
            "GET",
            f"{self._base_url}/v1/models",
            headers=self._headers(),
        )

    @staticmethod
    def _resolve_base_url(config: ModelConfig) -> str:
        if config.base_url is None:
            return _DEFAULT_BASE
        if config.base_url.query is not None or config.base_url.fragment is not None:
            raise ForgeException(
                ForgeErrorCode.CONFIG_INVALID,
                "Anthropic provider requires a plain API base.",
                details={"field": "model.provider"},
            )
        return str(config.base_url).rstrip("/")

    def _headers(self) -> dict[str, str]:
        headers = {
            "anthropic-version": _ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }
        if self._api_key is not None:
            headers["x-api-key"] = self._api_key
        return headers

    def _generation_payload(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_output_tokens: int,
        temperature: float | None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": self._config.model,
            "max_tokens": max_output_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        if temperature is not None:
            payload["temperature"] = temperature
        return payload

    def _parse_generation(self, response: httpx.Response) -> tuple[str, str, ProviderUsage]:
        body = self._response_object(response)
        stop_reason = body.get("stop_reason")
        if (
            not isinstance(stop_reason, str)
            or not stop_reason.strip()
            or stop_reason == "max_tokens"
        ):
            self._invalid_output()
        text = self._parse_content(body.get("content"))
        usage = self._parse_usage(body.get("usage"))
        model_value = body.get("model")
        model = (
            model_value
            if isinstance(model_value, str) and model_value.strip()
            else self._config.model
        )
        return text, model, usage

    @classmethod
    def _response_object(cls, response: httpx.Response) -> Mapping[str, object]:
        decode_failed = False
        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError):
            body = None
            decode_failed = True
        if decode_failed or not isinstance(body, Mapping):
            cls._invalid_output()
        return cast(Mapping[str, object], body)

    @classmethod
    def _parse_content(cls, raw_content: object) -> str:
        if not isinstance(raw_content, list):
            cls._invalid_output()
        chunks: list[str] = []
        for block in raw_content:
            if not isinstance(block, Mapping):
                cls._invalid_output()
            block_type = block.get("type")
            if not isinstance(block_type, str) or not block_type.strip():
                cls._invalid_output()
            if block_type != "text":
                continue
            text = block.get("text")
            if not isinstance(text, str):
                cls._invalid_output()
            if text.strip():
                chunks.append(text)
        if not chunks:
            cls._invalid_output()
        return "".join(chunks)

    @classmethod
    def _parse_usage(cls, raw_usage: object) -> ProviderUsage:
        if not isinstance(raw_usage, Mapping):
            cls._invalid_output()
        input_tokens = cls._token_count(raw_usage.get("input_tokens"))
        output_tokens = cls._token_count(raw_usage.get("output_tokens"))
        return ProviderUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            request_count=1,
        )

    @classmethod
    def _token_count(cls, value: object) -> int:
        if type(value) is not int or value < 0:
            cls._invalid_output()
        return value

    @classmethod
    def _parse_json_object(cls, content: str) -> dict[str, JsonValue]:
        decode_failed = False
        try:
            value = json.loads(content, parse_constant=cls._reject_json_constant)
        except (json.JSONDecodeError, ValueError):
            value = None
            decode_failed = True
        if decode_failed or not isinstance(value, dict):
            cls._invalid_output()
        return cast(dict[str, JsonValue], value)

    @staticmethod
    def _reject_json_constant(value: str) -> None:
        raise ValueError("non-standard JSON constant")

    def _record_usage(self, usage: ProviderUsage) -> None:
        self._usage = ProviderUsage(
            input_tokens=self._usage.input_tokens + usage.input_tokens,
            output_tokens=self._usage.output_tokens + usage.output_tokens,
            total_tokens=self._usage.total_tokens + usage.total_tokens,
            request_count=self._usage.request_count + 1,
        )

    @staticmethod
    def _invalid_output() -> NoReturn:
        raise ForgeException(
            ForgeErrorCode.MODEL_OUTPUT_INVALID,
            "Model output was malformed.",
        )
