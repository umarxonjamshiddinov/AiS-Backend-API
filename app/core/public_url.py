"""Resolve the public API URL without trusting client-supplied proxy headers."""

from urllib.parse import urlsplit, urlunsplit

from fastapi import Request

from app.core.config import get_settings
from app.providers.base import ProviderError


def deployment_api_base_url(request: Request) -> str:
    scheme = request.url.scheme
    host = request.headers.get("host", "")
    try:
        parsed = urlsplit(f"{scheme}://{host}")
        port = parsed.port
        root_path = request.scope.get("root_path", "").rstrip("/")
        valid = (
            scheme in ("http", "https")
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.netloc.endswith(":")
            and not any(char.isspace() or ord(char) < 33 for char in parsed.netloc)
            and not parsed.path
            and not parsed.query
            and not parsed.fragment
            and (port is None or 1 <= port <= 65535)
            and (not root_path or root_path.startswith("/"))
            and "//" not in root_path
            and "\\" not in root_path
            and not any(part in (".", "..") for part in root_path.split("/"))
        )
    except ValueError:
        valid = False
    if not valid:
        raise ProviderError(
            "public_api_url_unavailable",
            503,
            usage_unknown=False,
            message="The deployment API URL is not available; configure PUBLIC_API_BASE_URL.",
        )
    return urlunsplit((scheme, parsed.netloc, f"{root_path}/v1", "", ""))


def public_api_base_url(request: Request) -> str:
    configured = getattr(get_settings(), "public_api_base_url", None)
    return configured or deployment_api_base_url(request)


def public_site_url(request: Request) -> str:
    """Return the deployment root URL, using the configured public endpoint if available."""
    parsed = urlsplit(public_api_base_url(request))
    path = parsed.path.removesuffix("/v1").rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, f"{path}/", "", ""))
