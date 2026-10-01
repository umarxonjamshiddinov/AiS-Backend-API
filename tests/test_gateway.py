from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from conftest import FakeChat, completion, issue
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import ApiKey, Usage
from app.gateway.providers import bedrock_payload, registry
from app.gateway.quota import authenticate, reserve
from app.gateway.schemas import ChatRequest
from app.main import create_app
from app.providers.base import ProviderError


def test_admin_requires_auth():
    with TestClient(create_app(maintenance=False)) as client:
        assert client.get("/admin/api/overview").status_code in (401, 503)
        assert client.post("/internal/test/bedrock", json={"prompt": "x"}).status_code in (401, 503)


def test_key_is_hashed_and_model_details_hidden(gateway):
    client, factory = gateway
    key_id, token, headers = issue(client)
    with factory() as db:
        key = db.get(ApiKey, key_id)
        assert key.key_hash != token
        assert token not in repr(key.__dict__)
    overview = client.get("/admin/api/overview").json()
    assert token not in str(overview)
    response = client.get("/v1/models", headers=headers)
    assert response.json()["data"][0]["id"] == "ais-fast"
    assert "private-model" not in response.text


def test_chat_usage_is_settled(gateway):
    client, factory = gateway
    key_id, _, headers = issue(client)
    response = completion(client, headers)
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "OK"
    assert response.json()["model"] == "ais-fast"
    assert response.json()["usage"]["total_tokens"] == 6
    with factory() as db:
        key = db.get(ApiKey, key_id)
        assert key.reserved_usd == 0
        assert key.spent_usd == Decimal("0.00000800")
        usage = db.scalar(select(Usage))
        assert usage.status == "completed"


def test_streaming(gateway):
    client, _ = gateway
    _, _, headers = issue(client)
    response = completion(client, headers, stream=True, stream_options={"include_usage": True})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "chat.completion.chunk" in response.text
    assert '"prompt_tokens": 4' in response.text
    assert "data: [DONE]" in response.text
    assert "private-model" not in response.text


def test_invalid_auth_and_revocation(gateway):
    client, _ = gateway
    key_id, _, headers = issue(client)
    assert completion(client, {}).status_code == 401
    assert completion(client, {"Authorization": "Bearer sk-myais-invalid"}).status_code == 401
    assert client.post(f"/admin/api/keys/{key_id}/revoke").status_code == 200
    assert completion(client, headers).status_code == 401


def test_permission_denied(gateway):
    client, _ = gateway
    _, _, headers = issue(client, models=[])
    response = completion(client, headers, model="ais-fast")
    assert response.status_code == 403


@pytest.mark.parametrize(
    "limits,code",
    [
        ({"budget_usd": "0.00000001"}, "budget_exceeded"),
        ({"tpm": 1}, "token_limit_exceeded"),
    ],
)
def test_rejection_before_upstream(gateway, limits, code):
    client, factory = gateway
    _, _, headers = issue(client, **limits)
    response = completion(client, headers)
    assert response.status_code == 429
    assert response.json()["error"]["code"] == code
    with factory() as db:
        assert db.scalar(select(Usage)) is None


def test_rpm_is_shared_and_atomic(gateway):
    client, _ = gateway
    _, token, _ = issue(client, rpm=1)
    key_id = authenticate(token)
    request = ChatRequest(model="ais-fast", messages=[{"role": "user", "content": "Hi"}])

    def call(_):
        try:
            reserve(key_id, request)
            return "ok"
        except ProviderError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(call, range(5)))
    assert results.count("ok") == 1
    assert results.count("rate_limit_exceeded") == 4


def test_expired_key(gateway):
    client, _ = gateway
    _, _, headers = issue(client, expires_at="2020-01-01T00:00:00Z")
    assert completion(client, headers).status_code == 401


def test_failure_releases_reservation_and_records_estimate(gateway, monkeypatch):
    client, factory = gateway
    key_id, _, headers = issue(client)

    class FailedChat(FakeChat):
        def events(self, call):
            raise ProviderError("provider_error", 502)
            yield

    monkeypatch.setitem(registry.providers, "bedrock", FailedChat())
    assert completion(client, headers).status_code == 502
    with factory() as db:
        assert db.get(ApiKey, key_id).reserved_usd == 0
        assert db.get(ApiKey, key_id).spent_usd > 0
        usage = db.scalar(select(Usage))
        assert usage.status == "failed"
        assert usage.usage_estimated
        usage_id = usage.id
    assert client.post(f"/admin/api/usage/{usage_id}/release").status_code == 200
    assert client.post(f"/admin/api/usage/{usage_id}/release").status_code == 409
    with factory() as db:
        assert db.get(ApiKey, key_id).reserved_usd == 0


def test_validation_never_echoes_secret(gateway):
    client, _ = gateway
    response = client.put(
        "/admin/api/credentials", json={"provider": "invalid", "api_key": "private-secret"}
    )
    assert response.status_code == 422
    assert "private-secret" not in response.text


def test_bedrock_tool_conversion():
    request = ChatRequest(
        model="x",
        messages=[
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "Weather?"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call1",
                        "type": "function",
                        "function": {"name": "weather", "arguments": '{"city":"Tashkent"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call1", "content": "Sunny"},
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "weather",
                    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
                },
            }
        ],
    )
    payload = bedrock_payload(request)
    assert payload["system"][0]["text"] == "You are helpful"
    assert payload["messages"][1]["content"][0]["toolUse"]["input"] == {"city": "Tashkent"}
    assert payload["messages"][2]["content"][0]["toolResult"]["toolUseId"] == "call1"
    assert payload["toolConfig"]["tools"][0]["toolSpec"]["name"] == "weather"
