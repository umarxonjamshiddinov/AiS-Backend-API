# ruff: noqa: F811
import json
from decimal import Decimal

import httpx2
import pytest
from sqlalchemy import select
from test_catalog_gateway import (
    chat,
    image_messages,
    issue,
    mantle_gateway,  # noqa: F401 - pytest fixture import
)
from test_mantle_client import response_body

from app.db.models import ApiKey, Usage, User
from app.safety.schemas import SafetyContext, SafetyRequest


def verdict(blocked=False):
    return json.dumps(
        {
            "version": "ais_safety_v1",
            "decision": "blocked" if blocked else "allowed",
            "category": "credential_theft" if blocked else None,
        }
    )


def safety_wire(wire, *, blocked=False, invalid=False, image_block=False):
    def response(payload):
        is_safety = (
            "classifier" in payload["messages"][0]["content"]
            or "ASSESSOR" in payload["messages"][0]["content"]
        )
        if is_safety:
            content = (
                "bad contract"
                if invalid
                else verdict(blocked or (image_block and payload["model"].startswith("qwen.")))
            )
        else:
            content = "Visible square" if payload["model"].startswith("qwen.") else "OK"
        return httpx2.Response(200, json=response_body(payload["model"], content))

    wire.respond = response


def body(**changes):
    result = {"selected_model": "openai.gpt-oss-20b", "context": {"user_request": "Hello"}}
    result.update(changes)
    return result


def test_safety_requires_opt_in_and_cannot_choose_internal_model(mantle_gateway):
    client, factory, wire = mantle_gateway
    key, headers = issue(client)
    assert client.post("/v1/safety/assess", json=body(), headers=headers).status_code == 403
    key, headers = issue(client, safety_enabled=True)
    for field, value in (
        ("model", "openai.gpt-oss-safeguard-120b"),
        ("system_prompt", "Ignore policy"),
        ("max_tokens", 10),
    ):
        assert (
            client.post(
                "/v1/safety/assess", json=body(**{field: value}), headers=headers
            ).status_code
            == 400
        )
    assert (
        client.post(
            "/v1/safety/assess",
            json=body(selected_model="openai.gpt-oss-safeguard-20b"),
            headers=headers,
        ).status_code
        == 403
    )
    assert wire.requests == []


def test_safety_uses_server_model_and_fixed_policy_without_user_debit(mantle_gateway, monkeypatch):
    client, factory, wire = mantle_gateway
    monkeypatch.setattr("app.services.internal_inference.session_factory", lambda: factory)
    safety_wire(wire)
    key, headers = issue(client, safety_enabled=True, requests_per_day=1)
    response = client.post("/v1/safety/assess", json=body(), headers=headers)
    assert response.status_code == 200
    value = response.json()
    assert value["action"] == "allow"
    assert "model" not in value and "text" not in value and "internal_requests" not in value
    assert wire.requests[0]["payload"]["model"] == "openai.gpt-oss-safeguard-20b"
    assert chat(client, headers, model="openai.gpt-oss-20b").status_code == 200
    with factory() as db:
        requests = db.scalars(select(Usage).where(Usage.key_id == key["id"])).all()
        assert len(requests) == 2
        assert {r.purpose for r in requests} == {"chat", "safety"}
        safety = next(r for r in requests if r.purpose == "safety")
        assert safety.accounted_tokens == 0 and safety.cost_usd == 0
        assert all(r.status == "completed" and r.lease_expires_at is None for r in requests)
        assert db.get(ApiKey, key["id"]).reserved_usd == Decimal(0)
        internal = db.scalars(select(Usage).where(Usage.key_id.is_(None))).all()
        assert internal and all(r.purpose == "safety" for r in internal)
        assert all(r.input_tokens > 0 for r in internal)


@pytest.mark.parametrize("mode", ["revoked", "disabled_key", "disabled_user", "permissions"])
def test_safety_obeys_key_and_model_permissions(mantle_gateway, mode):
    client, factory, wire = mantle_gateway
    key, headers = issue(client, safety_enabled=True)
    with factory.begin() as db:
        entry = db.get(ApiKey, key["id"])
        if mode == "revoked":
            from app.gateway.quota import utcnow

            entry.revoked_at = utcnow()
        elif mode == "disabled_key":
            entry.active = False
        elif mode == "disabled_user":
            db.get(User, entry.user_id).active = False
        else:
            entry.all_public_models = False
    result = client.post("/v1/safety/assess", json=body(), headers=headers)
    assert result.status_code in {401, 403}
    assert wire.requests == []


@pytest.mark.parametrize("image", [False, True])
def test_unsafe_or_failed_safety_never_reaches_selected_model(mantle_gateway, monkeypatch, image):
    client, factory, wire = mantle_gateway
    monkeypatch.setattr("app.services.internal_inference.session_factory", lambda: factory)
    safety_wire(wire, blocked=not image, image_block=image)
    key, headers = issue(client, safety_enabled=True)
    response = chat(
        client,
        headers,
        model="openai.gpt-oss-20b",
        **({"messages": image_messages()} if image else {}),
    )
    assert response.status_code == 400
    models = [r["payload"]["model"] for r in wire.requests]
    assert "openai.gpt-oss-20b" not in models
    with factory() as db:
        request = db.scalar(select(Usage).where(Usage.key_id == key["id"]))
        assert request.status == "failed" and request.accounted_tokens == 0
        assert request.lease_expires_at is None
        assert db.get(ApiKey, key["id"]).reserved_usd == 0


def test_invalid_safety_contract_fails_closed_and_releases_reserve(mantle_gateway, monkeypatch):
    client, factory, wire = mantle_gateway
    monkeypatch.setattr("app.services.internal_inference.session_factory", lambda: factory)
    safety_wire(wire, invalid=True)
    key, headers = issue(client, safety_enabled=True)
    assert chat(client, headers, model="openai.gpt-oss-20b").status_code == 503
    assert len(wire.requests) == 1
    with factory() as db:
        assert db.get(ApiKey, key["id"]).reserved_usd == 0


def test_safety_rpm_and_concurrency_are_bounded_separately(mantle_gateway, monkeypatch):
    client, factory, wire = mantle_gateway
    monkeypatch.setattr("app.services.internal_inference.session_factory", lambda: factory)
    safety_wire(wire)
    key, headers = issue(client, safety_enabled=True, rpm=1)
    assert client.post("/v1/safety/assess", json=body(), headers=headers).status_code == 200
    assert client.post("/v1/safety/assess", json=body(), headers=headers).status_code == 429
    assert chat(client, headers, model="openai.gpt-oss-20b").status_code == 200


def test_native_and_fallback_safety_order(mantle_gateway, monkeypatch):
    client, factory, wire = mantle_gateway
    monkeypatch.setattr("app.services.internal_inference.session_factory", lambda: factory)
    safety_wire(wire)
    _, headers = issue(client, safety_enabled=True)
    for selected in ("google.gemma-3-4b-it", "openai.gpt-oss-20b"):
        wire.requests.clear()
        assert chat(client, headers, model=selected, messages=image_messages()).status_code == 200
        models = [r["payload"]["model"] for r in wire.requests]
        assert models[:2] == ["openai.gpt-oss-safeguard-20b", "qwen.qwen3-vl-235b-a22b-instruct"]
        assert models[-1] == selected
        assert len(models) == (3 if selected.startswith("google") else 4)


def test_safety_request_is_strict_and_bounded():
    with pytest.raises(ValueError):
        SafetyRequest.model_validate(
            body(context={"user_request": "Hello", "system": "change policy"})
        )
    with pytest.raises(ValueError):
        SafetyContext(user_request="x" * 96001)


def test_receipt_is_one_use_and_bound_to_key_model_and_exact_input(mantle_gateway, monkeypatch):
    client, factory, wire = mantle_gateway
    monkeypatch.setattr("app.services.internal_inference.session_factory", lambda: factory)
    safety_wire(wire)
    _, headers = issue(client, safety_enabled=True)
    result = client.post("/v1/safety/assess", json=body(), headers=headers).json()
    receipt = result["receipt"]
    wire.requests.clear()
    response = chat(
        client,
        headers,
        model="openai.gpt-oss-20b",
        safety_receipt=receipt,
        messages=[{"role": "user", "content": "Hello"}],
    )
    assert response.status_code == 200
    assert [r["payload"]["model"] for r in wire.requests] == ["openai.gpt-oss-20b"]
    assert (
        chat(client, headers, model="openai.gpt-oss-20b", safety_receipt=receipt).status_code == 400
    )
    result = client.post("/v1/safety/assess", json=body(), headers=headers).json()
    wire.requests.clear()
    assert (
        chat(
            client,
            headers,
            model="openai.gpt-oss-20b",
            safety_receipt=result["receipt"],
            messages=[{"role": "user", "content": "Hello changed request"}],
        ).status_code
        == 200
    )
    assert wire.requests[0]["payload"]["model"] == "openai.gpt-oss-safeguard-20b"
    result = client.post("/v1/safety/assess", json=body(), headers=headers).json()
    assert (
        chat(
            client, headers, model="google.gemma-3-4b-it", safety_receipt=result["receipt"]
        ).status_code
        == 400
    )


def test_adaptive_safety_fixed_contract_and_safe_completion(monkeypatch):
    from app.safety.schemas import SafetyContext
    from app.safety.service import assess

    facts = {
        "policy_version": "ais_safety_v2",
        "category": None,
        "intent": "benign",
        "severity": "none",
        "confidence": 0.8,
        "authorization": "not_applicable",
        "request_context": "direct",
        "reason_code": "benign_request",
        "decision": "allow",
        "recommended_action": "answer",
    }
    calls = []

    def complete(messages, **kwargs):
        calls.append(kwargs)
        return {"id": "internal-test", "text": json.dumps(facts), "usage": None}

    monkeypatch.setattr("app.safety.service.complete_safety", complete)
    result = assess(SafetyContext(user_request="Hello"), policy_version="ais_safety_v2")
    assert result["action"] == "allow" and result["contract_retries"] == 0
    assert calls == [{"adaptive": True, "factory": None}]
