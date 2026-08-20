"""Canonical, credential-free Provider route identities."""

from ipaddress import ip_address
from urllib.parse import urlsplit

from cove_book_forge.config.models import ModelConfig

_DEFAULT_ROUTES = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com",
    "anthropic": "https://api.anthropic.com",
}
_DEFAULT_PORTS = {"http": 80, "https": 443}

ProviderRouteIdentity = tuple[str, str, int | None, str]


def canonical_provider_route_identity(config: ModelConfig) -> ProviderRouteIdentity:
    """Return only network-routing components, never URL credentials or metadata."""
    raw_route = (
        str(config.base_url)
        if config.base_url is not None
        else _DEFAULT_ROUTES.get(config.provider, "")
    )
    parsed = urlsplit(raw_route)
    scheme = parsed.scheme.lower()
    explicit_port = parsed.port
    return (
        scheme,
        _canonical_host(parsed.hostname or ""),
        explicit_port if explicit_port is not None else _DEFAULT_PORTS.get(scheme),
        parsed.path.rstrip("/"),
    )


def _canonical_host(host: str) -> str:
    try:
        return ip_address(host).compressed.lower()
    except ValueError:
        return host.lower()
