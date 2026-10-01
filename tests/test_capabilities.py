import pytest
from conftest import completion, issue
from sqlalchemy import func, select

from app.db.models import ApiKey, Usage
from app.gateway.capabilities import ModelCapabilities, legacy_capabilities
from app.gateway.providers import registry

TOOLS = [{"type": "function", "function": {"name": "weather", "parameters": {"type": "object"}}}]
SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "answer",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
    },
}


def model(client, alias="ais-fast", **changes):
    payload = {"alias": alias, "provider": "bedrock", "upstream_model": "private-model"}
    payload.update(changes)
    return client.put("/admin/api/models", json=payload)


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"expires_at": "2020-01-01T00:00:00Z"}, "expired_api_key"),
        ({"active": False}, "disabled_api_key"),
    ],
)
def test_distinct_key_states(gateway, changes, code):
    client, _ = gateway
    _, _, headers = issue(client, **changes)
    response = completion(client, headers)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["type"] == "authentication_error"
    assert response.headers["www-authenticate"] == "Bearer"


def test_revoke_cannot_be_undone_or_change_provider_credentials(gateway):
    client, factory = gateway
    key_id, _, headers = issue(client)
    providers = client.get("/admin/api/overview").json()["providers"]
    client.post(f"/admin/api/keys/{key_id}/revoke")
    assert completion(client, headers).json()["error"]["code"] == "revoked_api_key"
    response = client.put(f"/admin/api/keys/{key_id}", json={"name": "test", "active": True})
    assert response.status_code == 409
    assert client.get("/admin/api/overview").json()["providers"] == providers
    with factory() as db:
        assert db.get(ApiKey, key_id).revoked_at is not None


def test_model_visibility_and_errors(gateway):
    client, factory = gateway
    assert model(client, "private", external_api_allowed=False).status_code == 200
    assert model(client, "disabled", enabled=False).status_code == 200
    assert model(client, "forbidden").status_code == 200
    _, _, headers = issue(client, models=["ais-fast", "private", "disabled"])
    response = client.get("/v1/models", headers=headers)
    assert response.json()["data"] == [
        {"id": "ais-fast", "object": "model", "created": 0, "owned_by": "myais"}
    ]
    assert "private-model" not in response.text
    assert "bedrock" not in response.text
    for alias in ("private", "disabled", "missing"):
        response = completion(client, headers, model=alias)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "model_not_found"
    response = completion(client, headers, model="forbidden")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "model_not_allowed"
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Usage)) == 0


@pytest.mark.parametrize(
    "payload,param",
    [
        ({"stream": True}, "stream"),
        ({"tools": TOOLS}, "tools"),
        (
            {
                "messages": [
                    {"role": "developer", "content": "Be concise"},
                    {"role": "user", "content": "Hi"},
                ]
            },
            "messages",
        ),
        ({"response_format": {"type": "json_object"}}, "response_format"),
        ({"response_format": SCHEMA}, "response_format"),
        (
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": "https://example.invalid/image.png"},
                            }
                        ],
                    }
                ]
            },
            "messages",
        ),
        ({"reasoning_effort": "high"}, "reasoning_effort"),
    ],
)
def test_unsupported_features_fail_before_reservation(gateway, payload, param):
    client, factory = gateway
    assert model(client, capabilities=ModelCapabilities().model_dump()).status_code == 200
    _, _, headers = issue(client)
    response = completion(client, headers, **payload)
    assert response.status_code == 400
    error = response.json()["error"]
    assert set(error) == {"message", "type", "param", "code"}
    assert error["code"] == "unsupported_capability"
    assert error["param"] == param
    assert registry.get("bedrock").calls == []
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Usage)) == 0


@pytest.mark.parametrize(
    "feature",
    [
        {"parallel_tool_calls": False, "tools": TOOLS},
        {"tool_choice": "required", "tools": TOOLS},
        {"tools": [{"type": "function", "function": {"name": "f", "strict": True}}]},
    ],
)
def test_tool_subcapabilities(gateway, feature):
    client, _ = gateway
    _, _, headers = issue(client)
    assert (
        completion(client, headers, **feature).json()["error"]["code"] == "unsupported_capability"
    )
    assert not registry.get("bedrock").calls


@pytest.mark.parametrize(
    "changes",
    [
        {"json_mode": True},
        {"reasoning": True, "reasoning_efforts": ["high"]},
        {"tools": True, "parallel_tool_calls": True},
    ],
)
def test_cannot_advertise_capability_missing_in_adapter(gateway, changes):
    client, _ = gateway
    caps = ModelCapabilities(**changes).model_dump()
    response = model(client, capabilities=caps)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_capability"


def test_new_unknown_model_is_conservative_and_preserves_updates(gateway):
    client, _ = gateway
    assert model(client, "configured-id", upstream_model="admin-confirmed-id").status_code == 200
    value = next(
        m
        for m in client.get("/admin/api/overview").json()["models"]
        if m["alias"] == "configured-id"
    )
    assert value["capabilities"] == ModelCapabilities().model_dump()
    assert (
        model(
            client,
            capabilities=legacy_capabilities("bedrock").model_dump(),
            external_api_allowed=False,
        ).status_code
        == 200
    )
    assert model(client, input_price="0").status_code == 200
    value = next(
        m for m in client.get("/admin/api/overview").json()["models"] if m["alias"] == "ais-fast"
    )
    assert value["capabilities"]["tools"]
    assert not value["external_api_allowed"]


def test_nonstream_does_not_require_streaming_capability(gateway):
    client, _ = gateway
    model(client, capabilities=ModelCapabilities().model_dump(), max_output_tokens=8)
    _, _, headers = issue(client)
    response = completion(client, headers, max_tokens=None)
    assert response.status_code == 200
    assert registry.get("bedrock").calls[0].request.output_limit() == 8
    assert not registry.get("bedrock").calls[0].request.stream


@pytest.mark.parametrize(
    "fields",
    [
        {"reasoning": {"effort": "high"}},
        {"extra_body": {"secret_parameter": "secret-value"}},
        {"max_completion_tokens": 32},
        {"stream_options": {"include_usage": True}},
        {"tools": TOOLS, "tool_choice": {"type": "function", "function": {"name": "missing"}}},
        {"messages": [{"role": "tool", "tool_call_id": "missing", "content": "x"}]},
    ],
)
def test_invalid_fields_never_reach_provider(gateway, fields):
    client, factory = gateway
    _, _, headers = issue(client)
    response = completion(client, headers, **fields)
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert "secret-value" not in response.text
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Usage)) == 0


def test_public_http_errors_and_size_limit(gateway):
    client, _ = gateway
    _, _, headers = issue(client)
    for method, path, code in (("GET", "/v1/missing", 404), ("POST", "/v1/models", 405)):
        response = client.request(method, path, headers=headers)
        assert response.status_code == code
        assert set(response.json()["error"]) == {"message", "type", "param", "code"}
    response = client.post(
        "/v1/chat/completions", content=b"{}", headers={**headers, "content-length": "9000000"}
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"
