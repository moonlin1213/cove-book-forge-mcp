import os

from cove_book_forge.config.models import ModelConfig
from cove_book_forge.errors import ForgeErrorCode, ForgeException

_REQUIRED_CREDENTIAL_PROVIDERS = frozenset({"openai", "deepseek", "anthropic"})


def resolve_provider_credential(config: ModelConfig) -> str | None:
    environment_name = config.api_key_env
    if environment_name is None:
        if config.provider not in _REQUIRED_CREDENTIAL_PROVIDERS:
            return None
        raise ForgeException(
            ForgeErrorCode.MODEL_AUTH_FAILED,
            "Configured model credential is unavailable.",
        )
    value = os.environ.get(environment_name)
    if value is None or not value.strip():
        raise ForgeException(
            ForgeErrorCode.MODEL_AUTH_FAILED,
            "Configured model credential is unavailable.",
        )
    return value
