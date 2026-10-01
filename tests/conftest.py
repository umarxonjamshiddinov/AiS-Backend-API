"""Integration tests run only in disposable schemas in the dedicated ais_test DB."""

from uuid import uuid4

import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.admin.routes import require_admin
from app.db.models import Base
from app.gateway.capabilities import legacy_capabilities
from app.gateway.providers import registry
from app.main import create_app
from app.providers.base import ChatEvent
from app.providers.bedrock_chat import BedrockAdapter


class FakeChat(BedrockAdapter):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.closed = False

    def configured(self):
        return True

    def events(self, call):
        self.calls.append(call)
        try:
            yield ChatEvent(delta={"role": "assistant"})
            yield ChatEvent(delta={"content": "OK"})
            yield ChatEvent(finish_reason="stop")
            yield ChatEvent(usage={"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6})
        finally:
            self.closed = True


@pytest.fixture
def isolated_database():
    value = dotenv_values(".env").get("TEST_DATABASE_URL")
    if not value:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    url = make_url(value)
    if url.database != "ais_test" or url.host != "127.0.0.1" or url.port != 55432:
        pytest.fail("Tests require ais_test on 127.0.0.1:55432")
    schema = "test_" + uuid4().hex
    engine = create_engine(value, hide_parameters=True, connect_args={"connect_timeout": 5})
    with engine.begin() as connection:
        connection.execute(text(f"CREATE SCHEMA {schema}"))
    test_engine = create_engine(
        value,
        hide_parameters=True,
        connect_args={"options": f"-csearch_path={schema}", "connect_timeout": 5},
    )
    try:
        yield test_engine
    finally:
        test_engine.dispose()
        with engine.begin() as connection:
            connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        engine.dispose()


@pytest.fixture
def gateway(isolated_database, monkeypatch):
    Base.metadata.create_all(isolated_database)
    factory = sessionmaker(isolated_database, expire_on_commit=False)
    monkeypatch.setattr("app.gateway.quota.session_factory", lambda: factory)
    monkeypatch.setattr("app.admin.routes.session_factory", lambda: factory)
    monkeypatch.setitem(registry.providers, "bedrock", FakeChat())
    application = create_app(maintenance=False)
    application.dependency_overrides[require_admin] = lambda: None
    with TestClient(application) as client:
        assert (
            client.put(
                "/admin/api/models",
                json={
                    "alias": "ais-fast",
                    "provider": "bedrock",
                    "upstream_model": "private-model",
                    "input_price": "1",
                    "output_price": "2",
                    "max_output_tokens": 4096,
                    "capabilities": legacy_capabilities("bedrock").model_dump(),
                },
            ).status_code
            == 200
        )
        yield client, factory


def issue(client, **changes):
    payload = {
        "name": "test client",
        "models": ["ais-fast"],
        "budget_usd": "10",
        "rpm": 30,
        "tpm": 100000,
    }
    payload.update(changes)
    response = client.post("/admin/api/keys", json=payload)
    assert response.status_code == 200
    result = response.json()
    return result["id"], result["key"], {"Authorization": "Bearer " + result["key"]}


def completion(client, headers, **changes):
    body = {"model": "ais-fast", "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 16}
    body.update(changes)
    return client.post("/v1/chat/completions", json=body, headers=headers)
