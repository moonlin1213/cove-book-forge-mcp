from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_serializer, model_validator

_PUBLIC_FIELD_VALUES = frozenset({"model.provider"})


class ForgeErrorCode(StrEnum):
    CONFIG_INVALID = "CONFIG_INVALID"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    MODEL_AUTH_FAILED = "MODEL_AUTH_FAILED"
    MODEL_RATE_LIMITED = "MODEL_RATE_LIMITED"
    MODEL_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"
    SOURCE_NOT_FOUND = "SOURCE_NOT_FOUND"
    SOURCE_CHANGED = "SOURCE_CHANGED"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    ENCRYPTED_DOCUMENT = "ENCRYPTED_DOCUMENT"
    OCR_REQUIRED = "OCR_REQUIRED"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    EXTERNAL_BOOK_INCOMPLETE = "EXTERNAL_BOOK_INCOMPLETE"
    OUTPUT_NOT_CONFIGURED = "OUTPUT_NOT_CONFIGURED"
    OUTPUT_PERMISSION_DENIED = "OUTPUT_PERMISSION_DENIED"
    EXTERNAL_MODIFICATION = "EXTERNAL_MODIFICATION"
    INSTALL_CONFLICT = "INSTALL_CONFLICT"
    PATH_NOT_ALLOWED = "PATH_NOT_ALLOWED"
    JOB_CONFLICT = "JOB_CONFLICT"
    JOB_INTERRUPTED = "JOB_INTERRUPTED"
    JOB_CANCELLED = "JOB_CANCELLED"
    PLAN_EXPIRED = "PLAN_EXPIRED"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"


_PUBLIC_MESSAGES = {
    ForgeErrorCode.CONFIG_INVALID: "Configuration is invalid.",
    ForgeErrorCode.MODEL_UNAVAILABLE: "Model provider is unavailable.",
    ForgeErrorCode.MODEL_AUTH_FAILED: "Model provider authentication failed.",
    ForgeErrorCode.MODEL_RATE_LIMITED: "Model provider rate limit was reached.",
    ForgeErrorCode.MODEL_OUTPUT_INVALID: "Model provider returned invalid output.",
    ForgeErrorCode.SOURCE_NOT_FOUND: "Source was not found.",
    ForgeErrorCode.SOURCE_CHANGED: "Source changed since the operation began.",
    ForgeErrorCode.UNSUPPORTED_FORMAT: "Source format is not supported.",
    ForgeErrorCode.ENCRYPTED_DOCUMENT: "Encrypted documents are not supported.",
    ForgeErrorCode.OCR_REQUIRED: "Source requires OCR before processing.",
    ForgeErrorCode.EXTRACTION_FAILED: "Source content could not be extracted.",
    ForgeErrorCode.EXTERNAL_BOOK_INCOMPLETE: "External book snapshot is incomplete.",
    ForgeErrorCode.OUTPUT_NOT_CONFIGURED: "Output is not configured.",
    ForgeErrorCode.OUTPUT_PERMISSION_DENIED: "Output location is not writable.",
    ForgeErrorCode.EXTERNAL_MODIFICATION: "Output changed outside this application.",
    ForgeErrorCode.INSTALL_CONFLICT: "Skill installation conflicts with an existing installation.",
    ForgeErrorCode.PATH_NOT_ALLOWED: "Path is outside the authorized locations.",
    ForgeErrorCode.JOB_CONFLICT: "Another job already controls this book and target.",
    ForgeErrorCode.JOB_INTERRUPTED: "Job was interrupted and can be resumed.",
    ForgeErrorCode.JOB_CANCELLED: "Job was cancelled.",
    ForgeErrorCode.PLAN_EXPIRED: "Forge plan expired and must be recreated.",
    ForgeErrorCode.CONFIRMATION_REQUIRED: "Explicit confirmation is required.",
}


def _public_details(details: Mapping[str, object]) -> dict[str, JsonValue]:
    field = details.get("field")
    if isinstance(field, str) and field in _PUBLIC_FIELD_VALUES:
        return {"field": field}
    return {}


class ForgeErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: ForgeErrorCode
    message: str = Field(min_length=1, max_length=1200)
    retryable: bool = False
    details: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def enforce_public_representation(cls, data: Any) -> Any:
        if not isinstance(data, Mapping):
            return data
        normalized = dict(data)
        raw_code = normalized.get("code")
        if not isinstance(raw_code, (ForgeErrorCode, str)):
            return data
        try:
            code = ForgeErrorCode(raw_code)
        except ValueError:
            return data
        normalized["message"] = _PUBLIC_MESSAGES[code]
        details = normalized.get("details")
        normalized["details"] = _public_details(details) if isinstance(details, Mapping) else {}
        return normalized

    @field_serializer("details")
    def serialize_public_details(self, details: Mapping[str, object]) -> dict[str, JsonValue]:
        return _public_details(details)


class ForgeException(RuntimeError):
    def __init__(
        self,
        code: ForgeErrorCode,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, JsonValue] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(_PUBLIC_MESSAGES[code])
        self.code = code
        self.retryable = retryable
        self.details = details or {}
        self.__cause__ = cause

    def as_detail(self) -> ForgeErrorDetail:
        return ForgeErrorDetail(
            code=self.code,
            message=str(self),
            retryable=self.retryable,
            details=self._public_details(),
        )

    def as_result(self) -> dict[str, object]:
        return {"ok": False, "error": self.as_detail().model_dump(mode="json")}

    def _public_details(self) -> dict[str, JsonValue]:
        return _public_details(self.details)
