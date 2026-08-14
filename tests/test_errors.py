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
            "details": {"field": "model.provider", "path": "/books/example.pdf"},
        },
    }
    assert "secret" not in str(exc.as_result())
    assert "source body" not in str(exc.as_result())
