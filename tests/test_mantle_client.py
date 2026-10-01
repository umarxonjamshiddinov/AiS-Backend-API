import json
import logging
from dataclasses import replace

import httpx2
import pytest
from openai import OpenAI

from app.core.config import Settings
from app.gateway.capabilities import ModelCapabilities
from app.gateway.schemas import ChatRequest
from app.providers.base import ChatInput, ProviderError
from app.providers.mantle import MantleAdapter, MantleClient, base_url_for

UPSTREAM_SECRET = "server-only-unit-secret-DO-NOT-EXPOSE"
BASE = "https://bedrock-mantle.us-east-1.api.aws/v1"
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aXioAAAAASUVORK5CYII="


def settings(**changes):
    values = dict(
        openai_api_key=UPSTREAM_SECRET,
        aws_bearer_token_bedrock=None,
        openai_base_url=BASE,
        ai_retry_backoff_seconds=0,
    )
    values.update(changes)
    return Settings(_env_file=None, **values)


def response_body(model="xai.grok-4.3", content="OK", **changes):
    body = {
        "id": "chatcmpl-upstream",
        "object": "chat.completion",
        "created": 1,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
    }
    body.update(changes)
    return body


def make_client(handler, **changes):
    options_seen = []

    def factory(**options):
        options_seen.append(options.copy())
        return OpenAI(
            **options,
            http_client=httpx2.Client(
                transport=httpx2.MockTransport(handler),
                follow_redirects=False,
                trust_env=False,
            ),
        )

    return MantleClient(client_factory=factory, settings=settings(**changes)), options_seen


def call(**changes):
    request = ChatRequest(
        model="xai.grok-4.3", messages=[{"role": "user", "content": "Hi"}], max_tokens=16
    )
    value = ChatInput(
        request,
        request.model,
        ModelCapabilities(system_prompt=True, temperature=False),
        "mantle_chat",
        region="us-east-1",
        api_route="mantle_openai",
    )
    return replace(value, **changes)


@pytest.mark.parametrize(
    "region,route,expected",
    [
        ("us-east-1", "mantle_openai", "https://bedrock-mantle.us-east-1.api.aws/openai/v1"),
        ("us-west-2", "mantle_openai", "https://bedrock-mantle.us-west-2.api.aws/openai/v1"),
        ("us-east-1", "mantle_default", "https://bedrock-mantle.us-east-1.api.aws/v1"),
    ],
)
def test_exact_mantle_endpoints(region, route, expected):
    assert base_url_for(region, route, BASE) == expected


@pytest.mark.parametrize(
    "base",
    [
        "https://api.myais.uz/v1",
        "http://bedrock-mantle.us-east-1.api.aws/v1",
        "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1",
        "https://bedrock-mantle.us-east-1.api.aws.evil.test/v1",
        "https://user:secret@bedrock-mantle.us-east-1.api.aws/v1",
        "https://bedrock-mantle.us-east-1.api.aws/v1?secret=private",
        "https://bedrock-mantle.us-east-1.api.aws:8443/v1",
        "https://bedrock-mantle.us-east-1.api.aws/unexpected",
    ],
)
def test_credentials_cannot_be_sent_to_non_mantle_origins(base):
    with pytest.raises(ProviderError, match="provider_unavailable"):
        base_url_for("us-east-1", "mantle_openai", base)


def test_model_region_overrides_base_and_aws_region():
    assert base_url_for("us-west-2", "mantle_openai", BASE).endswith("us-west-2.api.aws/openai/v1")
    with pytest.raises(ProviderError):
        base_url_for("us-east-1.evil.test", "mantle_default", BASE)
    with pytest.raises(ProviderError):
        base_url_for("us-east-1", "unknown_route", BASE)


def test_real_sdk_request_uses_server_key_exact_payload_and_custom_limits():
    seen = []

    def send(request):
        seen.append(request)
        return httpx2.Response(200, json=response_body())

    client, options = make_client(send)
    result = client.complete(call())
    assert result.text == "OK" and result.usage["total_tokens"] == 6
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == BASE.replace("/v1", "/openai/v1") + "/chat/completions"
    assert request.headers["authorization"] == "Bearer " + UPSTREAM_SECRET
    assert json.loads(request.content) == {
        "model": "xai.grok-4.3",
        "messages": [{"role": "user", "content": "Hi"}],
        "max_tokens": 16,
    }
    assert options[0]["max_retries"] == 0 and options[0]["timeout"] == 120


@pytest.mark.parametrize(
    "status,code,attempts",
    [
        (400, "upstream_error", 1),
        (401, "upstream_error", 1),
        (403, "upstream_error", 1),
        (404, "upstream_error", 1),
        (408, "upstream_timeout", 1),
        (409, "upstream_error", 1),
        (429, "upstream_rate_limit", 3),
        (500, "upstream_error", 3),
        (502, "upstream_error", 3),
        (503, "upstream_error", 3),
        (504, "upstream_timeout", 3),
    ],
)
def test_only_exported_http_errors_retry_and_never_leak(status, code, attempts, caplog):
    seen = []

    def send(request):
        seen.append(request)
        return httpx2.Response(status, json={"error": {"message": UPSTREAM_SECRET}})

    client, _ = make_client(send)
    current = call()
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ProviderError) as error:
            client.complete(current)
    assert error.value.code == code
    assert len(seen) == attempts == current.metrics["attempts"]
    assert UPSTREAM_SECRET not in json.dumps(error.value.as_dict()) + caplog.text
    assert UPSTREAM_SECRET not in json.dumps(current.metrics)


@pytest.mark.parametrize(
    "exception,code,status",
    [
        (httpx2.ReadTimeout, "upstream_timeout", 504),
        (httpx2.ConnectError, "upstream_error", 502),
    ],
)
def test_connection_and_timeout_retries(exception, code, status):
    seen = []

    def send(request):
        seen.append(request)
        raise exception(UPSTREAM_SECRET, request=request)

    client, _ = make_client(send)
    with pytest.raises(ProviderError) as error:
        client.complete(call())
    assert error.value.code == code and error.value.status_code == status
    assert len(seen) == 3


def test_retry_can_recover_once_and_not_duplicate_sdk_retries():
    seen = []

    def send(request):
        seen.append(request)
        return httpx2.Response(500 if len(seen) == 1 else 200, json=response_body())

    client, _ = make_client(send)
    assert client.complete(call()).text == "OK"
    assert len(seen) == 2


@pytest.mark.parametrize(
    "body",
    [
        response_body(content=""),
        response_body(content="   "),
        response_body(choices=[]),
        response_body(usage={"prompt_tokens": -1, "completion_tokens": 2, "total_tokens": 1}),
        response_body(usage={"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 9}),
    ],
)
def test_empty_invalid_responses_are_safe_errors_without_retries(body):
    seen = []

    def send(request):
        seen.append(request)
        return httpx2.Response(200, json=body)

    client, _ = make_client(send)
    with pytest.raises(ProviderError, match="upstream_error"):
        client.complete(call())
    assert len(seen) == 1


def test_missing_usage_is_explicitly_unknown_not_fabricated():
    client, _ = make_client(lambda request: httpx2.Response(200, json=response_body(usage=None)))
    assert client.complete(call()).usage is None


@pytest.mark.parametrize("refusal", [False, True])
def test_provider_moderation_checked_before_text_return(refusal):
    body = response_body(content="Do not return provider-filtered content")
    if refusal:
        body["choices"][0]["message"]["refusal"] = "Filtered"
    else:
        body["choices"][0]["finish_reason"] = "content_filter"
    client, _ = make_client(lambda request: httpx2.Response(200, json=body))
    with pytest.raises(ProviderError, match="content_filter"):
        client.complete(call())


def test_redirects_are_not_followed_with_server_credentials():
    seen = []

    def send(request):
        seen.append(request)
        return httpx2.Response(307, headers={"Location": "https://attacker.invalid/steal"})

    client, _ = make_client(send)
    with pytest.raises(ProviderError, match="upstream_error"):
        client.complete(call())
    assert len(seen) == 1 and seen[0].url.host.endswith(".api.aws")


def test_native_image_payload_is_preserved_and_secret_reflection_redacted():
    seen = []

    def send(request):
        seen.append(json.loads(request.content))
        return httpx2.Response(200, json=response_body(content="Value: " + UPSTREAM_SECRET))

    client, _ = make_client(send)
    request = ChatRequest(
        model="xai.grok-4.3",
        max_tokens=16,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Look"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + PNG}},
                ],
            }
        ],
    )
    current = call(request=request)
    adapter = MantleAdapter(client)
    adapter.validate(current)
    events = list(adapter.events(current))
    assert seen[0]["messages"][0]["content"] == [
        {"type": "text", "text": "Look"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + PNG}},
    ]
    assert events[0].delta["content"] == "Value: [REDACTED]"


def test_legacy_server_credential_is_fallback_and_new_variable_takes_priority():
    assert (
        settings(aws_bearer_token_bedrock="old").mantle_api_key.get_secret_value()
        == UPSTREAM_SECRET
    )
    assert (
        settings(
            openai_api_key=None, aws_bearer_token_bedrock="old"
        ).mantle_api_key.get_secret_value()
        == "old"
    )
    assert "secret" not in repr(settings())
