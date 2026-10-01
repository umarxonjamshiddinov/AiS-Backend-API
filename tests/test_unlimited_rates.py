from datetime import UTC, datetime

import pytest
from conftest import completion, issue
from test_admin_remote import AdminDOM

from app.db.models import ApiKey, Usage
from app.gateway.providers import registry
from app.gateway.schemas import KeyInput
from app.manage import execute, parser


def test_unlimited_rpm_accepts_more_than_default_in_one_minute(gateway, monkeypatch):
    client, factory = gateway
    at = datetime.now(UTC)
    monkeypatch.setattr("app.gateway.quota.utcnow", lambda: at)
    key_id, _, headers = issue(client, rpm=None, budget_usd=None)
    for _ in range(32):
        assert completion(client, headers).status_code == 200
    assert len(registry.get("bedrock").calls) == 32
    with factory() as db:
        key = db.get(ApiKey, key_id)
        assert key.rpm is None and key.tpm == 100000


def test_unlimited_tpm_ignores_large_existing_token_usage(gateway):
    client, factory = gateway
    key_id, _, headers = issue(client, tpm=None, rpm=3, budget_usd=None)
    with factory.begin() as db:
        key = db.get(ApiKey, key_id)
        db.add(
            Usage(
                key_id=key.id,
                user_id=key.user_id,
                alias="ais-fast",
                provider="bedrock",
                upstream_model="private-model",
                status="completed",
                reserved_tokens=0,
                reserved_cost=0,
                input_price=0,
                output_price=0,
                accounted_tokens=10**12,
            )
        )
    assert completion(client, headers).status_code == 200
    with factory() as db:
        key = db.get(ApiKey, key_id)
        assert key.tpm is None and key.rpm == 3


@pytest.mark.parametrize(
    "limits,allowed,field",
    [
        ({"rpm": 1, "tpm": None}, 1, "rpm"),
        ({"rpm": None, "tpm": 1}, 0, "tpm"),
    ],
)
def test_finite_dimension_still_limits_an_otherwise_unlimited_key(gateway, limits, allowed, field):
    client, _ = gateway
    _, _, headers = issue(client, **limits)
    for _ in range(allowed):
        assert completion(client, headers).status_code == 200
    result = completion(client, headers)
    assert result.status_code == 429 and result.json()["error"]["param"] == field
    assert len(registry.get("bedrock").calls) == allowed


@pytest.mark.parametrize("field", ["requests_per_day", "requests_per_month"])
def test_unlimited_minute_limits_preserve_calendar_limits(gateway, field):
    client, _ = gateway
    _, _, headers = issue(client, rpm=None, tpm=None, **{field: 1})
    assert completion(client, headers).status_code == 200
    result = completion(client, headers)
    assert result.status_code == 429 and result.json()["error"]["param"] == field


def test_null_limits_roundtrip_and_partial_edits_preserve_values(gateway):
    client, factory = gateway
    key_id, _, _ = issue(client, rpm=7, tpm=12345)
    for changes, expected in (
        ({"rpm": None}, (None, 12345)),
        ({"tpm": None}, (None, None)),
        ({}, (None, None)),
        ({"rpm": 4, "tpm": 4000}, (4, 4000)),
        ({}, (4, 4000)),
    ):
        response = client.put(f"/admin/api/keys/{key_id}", json={"name": "edited", **changes})
        assert response.status_code == 200
        with factory() as db:
            key = db.get(ApiKey, key_id)
            assert (key.rpm, key.tpm) == expected
            user_id = key.user_id
        listed = client.get(f"/admin/api/users/{user_id}/keys").json()["data"][0]
        assert (listed["rpm"], listed["tpm"]) == expected
        overview = client.get("/admin/api/overview").json()
        visible = next(k for k in overview["keys"] if k["id"] == key_id)
        assert (visible["rpm"], visible["tpm"]) == expected


@pytest.mark.parametrize("field", ["rpm", "tpm"])
@pytest.mark.parametrize("value", [0, -1, "unlimited"])
def test_api_requires_null_or_positive_numeric_limits(gateway, field, value):
    client, _ = gateway
    response = client.post("/admin/api/keys", json={"name": "invalid", field: value})
    assert response.status_code == 422


def test_cli_unlimited_creates_null_limits_and_working_key(gateway, monkeypatch):
    client, factory = gateway
    monkeypatch.setattr("app.manage.session_factory", lambda: factory)
    owner = execute(parser().parse_args(["create-user", "--name", "unlimited-owner"]))
    created = execute(
        parser().parse_args(
            [
                "create-key",
                "--user",
                owner["id"],
                "--name",
                "CLI unlimited",
                "--model",
                "ais-fast",
                "--rpm",
                "unlimited",
                "--tpm",
                "unlimited",
            ]
        )
    )
    with factory() as db:
        key = db.get(ApiKey, created["id"])
        assert key.rpm is None and key.tpm is None
    assert completion(client, {"Authorization": "Bearer " + created["key"]}).status_code == 200


def test_omitted_limits_keep_existing_creation_defaults():
    payload = KeyInput(name="defaults")
    args = parser().parse_args(["create-key", "--user", "owner", "--name", "defaults"])
    assert payload.rpm == args.rpm == 30
    assert payload.tpm == args.tpm == 2000000


def test_admin_rate_inputs_allow_empty_unlimited_values(gateway):
    client, _ = gateway
    dom = AdminDOM(client.get("/admin").text)
    for name in ("key-rpm", "key-tpm"):
        control = next(item for item in dom.inputs if item.get("id") == name)
        assert "required" not in control
        assert control["placeholder"] == "Без лимита"
        assert control["min"] == "1"
