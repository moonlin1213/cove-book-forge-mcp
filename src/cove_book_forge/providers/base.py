from collections.abc import Mapping
from typing import Protocol, Self, runtime_checkable

from pydantic import ConfigDict, Field, JsonValue, model_validator

from cove_book_forge.contracts.base import ContractModel


class ProviderCapabilities(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    json_mode: bool
    json_schema: bool = False
    max_context_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    reasoning_parameters: tuple[str, ...] = ()


class ProviderUsage(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    request_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def require_consistent_total(self) -> Self:
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("total_tokens must equal input_tokens + output_tokens")
        return self


class TextGeneration(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    text: str
    model: str
    usage: ProviderUsage


class JsonGeneration(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    value: dict[str, JsonValue]
    model: str
    usage: ProviderUsage


@runtime_checkable
class ModelProvider(Protocol):
    @property
    def capabilities(self) -> ProviderCapabilities: ...

    @property
    def usage(self) -> ProviderUsage: ...

    async def generate_text(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_output_tokens: int,
        temperature: float | None = None,
    ) -> TextGeneration: ...

    async def generate_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_output_tokens: int,
        temperature: float | None = None,
        json_schema: Mapping[str, JsonValue] | None = None,
    ) -> JsonGeneration: ...

    async def healthcheck(self) -> None: ...
