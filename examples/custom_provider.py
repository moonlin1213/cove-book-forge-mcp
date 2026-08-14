"""Typed local custom-provider example.

Applications should replace the deterministic demonstration generation logic below with
their own backend while preserving the public async provider contract.
"""

from collections.abc import Mapping

from pydantic import JsonValue

from cove_book_forge.config import ModelConfig
from cove_book_forge.providers import (
    JsonGeneration,
    ProviderCapabilities,
    ProviderUsage,
    TextGeneration,
)


class DeterministicLocalProvider:
    def __init__(self, config: ModelConfig) -> None:
        self._model = config.model
        self._max_output_tokens = config.default_max_output_tokens
        self._usage = ProviderUsage()

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            json_mode=True,
            json_schema=False,
            max_output_tokens=self._max_output_tokens,
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
        del max_output_tokens, temperature
        usage = self._call_usage(system_prompt, user_prompt, user_prompt)
        result = TextGeneration(text=user_prompt, model=self._model, usage=usage)
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
        del max_output_tokens, temperature, json_schema
        usage = self._call_usage(system_prompt, user_prompt, user_prompt)
        result = JsonGeneration(
            value={"text": user_prompt},
            model=self._model,
            usage=usage,
        )
        self._record_usage(usage)
        return result

    async def healthcheck(self) -> None:
        return None

    @staticmethod
    def _call_usage(system_prompt: str, user_prompt: str, output: str) -> ProviderUsage:
        input_tokens = len(system_prompt.split()) + len(user_prompt.split())
        output_tokens = len(output.split())
        return ProviderUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            request_count=1,
        )

    def _record_usage(self, usage: ProviderUsage) -> None:
        self._usage = ProviderUsage(
            input_tokens=self._usage.input_tokens + usage.input_tokens,
            output_tokens=self._usage.output_tokens + usage.output_tokens,
            total_tokens=self._usage.total_tokens + usage.total_tokens,
            request_count=self._usage.request_count + 1,
        )


def custom_provider_factory(
    config: ModelConfig,
    api_key: str | None,
) -> DeterministicLocalProvider:
    del api_key
    return DeterministicLocalProvider(config)
