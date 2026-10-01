import json
import logging
from uuid import uuid4

import httpx2
import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from openai import OpenAI
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker
from test_mantle_client import PNG, UPSTREAM_SECRET, response_body, settings
from test_openai_client import sdk_client

from app.admin.routes import require_admin
from app.catalog import load_bootstrap_catalog
from app.db.models import ApiKey, Base, KeyModel, ModelRoute, Usage, User
from app.gateway.providers import registry
from app.main import create_app
from app.providers.mantle import MantleAdapter, MantleClient
from app.services.internal_inference import complete_safety
from app.services.models import resolve_model
from scripts.seed_models import seed_models

OPENAI_ROUTE_IDS = {
    "xai.grok-4.3",
    "xai.grok-4.6",
    "google.gemma-4-31b",
    "google.gemma-4-e2b",
    "google.gemma-4-26b-a4b",
}
SAFEGUARDS = {"openai.gpt-oss-safeguard-20b", "openai.gpt-oss-safeguard-120b"}
EXPECTED_IDS = set(
    """
xai.grok-4.3 xai.grok-4.6 google.gemma-4-31b google.gemma-4-e2b google.gemma-4-26b-a4b
writer.palmyra-vision-7b zai.glm-5 qwen.qwen3-coder-next zai.glm-4.7-flash
moonshotai.kimi-k2.5 zai.glm-4.7 minimax.minimax-m2.5 minimax.minimax-m2.1 deepseek.v3.2
nvidia.nemotron-super-3-120b mistral.devstral-2-123b
nvidia.nemotron-nano-3-30b qwen.qwen3-next-80b-a3b-instruct qwen.qwen3-32b
google.gemma-3-12b-it google.gemma-3-4b-it openai.gpt-oss-120b minimax.minimax-m2
mistral.voxtral-small-24b-2507 deepseek.v3.1 google.gemma-3-27b-it
qwen.qwen3-coder-480b-a35b-instruct openai.gpt-oss-safeguard-20b openai.gpt-oss-safeguard-120b
mistral.voxtral-mini-3b-2507 qwen.qwen3-coder-30b-a3b-instruct
moonshotai.kimi-k2-thinking qwen.qwen3-vl-235b-a22b-instruct
mistral.magistral-small-2509 qwen.qwen3-235b-a22b-2507 openai.gpt-oss-20b
mistral.mistral-large-3-675b-instruct mistral.ministral-3-14b-instruct
mistral.ministral-3-8b-instruct mistral.ministral-3-3b-instruct
nvidia.nemotron-nano-12b-v2 nvidia.nemotron-nano-9b-v2
""".split()
)


class Wire:
    def __init__(self):
        self.requests = []
        self.respond = None

    def __call__(self, request):
        payload = json.loads(request.content)
        self.requests.append(
            {
                "url": str(request.url),
                "payload": payload,
                "authorization": request.headers.get("authorization"),
            }
        )
        if self.respond:
            return self.respond(payload)
        content = (
            "Visible image: a small square."
            if payload["model"].startswith("qwen.qwen3-vl")
            else "OK"
        )
        return httpx2.Response(200, json=response_body(payload["model"], content))


@pytest.fixture
def mantle_gateway(isolated_database, monkeypatch):
    with isolated_database.begin() as connection:
        config = Config("alembic.ini")
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
    factory = sessionmaker(isolated_database, expire_on_commit=False)
    for target in (
        "app.gateway.quota.session_factory",
        "app.admin.routes.session_factory",
        "app.api.routes.session_factory",
        "app.manage.session_factory",
    ):
        monkeypatch.setattr(target, lambda: factory)
    wire = Wire()

    def sdk_factory(**options):
        return OpenAI(
            **options,
            http_client=httpx2.Client(
                transport=httpx2.MockTransport(wire),
                trust_env=False,
                follow_redirects=False,
            ),
        )

    adapter = MantleAdapter(MantleClient(client_factory=sdk_factory, settings=settings()))
    monkeypatch.setitem(registry.providers, "amazon_bedrock", adapter)
    application = create_app(maintenance=False)
    application.dependency_overrides[require_admin] = lambda: None
    with TestClient(application) as client:
        yield client, factory, wire


def issue(client, **changes):
    user = client.post("/admin/api/users", json={"username": "test-" + uuid4().hex})
    assert user.status_code == 200
    payload = {
        "name": "unit key",
        "user_id": user.json()["id"],
        "all_public_models": True,
        "tpm": 2000000,
    }
    payload.update(changes)
    created = client.post("/admin/api/keys", json=payload)
    assert created.status_code == 200
    value = created.json()
    return value, {"Authorization": "Bearer " + value["key"]}


def chat(client, headers, model="xai.grok-4.3", **changes):
    payload = {"model": model, "messages": [{"role": "user", "content": "Hi"}]}
    payload.update(changes)
    return client.post("/v1/chat/completions", json=payload, headers=headers)


def image_messages():
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe this image"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + PNG}},
            ],
        }
    ]


def test_full_export_catalog_has_exact_ids_routes_regions_and_honest_capabilities():
    models = load_bootstrap_catalog()
    assert len(models) == 42
    assert sum(m["is_public"] for m in models) == 40
    assert sum(not m["is_public"] for m in models) == 2
    assert {m["model_id"] for m in models} == EXPECTED_IDS
    assert len({m["model_key"] for m in models}) == 42
    assert {m["model_id"] for m in models if not m["is_public"]} == SAFEGUARDS
    assert {m["model_id"] for m in models if m["api_route"] == "mantle_openai"} == OPENAI_ROUTE_IDS
    for m in models:
        assert m["backend"] == "amazon_bedrock"
        assert m["context_window"] > 0
        assert m["manufacturer"] and m["display_name"] and m["model_id"] and m["api_route"]
        assert m["region"] == ("us-west-2" if m["model_key"] == "grok_4_6" else "us-east-1")
        assert not m["metadata"]["audio_runtime"] and not m["metadata"]["video_runtime"]
        assert not m["metadata"]["reasoning_control_runtime"]
    assert sum(m["supports_vision"] for m in models) == 16
    assert sum(m["vision_behavior"] == "vision_fallback" for m in models) == 24
    assert {m["model_key"] for m in models if m["supports_reasoning"]} == {"grok_4_6"}
    assert "openai.gpt-6-astra" not in EXPECTED_IDS


def test_empty_db_migrations_seed_counts_and_model_resolver(mantle_gateway):
    client, factory, _ = mantle_gateway
    assert client.get("/ready").json()["catalog_models"] == 42
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(ModelRoute)) == 42
        assert (
            db.scalar(select(func.count()).select_from(ModelRoute).where(ModelRoute.is_public))
            == 40
        )
        for model in load_bootstrap_catalog():
            row = resolve_model(db, model["model_key"])
            assert resolve_model(db, model["model_id"]).alias == row.alias
            assert row.upstream_model == model["model_id"]
            assert row.region == model["region"] and row.api_route == model["api_route"]
            assert row.max_output_tokens == model["max_output"]
            assert row.max_context_tokens == model["context_window"]
            assert not row.capabilities.get("streaming") and not row.capabilities.get("tools")


def test_seed_is_idempotent_and_db_edits_survive(mantle_gateway):
    _, factory, _ = mantle_gateway
    with factory.begin() as db:
        row = resolve_model(db, "grok_4_3")
        row.enabled = False
        row.region = "us-west-2"
        row.max_output_tokens = 123
    assert seed_models(factory)["inserted"] == 0
    assert seed_models(factory)["inserted"] == 0
    with factory() as db:
        row = resolve_model(db, "grok_4_3")
        assert not row.enabled and row.region == "us-west-2" and row.max_output_tokens == 123
        assert db.scalar(select(func.count()).select_from(ModelRoute)) == 42


def test_same_key_lists_40_public_models_and_calls_four_configurations(mantle_gateway):
    client, factory, wire = mantle_gateway
    value, headers = issue(client)
    with sdk_client(client, value["key"]) as sdk:
        listed = {m.id for m in sdk.models.list()}
        assert listed == EXPECTED_IDS - SAFEGUARDS and len(listed) == 40
        for model in ("xai.grok-4.3", "moonshotai.kimi-k2.5", "zai.glm-5", "xai.grok-4.6"):
            result = sdk.chat.completions.create(
                model=model, messages=[{"role": "user", "content": "Hi"}]
            )
            assert result.choices[0].message.content == "OK" and result.model == model
            assert result.usage.total_tokens == 6
    assert wire.requests[0]["url"].endswith("us-east-1.api.aws/openai/v1/chat/completions")
    assert wire.requests[1]["url"].endswith("us-east-1.api.aws/v1/chat/completions")
    assert wire.requests[3]["url"].endswith("us-west-2.api.aws/openai/v1/chat/completions")
    assert all(r["authorization"] == "Bearer " + UPSTREAM_SECRET for r in wire.requests)
    with factory() as db:
        rows = db.scalars(select(Usage).where(Usage.key_id == value["id"])).all()
        assert len(rows) == 4
        assert {r.user_id for r in rows} == {value["user_id"]}
        assert all(
            r.model_key and r.region and r.api_route and r.status == "completed" for r in rows
        )
        assert all(db.get(ModelRoute, r.alias) and r.input_tokens == 4 for r in rows)
        assert db.get(ApiKey, value["id"]).last_used_at is not None


@pytest.mark.parametrize("model", sorted(SAFEGUARDS))
def test_internal_models_hidden_and_blocked_even_with_explicit_grants(mantle_gateway, model):
    client, factory, wire = mantle_gateway
    value, headers = issue(client, models=[model])
    with factory.begin() as db:
        db.get(ModelRoute, model).external_api_allowed = True
    response = chat(client, headers, model)
    assert response.status_code == 403 and response.json()["error"]["code"] == "model_not_allowed"
    assert model not in {m["id"] for m in client.get("/v1/models", headers=headers).json()["data"]}
    assert wire.requests == []
    with factory() as db:
        assert db.get(KeyModel, (value["id"], model)) is not None


def test_auth_key_lifecycle_disabled_user_and_secret_free_lists(mantle_gateway, caplog):
    client, factory, _ = mantle_gateway
    value, headers = issue(client)
    token = value["key"]
    with caplog.at_level(logging.DEBUG):
        assert client.get("/v1/models", headers=headers).status_code == 200
        invalid = client.get("/v1/models", headers={"Authorization": "Bearer sk-myais-invalid"})
        assert invalid.status_code == 401 and invalid.json()["error"]["code"] == "invalid_api_key"
        client.put("/admin/api/users/" + value["user_id"], json={"active": False})
        response = chat(client, headers)
        assert response.status_code == 403 and response.json()["error"]["code"] == "inactive_user"
        assert client.get("/v1/models", headers=headers).status_code == 403
        client.put("/admin/api/users/" + value["user_id"], json={"active": True})
        assert client.post("/admin/api/keys/" + value["id"] + "/revoke").status_code == 200
        revoked = chat(client, headers)
        assert revoked.status_code == 401 and revoked.json()["error"]["code"] == "revoked_api_key"
    listed = client.get("/admin/api/users/" + value["user_id"] + "/keys")
    assert listed.status_code == 200 and len(listed.json()["data"]) == 1
    assert token not in listed.text + caplog.text and "key_hash" not in listed.text
    assert UPSTREAM_SECRET not in listed.text + caplog.text
    with factory() as db:
        key = db.get(ApiKey, value["id"])
        assert key.key_hash != token and len(key.key_hash) == 64
        assert token not in repr(key.__dict__) and key.user_id == value["user_id"]


def test_model_permissions_enabled_status_and_unknown_id(mantle_gateway):
    client, _, wire = mantle_gateway
    _, headers = issue(client, all_public_models=False, models=["xai.grok-4.3"])
    assert chat(client, headers).status_code == 200
    assert chat(client, headers, "zai.glm-5").status_code == 403
    assert chat(client, headers, "does-not-exist").status_code == 404
    changed = client.put(
        "/admin/api/models",
        json={"alias": "xai.grok-4.3", "upstream_model": "xai.grok-4.3", "enabled": False},
    )
    assert changed.status_code == 200
    assert chat(client, headers).status_code == 404
    assert client.get("/v1/models", headers=headers).json()["data"] == []
    assert len(wire.requests) == 1


@pytest.mark.parametrize(
    "model,limit",
    [
        ("writer.palmyra-vision-7b", 1000),
        ("zai.glm-4.7", 4000),
        ("zai.glm-4.7-flash", 4000),
        ("xai.grok-4.3", 4096),
        ("zai.glm-5", 4096),
    ],
)
def test_exported_output_limits_and_explicit_validation(mantle_gateway, model, limit):
    client, _, wire = mantle_gateway
    _, headers = issue(client)
    assert chat(client, headers, model).status_code == 200
    assert wire.requests[0]["payload"]["max_tokens"] == limit
    assert chat(client, headers, model, max_tokens=limit + 1).status_code == 400
    assert chat(client, headers, model, max_completion_tokens=16).status_code == 200
    assert wire.requests[-1]["payload"]["max_tokens"] == 16


@pytest.mark.parametrize(
    "changes",
    [
        {"stream": True},
        {"temperature": 0},
        {"reasoning_effort": "high"},
        {"tools": [{"type": "function", "function": {"name": "f"}}]},
        {"response_format": {"type": "json_object"}},
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_audio", "input_audio": {"data": "AA==", "format": "wav"}}
                    ],
                }
            ]
        },
        {
            "messages": [
                {"role": "user", "content": [{"type": "video_url", "video_url": {"url": "test"}}]}
            ]
        },
    ],
)
def test_unwired_capabilities_fail_before_upstream(mantle_gateway, changes):
    client, _, wire = mantle_gateway
    _, headers = issue(client)
    response = chat(client, headers, "xai.grok-4.6", **changes)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_capability"
    assert wire.requests == []


@pytest.mark.parametrize("model,native", [("moonshotai.kimi-k2.5", True), ("zai.glm-5", False)])
def test_native_vs_qwen_fallback_and_separate_usage(mantle_gateway, model, native):
    client, factory, wire = mantle_gateway
    value, headers = issue(client)
    response = chat(client, headers, model, messages=image_messages())
    assert response.status_code == 200
    assert len(wire.requests) == (1 if native else 2)
    payload = wire.requests[-1]["payload"]
    assert payload["model"] == model
    if native:
        assert "image_url" in json.dumps(payload) and PNG in json.dumps(payload)
    else:
        assert wire.requests[0]["payload"]["model"] == "qwen.qwen3-vl-235b-a22b-instruct"
        assert wire.requests[0]["payload"]["max_tokens"] == 2048
        assert "image_url" not in json.dumps(payload) and PNG not in json.dumps(payload)
        assert "Visible image" in json.dumps(payload)
    with factory() as db:
        entry = db.scalar(select(Usage).where(Usage.key_id == value["id"]))
        assert entry.user_id == value["user_id"] and entry.accounted_tokens == 6
        assert response.headers["x-request-id"] == entry.id
        assert response.json()["id"] == "chatcmpl-" + entry.id
        assert ("vision_helper" in entry.execution_metadata) != native
        assert entry.execution_metadata["inferences"] == (1 if native else 2)
        assert PNG not in repr(entry.__dict__)


def test_failed_helper_does_not_call_main_or_debit_its_tokens(mantle_gateway):
    client, factory, wire = mantle_gateway
    value, headers = issue(client)
    wire.respond = lambda payload: httpx2.Response(
        429, json={"error": {"message": UPSTREAM_SECRET}}
    )
    response = chat(client, headers, "zai.glm-5", messages=image_messages())
    assert response.status_code == 429 and UPSTREAM_SECRET not in response.text
    assert len(wire.requests) == 3
    assert all(r["payload"]["model"] == "qwen.qwen3-vl-235b-a22b-instruct" for r in wire.requests)
    with factory() as db:
        entry = db.scalar(select(Usage).where(Usage.key_id == value["id"]))
        assert entry.status == "failed" and entry.accounted_tokens == 0
        assert entry.execution_metadata["vision_helper"]["attempts"] == 3


def test_missing_usage_succeeds_with_explicit_estimate(mantle_gateway):
    client, factory, wire = mantle_gateway
    value, headers = issue(client)
    wire.respond = lambda payload: httpx2.Response(200, json=response_body(usage=None))
    response = chat(client, headers)
    assert response.status_code == 200 and response.json()["usage"] is None
    with factory() as db:
        entry = db.scalar(select(Usage).where(Usage.key_id == value["id"]))
        assert entry.status == "completed" and entry.usage_estimated
        assert entry.accounted_tokens == entry.reserved_tokens


@pytest.mark.parametrize(
    "model_key,adaptive,images",
    [
        ("gpt_oss_safeguard_20b", False, False),
        ("gpt_oss_safeguard_120b", True, False),
        ("gpt_oss_safeguard_20b", True, True),
    ],
)
def test_internal_safety_invocation_works_without_user_key_quota(
    mantle_gateway, model_key, adaptive, images
):
    _, factory, wire = mantle_gateway
    messages = (
        image_messages()
        if images
        else [
            {"role": "system", "content": "Classify the request"},
            {"role": "user", "content": "Hello"},
        ]
    )
    result = complete_safety(messages, model_key=model_key, adaptive=adaptive, factory=factory)
    payload = wire.requests[0]["payload"]
    assert payload["max_tokens"] == (2048 if adaptive else 1024)
    assert payload["model"] == (
        "qwen.qwen3-vl-235b-a22b-instruct"
        if images
        else "openai.gpt-oss-safeguard-120b"
        if adaptive
        else "openai.gpt-oss-safeguard-20b"
    )
    with factory() as db:
        row = db.get(Usage, result["id"])
        assert row.purpose == "safety" and row.key_id is None and row.user_id is None
        assert row.status == "completed" and row.input_tokens == 4 and row.accounted_tokens == 0


def test_cli_creates_user_and_one_time_key_for_http_requests(mantle_gateway):
    from app.manage import execute, parser

    client, factory, _ = mantle_gateway
    user = execute(parser().parse_args(["create-user", "--name", "first-owner"]))
    assert (
        execute(parser().parse_args(["create-user", "--name", "first-owner"]))["id"] == user["id"]
    )
    value = execute(parser().parse_args(["create-key", "--user", "first-owner", "--name", "bot"]))
    headers = {"Authorization": "Bearer " + value["key"]}
    assert len(client.get("/v1/models", headers=headers).json()["data"]) == 40
    assert chat(client, headers).status_code == 200
    assert chat(client, headers, "moonshotai.kimi-k2.5").status_code == 200
    listed = execute(parser().parse_args(["list-keys", "--user", "first-owner"]))
    assert value["key"] not in str(listed) and "key_hash" not in str(listed)
    execute(parser().parse_args(["revoke-key", "--id", value["id"]]))
    assert chat(client, headers).status_code == 401
    with factory() as db:
        assert db.get(User, user["id"]).active


def test_admin_route_edits_are_runtime_authority_and_identity_is_stable(mantle_gateway):
    client, factory, wire = mantle_gateway
    _, headers = issue(client)
    body = {"alias": "xai.grok-4.3", "upstream_model": "xai.grok-4.3", "region": "us-west-2"}
    assert client.put("/admin/api/models", json=body).status_code == 200
    assert seed_models(factory)["inserted"] == 0
    assert chat(client, headers).status_code == 200
    assert wire.requests[0]["url"].endswith("us-west-2.api.aws/openai/v1/chat/completions")
    for change in ({"model_key": "another_identity"}, {"upstream_model": "other.model"}):
        result = client.put("/admin/api/models", json={**body, **change})
        assert result.status_code == 400
    with factory() as db:
        row = resolve_model(db, "grok_4_3")
        assert row.upstream_model == "xai.grok-4.3" and row.region == "us-west-2"
