import json
from collections.abc import Awaitable, Callable, Mapping
from typing import NoReturn, cast

import httpx
from pydantic import JsonValue

from cove_book_forge.config.models import ModelConfig
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.providers.base import (
    JsonGeneration,
    ProviderCapabilities,
    ProviderUsage,
    TextGeneration,
)
from cove_book_forge.providers.transport import ProviderTransport

_DEFAULT_BASES = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com",
}
_JSON_OBJECT_INSTRUCTION = "Return exactly one valid JSON object and no other text."


class OpenAICompatibleProvider:
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
            json_mode=True,
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
        payload = self._generation_payload(
            system_prompt,
            user_prompt,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
        )
        response = await self._requester.request(
            "POST",
            f"{self._base_url}/chat/completions",
            headers=self._headers(),
            json=payload,
        )
        content, model, usage = self._parse_generation(response)
        result = TextGeneration(text=content, model=model, usage=usage)
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
        payload = self._generation_payload(
            instructed_system_prompt,
            user_prompt,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
        )
        payload["response_format"] = {"type": "json_object"}
        response = await self._requester.request(
            "POST",
            f"{self._base_url}/chat/completions",
            headers=self._headers(),
            json=payload,
        )
        content, model, usage = self._parse_generation(response)
        value = self._parse_json_object(content)
        result = JsonGeneration(value=value, model=model, usage=usage)
        self._record_usage(usage)
        return result

    async def healthcheck(self) -> None:
        await self._requester.request(
            "GET",
            f"{self._base_url}/models",
            headers=self._headers(),
        )

    @staticmethod
    def _resolve_base_url(config: ModelConfig) -> str:
        if config.base_url is not None:
            return str(config.base_url).rstrip("/")
        default = _DEFAULT_BASES.get(config.provider)
        if default is None:
            raise ForgeException(
                ForgeErrorCode.CONFIG_INVALID,
                "OpenAI-compatible provider requires an API base.",
                details={"field": "model.provider"},
            )
        return default

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key}"
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
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_output_tokens,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        return payload

    def _parse_generation(self, response: httpx.Response) -> tuple[str, str, ProviderUsage]:
        body = self._response_object(response)
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            self._invalid_output()
        choice = choices[0]
        if choice.get("finish_reason") == "length":
            self._invalid_output()
        message = choice.get("message")
        if not isinstance(message, Mapping):
            self._invalid_output()
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            self._invalid_output()
        model_value = body.get("model")
        model = (
            model_value
            if isinstance(model_value, str) and model_value.strip()
            else self._config.model
        )
        usage = self._parse_usage(body.get("usage"))
        return content, model, usage

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
    def _parse_usage(cls, raw_usage: object) -> ProviderUsage:
        if not isinstance(raw_usage, Mapping):
            cls._invalid_output()
        input_tokens = cls._token_count(raw_usage.get("prompt_tokens"))
        output_tokens = cls._token_count(raw_usage.get("completion_tokens"))
        raw_total = raw_usage.get("total_tokens")
        total_tokens = (
            input_tokens + output_tokens if raw_total is None else cls._token_count(raw_total)
        )
        if total_tokens != input_tokens + output_tokens:
            cls._invalid_output()
        return ProviderUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
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
