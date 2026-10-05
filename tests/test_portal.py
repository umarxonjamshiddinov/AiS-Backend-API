import json

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.portal import configured_portal_directory, mount_portal


@pytest.fixture
def portal(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    (release / "assets").mkdir()
    (release / "assets/app-123.js").write_text("console.log('portal');", encoding="utf-8")
    (release / "index.html").write_text("<!doctype html><title>AiS API</title>", encoding="utf-8")
    (release / "favicon.svg").write_text("<svg></svg>", encoding="utf-8")
    (release / "robots.txt").write_text("User-agent: *\nAllow: /", encoding="utf-8")
    (release / "sitemap.xml").write_text("<urlset></urlset>", encoding="utf-8")
    return release


def portal_app(directory):
    application = create_app(maintenance=False)
    mount_portal(application, directory)
    return application


def test_portal_get_head_and_cache_policy(portal):
    with TestClient(portal_app(portal)) as client:
        root = client.get("/")
        assert root.status_code == 200
        assert "AiS API" in root.text
        assert root.headers["content-type"].startswith("text/html")
        assert root.headers["cache-control"] == "no-cache"
        assert client.head("/").status_code == 200
        assert client.head("/").content == b""
        for name in ("favicon.svg", "robots.txt", "sitemap.xml"):
            assert client.get(f"/{name}").status_code == 200
            assert client.head(f"/{name}").status_code == 200
        asset = client.get("/assets/app-123.js")
        assert asset.status_code == 200
        assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_portal_keeps_api_errors_and_admin_assets(portal):
    with TestClient(portal_app(portal)) as client:
        assert client.get("/health").json() == {"status": "ok"}
        unauthorized = client.get("/v1/models")
        assert unauthorized.status_code == 401
        assert unauthorized.json()["error"]["code"] == "invalid_api_key"
        for path in ("/v1/unknown", "/v1/assets/app-123.js"):
            response = client.get(path)
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "not_found"
        assert client.get("/admin").status_code == 200
        assert client.post("/v1/safety/assess", json={}).status_code == 401
        assert client.get("/docs").status_code == 200
        assert client.get("/redoc").status_code == 200
        assert client.get("/static/admin.js").status_code == 200
        assert client.get("/assets/missing.js").status_code == 404
        assert client.get("/assets/%2e%2e/index.html").status_code == 404
        assert client.get("/unknown-page").status_code == 404
        assert client.post("/").status_code == 405
        assert "/" not in client.get("/openapi.json").json()["paths"]


def test_incomplete_release_does_not_interrupt_api(tmp_path):
    with TestClient(portal_app(tmp_path)) as client:
        assert client.get("/").status_code == 404
        assert client.get("/health").status_code == 200


def test_manifest_and_environment_selection(tmp_path, monkeypatch):
    monkeypatch.setattr("app.portal.PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("AIS_PORTAL_DIR", raising=False)
    assert configured_portal_directory() is None
    manifest = tmp_path / ".runtime/portal/active.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"directory": str(tmp_path)}), encoding="utf-8-sig")
    assert configured_portal_directory() == tmp_path
    manifest.write_text("invalid json", encoding="utf-8")
    assert configured_portal_directory() is None
    monkeypatch.setenv("AIS_PORTAL_DIR", str(tmp_path / "override"))
    assert configured_portal_directory() == tmp_path / "override"
    monkeypatch.setenv("AIS_PORTAL_DIR", "relative/release")
    assert configured_portal_directory() is None
