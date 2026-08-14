from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, JsonValue

_PUBLIC_DETAIL_KEYS = frozenset({"field", "path"})
_SENSITIVE_MESSAGE_MARKERS = (
    "authorization",
    "bearer",
    "api key",
    "api_key",
    "apikey",
    "request",
    "source body",
)
_GENERIC_PUBLIC_MESSAGE = "An internal error occurred."


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


class ForgeErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: ForgeErrorCode
    message: str = Field(min_length=1, max_length=1200)
    retryable: bool = False
    details: dict[str, JsonValue] = Field(default_factory=dict)


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
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.details = details or {}
        self.__cause__ = cause

    def as_result(self) -> dict[str, object]:
        detail = ForgeErrorDetail(
            code=self.code,
            message=self._public_message(),
            retryable=self.retryable,
            details=self._public_details(),
        )
        return {"ok": False, "error": detail.model_dump(mode="json")}

    def _public_message(self) -> str:
        message = str(self)
        if any(marker in message.lower() for marker in _SENSITIVE_MESSAGE_MARKERS):
            return _GENERIC_PUBLIC_MESSAGE
        return message

    def _public_details(self) -> dict[str, JsonValue]:
        return {
            key: value
            for key, value in self.details.items()
            if key in _PUBLIC_DETAIL_KEYS and isinstance(value, str)
        }
