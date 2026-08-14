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
