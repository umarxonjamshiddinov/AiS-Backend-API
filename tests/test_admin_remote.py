import hashlib
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest
from conftest import completion, issue
from pydantic import SecretStr
from sqlalchemy import select

from app.admin.assets import AdminAssets
from app.admin.routes import require_admin
from app.core.config import Settings
from app.db.models import ApiKey, KeyModel, ModelRoute, Usage
from app.gateway.capabilities import ModelCapabilities
from app.gateway.providers import registry
from scripts.seed_models import seed_models


class AdminDOM(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.ids = []
        self.assets = []
        self.inputs = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.ids.append(attrs["id"])
        if tag == "script" and attrs.get("src"):
            self.assets.append(attrs["src"])
        if tag == "link" and attrs.get("rel") == "stylesheet":
            self.assets.append(attrs["href"])
        if tag == "input":
            self.inputs.append(attrs)


def test_html_js_dom_contract_and_cache_delivery(gateway):
    client, _ = gateway
    page = client.get("/admin", headers={"Host": "api.myais.uz"})
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert client.get("/admin/").status_code == 200
    dom = AdminDOM(page.text)
    assert len(dom.ids) == len(set(dom.ids))
    assert len(dom.assets) == 2
    for url in dom.assets:
        assert re.fullmatch(r"/admin/assets/[a-f0-9]{20}/admin\.(js|css)", url)
        asset = client.get(url)
        assert asset.status_code == 200
        digest = hashlib.sha256(asset.content).hexdigest()[:20]
        assert digest == url.split("/")[3]
        assert "immutable" in asset.headers["cache-control"]
        assert client.get(url, headers={"If-None-Match": asset.headers["etag"]}).status_code == 304
        if url.endswith(".js"):
            script = asset.text
    # Includes every literal lookup and IDs passed through quotaFields dynamically.
    lookups = set(re.findall(r'\$\("([a-z0-9-]+)"\)', script))
    lookups.update(re.findall(r'"(key-[a-z-]+)":', script))
    assert lookups <= set(dom.ids)
    assert 'fetch("/admin/api" + path' in script
    assert 'mode: "same-origin"' in script
    assert "https://api.myais.uz/v1" in page.text
    assert "http://127.0.0.1" not in page.text + script
    assert "http://localhost" not in page.text + script
    assert 'value="openai"' not in page.text
    assert client.get("/static/admin.js").headers["cache-control"] == "no-store"
    assert client.get("/admin/assets/invalid/admin.js").status_code == 404
    token_input = next(a for a in dom.inputs if a.get("id") == "admin-token")
    assert token_input["type"] == "password"
    assert "value" not in token_input and "name" not in token_input


def test_changed_script_gets_new_url_and_snapshot_stays_consistent():
    html = '<script src="/static/admin.js?v=old"></script><link href="/static/admin.css">'
    files = {"admin.js": b"first version", "admin.css": b"body{}"}
    original = AdminAssets(html, files)
    files["admin.js"] = b"second version"
    updated = AdminAssets(html, files)
    assert original.html != updated.html
    old_digest = hashlib.sha256(b"first version").hexdigest()[:20]
    assert original.response(old_digest, "admin.js", None).body == b"first version"
    assert "/static/" not in updated.html


def authorize(client, monkeypatch):
    token = "unit-only-admin-token"
    monkeypatch.setattr(
        "app.admin.routes.get_settings", lambda: SimpleNamespace(admin_token=SecretStr(token))
    )
    client.app.dependency_overrides.pop(require_admin)
    return token, {"Authorization": "Bearer " + token}


def test_admin_auth_and_secret_free_responses(gateway, monkeypatch):
    client, _ = gateway
    token, headers = authorize(client, monkeypatch)
    for supplied in ({}, {"Authorization": "Bearer invalid-test"}):
        result = client.get("/admin/api/overview", headers=supplied)
        assert result.status_code == 401
        assert token not in result.text
        assert (
            client.post("/admin/api/keys", headers=supplied, json={"name": "x"}).status_code == 401
        )
    result = client.get("/admin/api/overview", headers=headers)
    assert result.status_code == 200
    assert set(result.json()["providers"]) == {"bedrock", "amazon_bedrock"}
    assert token not in result.text and "key_hash" not in result.text
    assert "aws_bearer_token_bedrock" not in result.text.lower()
    assert "openai_api_key" in Settings.model_fields
    assert "unit-upstream-secret" not in repr(
        Settings(_env_file=None, openai_api_key="unit-upstream-secret")
    )


def test_authenticated_admin_key_crud_persists_all_limits(gateway, monkeypatch):
    client, factory = gateway
    _, headers = authorize(client, monkeypatch)
    created = client.post(
        "/admin/api/keys",
        headers=headers,
        json={
            "name": "remote client",
            "models": ["ais-fast"],
            "budget_usd": None,
        },
    ).json()
    key_id, secret = created["id"], created["key"]
    expires = datetime.now(UTC) + timedelta(days=5)
    payload = {
        "name": "remote edited",
        "models": ["ais-fast"],
        "rpm": 7,
        "tpm": 12345,
        "requests_per_day": 11,
        "requests_per_month": 21,
        "tokens_per_day": 23456,
        "tokens_per_month": 34567,
        "max_concurrent_requests": 2,
        "expires_at": expires.isoformat(),
        "budget_usd": "0.25",
        "active": False,
    }
    assert client.put("/admin/api/keys/" + key_id, headers=headers, json=payload).status_code == 200
    with factory() as db:
        key = db.get(ApiKey, key_id)
        for name in (
            "name",
            "rpm",
            "tpm",
            "requests_per_day",
            "requests_per_month",
            "tokens_per_day",
            "tokens_per_month",
            "max_concurrent_requests",
            "active",
        ):
            assert getattr(key, name) == payload[name]
        assert key.expires_at == expires and key.budget_usd == Decimal("0.25")
        assert key.key_hash != secret
    client_headers = {"Authorization": "Bearer " + secret}
    assert client.get("/v1/models", headers=client_headers).status_code == 401
    payload.update(active=True, models=[], budget_usd=None)
    assert client.put("/admin/api/keys/" + key_id, headers=headers, json=payload).status_code == 200
    assert client.get("/v1/models", headers=client_headers).json()["data"] == []
    payload["models"] = ["ais-fast"]
    client.put("/admin/api/keys/" + key_id, headers=headers, json=payload)
    assert client.get("/v1/models", headers=client_headers).json()["data"][0]["id"] == "ais-fast"
    assert secret not in client.get("/admin/api/overview", headers=headers).text
    assert client.post("/admin/api/keys/" + key_id + "/revoke", headers=headers).status_code == 200
    assert completion(client, client_headers).json()["error"]["code"] == "revoked_api_key"


def test_authenticated_admin_model_changes_are_persisted(gateway, monkeypatch):
    client, factory = gateway
    _, headers = authorize(client, monkeypatch)
    body = {
        "alias": "remote-model",
        "upstream_model": "confirmed-id",
        "provider": "bedrock",
        "enabled": False,
        "external_api_allowed": False,
        "max_output_tokens": 500,
        "max_context_tokens": 10000,
        "capabilities": ModelCapabilities().model_dump(),
    }
    assert client.put("/admin/api/models", headers=headers, json=body).status_code == 200
    body.update(enabled=True, external_api_allowed=True)
    body["capabilities"]["streaming"] = True
    assert client.put("/admin/api/models", headers=headers, json=body).status_code == 200
    with factory() as db:
        row = db.get(ModelRoute, "remote-model")
        assert row.enabled and row.external_api_allowed and row.capabilities["streaming"]
        assert row.upstream_model == "confirmed-id" and row.max_output_tokens == 500
    body.update(enabled=False, external_api_allowed=False)
    client.put("/admin/api/models", headers=headers, json=body)
    with factory() as db:
        row = db.get(ModelRoute, "remote-model")
        assert not row.enabled and not row.external_api_allowed


def test_openai_upstream_cannot_be_configured(gateway):
    client, _ = gateway
    assert set(registry.providers) == {"bedrock", "amazon_bedrock"}
    response = client.put(
        "/admin/api/models",
        json={"alias": "unsupported", "provider": "openai", "upstream_model": "gpt-4o"},
    )
    assert response.status_code == 422
    response = client.put(
        "/admin/api/credentials", json={"provider": "openai", "api_key": "not-a-real-key"}
    )
    assert response.status_code == 422 and "not-a-real-key" not in response.text


@pytest.mark.parametrize("reference", ["none", "permission", "usage", "enabled"])
def test_legacy_seed_cleanup_preserves_references(gateway, reference):
    client, factory = gateway
    key_id, _, headers = issue(client)
    with factory.begin() as db:
        db.add(
            ModelRoute(
                alias="gpt-4o",
                provider="openai",
                upstream_model="gpt-4o",
                enabled=reference == "enabled",
                provider_transport="openai_chat",
                input_price=1,
                output_price=1,
                capabilities={},
            )
        )
        db.flush()
        if reference == "permission":
            db.add(KeyModel(key_id=key_id, alias="gpt-4o"))
        if reference == "usage":
            db.add(
                Usage(
                    key_id=key_id,
                    alias="gpt-4o",
                    provider="openai",
                    status="completed",
                    reserved_tokens=0,
                    reserved_cost=0,
                    input_price=1,
                    output_price=1,
                )
            )
    seed_models(factory)
    with factory() as db:
        assert (db.get(ModelRoute, "gpt-4o") is None) == (reference == "none")
        assert db.get(ApiKey, key_id) is not None
        if reference == "usage":
            assert db.scalar(select(Usage).where(Usage.alias == "gpt-4o")) is not None
    assert all(
        m["id"] != "gpt-4o" for m in client.get("/v1/models", headers=headers).json()["data"]
    )
    assert completion(client, headers, model="gpt-4o").status_code == 404
    assert all(
        m["provider"] in {"bedrock", "amazon_bedrock"}
        for m in client.get("/admin/api/overview").json()["models"]
    )
