import asyncio
import json
import threading

import pytest
from conftest import FakeChat, completion, issue
from sqlalchemy import select

from app.db.models import ApiKey, Usage
from app.gateway.providers import registry
from app.providers.base import ChatEvent, ProviderError


def test_midstream_error_is_openai_style_and_ends_stream(gateway, monkeypatch):
    client, factory = gateway

    class Broken(FakeChat):
        def events(self, call):
            try:
                yield ChatEvent(delta={"role": "assistant"})
                yield ChatEvent(delta={"content": "partial"})
                raise RuntimeError("credential-that-must-not-leak")
            finally:
                self.closed = True

    adapter = Broken()
    monkeypatch.setitem(registry.providers, "bedrock", adapter)
    key_id, _, headers = issue(client, max_concurrent_requests=1)
    response = completion(client, headers, stream=True)
    assert response.status_code == 200
    assert "partial" in response.text
    assert "credential-that-must-not-leak" not in response.text
    assert response.text.endswith("data: [DONE]\n\n")
    error = next(
        json.loads(line[6:])["error"]
        for line in response.text.splitlines()
        if line.startswith("data: {") and '"error"' in line
    )
    assert set(error) == {"message", "type", "param", "code"}
    assert error["code"] == "provider_error"
    assert adapter.closed
    with factory() as db:
        assert db.get(ApiKey, key_id).reserved_usd == 0
        assert db.scalar(select(Usage)).status == "failed"


@pytest.mark.parametrize(
    "events",
    [
        [],
        [ChatEvent(delta={"content": "partial"})],
        [
            ChatEvent(finish_reason="stop"),
            ChatEvent(usage={"prompt_tokens": -1, "completion_tokens": 2, "total_tokens": 1}),
        ],
        [
            ChatEvent(finish_reason="stop"),
            ChatEvent(usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 8}),
        ],
        [ChatEvent(finish_reason="invented")],
    ],
)
def test_incomplete_or_invalid_upstream_never_counts_as_success(gateway, monkeypatch, events):
    client, factory = gateway

    class Invalid(FakeChat):
        def events(self, call):
            yield from events

    monkeypatch.setitem(registry.providers, "bedrock", Invalid())
    key_id, _, headers = issue(client)
    response = completion(client, headers)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "provider_error"
    with factory() as db:
        assert db.get(ApiKey, key_id).reserved_usd == 0
        assert db.scalar(select(Usage)).status == "failed"


def test_streamed_tools_and_tool_result_roundtrip(gateway, monkeypatch):
    client, _ = gateway

    class ToolChat(FakeChat):
        def events(self, call):
            self.calls.append(call)
            yield ChatEvent(delta={"role": "assistant"})
            if call.request.messages[-1].role == "tool":
                assert call.request.messages[-1].tool_call_id == "call-1"
                yield ChatEvent(delta={"content": "Sunny"})
                yield ChatEvent(finish_reason="stop")
            else:
                yield ChatEvent(
                    delta={
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "weather", "arguments": ""},
                            }
                        ]
                    }
                )
                yield ChatEvent(
                    delta={"tool_calls": [{"index": 0, "function": {"arguments": '{"city":'}}]}
                )
                yield ChatEvent(
                    delta={"tool_calls": [{"index": 0, "function": {"arguments": '"Tashkent"}'}}]}
                )
                yield ChatEvent(finish_reason="tool_calls")
            yield ChatEvent(
                usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}
            )

    monkeypatch.setitem(registry.providers, "bedrock", ToolChat())
    _, _, headers = issue(client)
    tools = [
        {"type": "function", "function": {"name": "weather", "parameters": {"type": "object"}}}
    ]
    response = completion(client, headers, tools=tools)
    message = response.json()["choices"][0]["message"]
    assert json.loads(message["tool_calls"][0]["function"]["arguments"]) == {"city": "Tashkent"}
    streamed = completion(client, headers, tools=tools, stream=True)
    assert '"finish_reason": "tool_calls"' in streamed.text
    assert '"index": 0' in streamed.text
    result = completion(
        client,
        headers,
        tools=tools,
        messages=[
            {"role": "user", "content": "Weather?"},
            message,
            {"role": "tool", "tool_call_id": "call-1", "content": "Sunny"},
        ],
    )
    assert result.json()["choices"][0]["message"]["content"] == "Sunny"


async def drive_asgi(app, headers, *, on_body, disconnect):
    payload = json.dumps(
        {
            "model": "ais-fast",
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 16,
            "stream": True,
        }
    ).encode()
    delivered = False
    messages = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "root_path": "",
        "server": ("test", 80),
        "client": ("test", 123),
        "headers": [
            (b"content-type", b"application/json"),
            (b"authorization", headers["Authorization"].encode()),
        ],
    }

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)
        if message["type"] == "http.response.body":
            await on_body(message.get("body", b""))

    await asyncio.wait_for(app(scope, receive, send), timeout=5)
    return messages


def test_sse_delivers_delta_before_upstream_finishes(gateway, monkeypatch):
    client, _ = gateway
    gate, finished = threading.Event(), threading.Event()

    class Gated(FakeChat):
        def events(self, call):
            try:
                yield ChatEvent(delta={"role": "assistant"})
                yield ChatEvent(delta={"content": "first-byte"})
                if not gate.wait(2):
                    raise ProviderError("provider_error")
                finished.set()
                yield ChatEvent(finish_reason="stop")
                yield ChatEvent(
                    usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
                )
            finally:
                self.closed = True

    adapter = Gated()
    monkeypatch.setitem(registry.providers, "bedrock", adapter)
    _, _, headers = issue(client)

    async def scenario():
        async def received(body):
            if b"first-byte" in body:
                assert not finished.is_set()
                gate.set()

        return await drive_asgi(client.app, headers, on_body=received, disconnect=asyncio.Event())

    messages = asyncio.run(scenario())
    assert gate.is_set() and adapter.closed
    assert b"[DONE]" in b"".join(m.get("body", b"") for m in messages)


def test_disconnect_releases_reservation_without_generator_gc(gateway, monkeypatch):
    client, factory = gateway
    adapter = FakeChat()
    monkeypatch.setitem(registry.providers, "bedrock", adapter)
    key_id, _, headers = issue(client, max_concurrent_requests=1)

    async def scenario():
        disconnected = asyncio.Event()

        async def received(body):
            if b"data:" in body:
                disconnected.set()
                await asyncio.sleep(0)

        return await drive_asgi(client.app, headers, on_body=received, disconnect=disconnected)

    asyncio.run(scenario())
    assert adapter.closed
    with factory() as db:
        assert db.get(ApiKey, key_id).reserved_usd == 0
        assert db.scalar(select(Usage)).status in ("cancelled", "completed")
        assert db.scalar(select(Usage)).lease_expires_at is None
    assert completion(client, headers).status_code == 200
