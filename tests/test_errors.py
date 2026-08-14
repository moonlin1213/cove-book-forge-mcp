import json

import pytest
from pydantic import JsonValue

from cove_book_forge.contracts.jobs import ForgeJob, ForgeJobStatus, ForgeTarget
from cove_book_forge.errors import ForgeErrorCode, ForgeErrorDetail, ForgeException

PUBLIC_MESSAGES = {
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
}


@pytest.mark.parametrize(("code", "public_message"), PUBLIC_MESSAGES.items())
def test_every_error_code_uses_its_actionable_public_message(
    code: ForgeErrorCode, public_message: str
) -> None:
    detail = ForgeException(code, "Caller-controlled failure text.").as_detail()

    assert detail.message == public_message


def test_direct_error_detail_normalizes_untrusted_public_fields() -> None:
    detail = ForgeErrorDetail(
        code=ForgeErrorCode.MODEL_AUTH_FAILED,
        message="Authorization: Bearer secret-token",
        details={
            "authorization": "Bearer secret-token",
            "request": {"messages": ["private source body"]},
            "field": "model.provider",
        },
    )

    assert detail.model_dump(mode="json") == {
        "code": "MODEL_AUTH_FAILED",
        "message": "Model provider authentication failed.",
        "retryable": False,
        "details": {"field": "model.provider"},
    }


def test_error_detail_serialization_recloses_details_after_mutation() -> None:
    detail = ForgeErrorDetail(
        code=ForgeErrorCode.MODEL_AUTH_FAILED,
        message="Caller-controlled message",
        details={"field": "model.provider"},
    )
    detail.details.update(
        {
            "api_key": "sk-injected-key",
            "request": {"messages": ["injected private source"]},
            "source_content": "injected private source",
        }
    )
    job = ForgeJob(
        job_id="job-mutated-error",
        book_id="book-1",
        target=ForgeTarget.SKILL,
        status=ForgeJobStatus.FAILED,
        error=detail,
    )
    expected_error = {
        "code": "MODEL_AUTH_FAILED",
        "message": "Model provider authentication failed.",
        "retryable": False,
        "details": {"field": "model.provider"},
    }

    assert detail.model_dump(mode="json") == expected_error
    assert job.model_dump(mode="json")["error"] == expected_error
    assert json.loads(job.model_dump_json())["error"] == expected_error

    for payload in (str(job.model_dump(mode="json")), job.model_dump_json()):
        for private_value in (
            "api_key",
            "sk-injected-key",
            "request",
            "source_content",
            "injected private source",
        ):
            assert private_value not in payload


def test_job_error_serialization_cannot_expose_provider_failure_payload() -> None:
    exc = ForgeException(
        ForgeErrorCode.MODEL_AUTH_FAILED,
        "Upstream auth failed for sk-private-key",
        details={
            "authorization": "Bearer secret-token",
            "api_key": "sk-private-key",
            "request": {"messages": ["private source body"]},
            "source_content": "private source body",
            "field": "model.provider",
        },
        cause=RuntimeError("Provider response included X-API-Key: sk-private-key"),
    )
    job = ForgeJob(
        job_id="job-1",
        book_id="book-1",
        target=ForgeTarget.SKILL,
        status=ForgeJobStatus.FAILED,
        error=exc.as_detail(),
    )

    payload = job.model_dump_json()
    assert '"message":"Model provider authentication failed."' in payload
    assert '"details":{"field":"model.provider"}' in payload
    for private_value in (
        "Upstream auth failed",
        "RuntimeError",
        "authorization",
        "Bearer",
        "request",
        "private source body",
        "sk-private-key",
    ):
        assert private_value not in payload


def test_forge_exception_serializes_public_error_without_private_cause() -> None:
    exc = ForgeException(
        ForgeErrorCode.CONFIG_INVALID,
        "Configuration is invalid.",
        retryable=False,
        details={"field": "model.provider"},
        cause=RuntimeError("Authorization: Bearer secret"),
    )

    assert exc.as_result() == {
        "ok": False,
        "error": {
            "code": "CONFIG_INVALID",
            "message": "Configuration is invalid.",
            "retryable": False,
            "details": {"field": "model.provider"},
        },
    }
    assert "secret" not in str(exc.as_result())


def test_forge_exception_redacts_sensitive_message_and_details() -> None:
    exc = ForgeException(
        ForgeErrorCode.MODEL_AUTH_FAILED,
        "Upstream failure: Authorization: Bearer secret-token",
        details={
            "authorization": "Bearer secret-token",
            "api_key": "sk-secret",
            "request": {"model": "test", "messages": ["source body"]},
            "source_body": "source body",
            "field": "model.provider",
            "path": "/books/example.pdf",
        },
    )

    assert exc.as_result() == {
        "ok": False,
        "error": {
            "code": "MODEL_AUTH_FAILED",
            "message": "Model provider authentication failed.",
            "retryable": False,
            "details": {"field": "model.provider"},
        },
    }
    assert "secret" not in str(exc.as_result())
    assert "source body" not in str(exc.as_result())


@pytest.mark.parametrize(
    ("message", "details"),
    [
        ("Unmarked source body content: private chapter text.", {}),
        ("Upstream header X-API-Key: secret-key", {}),
        ("Your request is invalid.", {}),
        ("Configuration is invalid.", {"field": "Authorization: Bearer secret"}),
    ],
)
def test_forge_exception_uses_code_owned_message_and_safe_field_only(
    message: str, details: dict[str, str]
) -> None:
    exc = ForgeException(ForgeErrorCode.CONFIG_INVALID, message, details=details)

    assert str(exc) == "Configuration is invalid."
    assert exc.as_result()["error"] == {
        "code": "CONFIG_INVALID",
        "message": "Configuration is invalid.",
        "retryable": False,
        "details": {},
    }


def test_forge_exception_keeps_safe_dotted_field_identifier() -> None:
    exc = ForgeException(
        ForgeErrorCode.CONFIG_INVALID,
        "Untrusted caller text.",
        details={"field": "model.provider"},
    )

    assert exc.as_result()["error"]["details"] == {"field": "model.provider"}


@pytest.mark.parametrize(
    "field",
    [
        "sk_secret_token",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzZWNyZXQifQ.signature",
    ],
)
def test_forge_exception_drops_identifier_shaped_sensitive_field_values(field: str) -> None:
    exc = ForgeException(
        ForgeErrorCode.CONFIG_INVALID,
        "Untrusted caller text.",
        details={"field": field},
    )

    assert exc.as_result()["error"]["details"] == {}


@pytest.mark.parametrize("field", [[], {}])
def test_forge_exception_drops_non_string_field_values(field: JsonValue) -> None:
    exc = ForgeException(
        ForgeErrorCode.CONFIG_INVALID,
        "Untrusted caller text.",
        details={"field": field},
    )

    assert exc.as_result()["error"]["details"] == {}
