from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.public_url import deployment_api_base_url, public_api_base_url
from app.portal import mount_portal
from app.providers.base import ProviderError


def request_for(*, scheme="http", host="127.0.0.1:8000", root_path="", forwarded=()):
    headers = [(b"host", host.encode())]
    headers.extend((name.lower().encode(), value.encode()) for name, value in forwarded)
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": scheme,
            "path": "/admin/api/overview",
            "raw_path": b"/admin/api/overview",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8000),
            "root_path": root_path,
        }
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://ai.example.com/v1", "https://ai.example.com/v1"),
        ("https://ai.example.com/", "https://ai.example.com/v1"),
        ("https://ai.example.com/gateway/v1/", "https://ai.example.com/gateway/v1"),
        ("http://127.0.0.1:8000", "http://127.0.0.1:8000/v1"),
    ],
)
def test_public_api_base_url_is_normalized(value, expected):
    assert Settings(_env_file=None, public_api_base_url=value).public_api_base_url == expected


@pytest.mark.parametrize(
    "value",
    [
        "relative/path",
        "ftp://ai.example.com/v1",
        "https://user:password@ai.example.com/v1",
        "https://ai.example.com/v1?token=secret",
        "https://ai.example.com/v1#fragment",
        "https://ai.example.com:invalid/v1",
        "https://@ai.example.com/v1",
        "https://ai.example.com:/v1",
        "https://invalid host/v1",
    ],
)
def test_invalid_public_api_base_url_fails_configuration(value):
    with pytest.raises(ValueError, match="PUBLIC_API_BASE_URL"):
        Settings(_env_file=None, public_api_base_url=value)


def test_settings_reads_public_api_base_url_environment_variable(monkeypatch):
    monkeypatch.setenv("PUBLIC_API_BASE_URL", "https://api.example.com/v1/")
    assert Settings(_env_file=None).public_api_base_url == "https://api.example.com/v1"


def test_deployment_url_uses_asgi_origin_and_mount_path_only():
    request = request_for(
        scheme="https",
        host="selfhost.example:8443",
        root_path="/gateway",
        forwarded=(("X-Forwarded-Host", "attacker.example"), ("X-Forwarded-Proto", "http")),
    )
    assert deployment_api_base_url(request) == "https://selfhost.example:8443/gateway/v1"


def test_configured_url_overrides_request_origin(monkeypatch):
    monkeypatch.setattr(
        "app.core.public_url.get_settings",
        lambda: SimpleNamespace(public_api_base_url="https://api.example.com/v1"),
    )
    request = request_for(host="internal-container:8000")
    assert public_api_base_url(request) == "https://api.example.com/v1"


@pytest.mark.parametrize(
    ("configured", "host", "forwarded", "expected"),
    [
        (None, "selfhost.example", "attacker.example", "http://selfhost.example/"),
        (
            "https://api.example.com/gateway/v1",
            "internal-container:8000",
            "attacker.example",
            "https://api.example.com/gateway/",
        ),
    ],
)
def test_portal_sitemap_uses_instance_url_not_untrusted_forwarded_host(
    tmp_path, monkeypatch, configured, host, forwarded, expected
):
    assets = tmp_path / "assets"
    assets.mkdir()
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    (assets / "bundle.js").write_text("void 0;", encoding="utf-8")
    (tmp_path / "sitemap.xml").write_text("<urlset />", encoding="utf-8")
    (tmp_path / "robots.txt").write_text("User-agent: *\nAllow: /\n", encoding="utf-8")
    monkeypatch.setattr(
        "app.core.public_url.get_settings",
        lambda: SimpleNamespace(public_api_base_url=configured),
    )
    application = FastAPI()
    assert mount_portal(application, tmp_path)
    with TestClient(application) as client:
        response = client.get(
            "/sitemap.xml",
            headers={"Host": host, "X-Forwarded-Host": forwarded, "X-Forwarded-Proto": "https"},
        )
    assert response.status_code == 200
    assert f"<loc>{expected}</loc>" in response.text
    assert "myais.uz" not in response.text


@pytest.mark.parametrize(
    "host", ["", "user@host.example", "@host.example", "host.example/path", "host.example:bad"]
)
def test_invalid_request_origin_requires_explicit_public_url(host):
    with pytest.raises(ProviderError, match="public_api_url_unavailable"):
        deployment_api_base_url(request_for(host=host))
