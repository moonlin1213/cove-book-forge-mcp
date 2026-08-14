import pytest

from cove_book_forge.errors import ForgeErrorCode, ForgeException


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
            "message": "An internal error occurred.",
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
