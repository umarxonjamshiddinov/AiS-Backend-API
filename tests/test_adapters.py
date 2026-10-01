import base64
import json
from unittest.mock import Mock

import botocore.session
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from botocore.validate import validate_parameters

from app.gateway.capabilities import legacy_capabilities
from app.gateway.schemas import ChatRequest
from app.providers.base import ChatInput, ProviderError
from app.providers.bedrock_chat import BedrockAdapter, bedrock_payload

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aXioAAAAASUVORK5CYII="
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "weather",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }
]


def call(**fields):
    values = {"model": "public-alias", "messages": [{"role": "user", "content": "Hi"}]}
    values.update(fields)
    return ChatInput(
        ChatRequest(**values),
        "confirmed-private-model",
        legacy_capabilities("bedrock"),
        "converse",
    )


class Events:
    def __init__(self, events):
        self.events, self.closed = events, False

    def __iter__(self):
        yield from self.events

    def close(self):
        self.closed = True


def test_bedrock_nonstream_uses_converse_and_normalizes_tools():
    sdk = Mock()
    sdk.converse.return_value = {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": "call-1",
                            "name": "weather",
                            "input": {"city": "Tashkent"},
                        }
                    }
                ],
            }
        },
        "stopReason": "tool_use",
        "usage": {"inputTokens": 4, "outputTokens": 6, "totalTokens": 10},
    }
    events = list(BedrockAdapter(client=sdk).events(call(tools=TOOLS)))
    sdk.converse_stream.assert_not_called()
    assert sdk.converse.call_args.kwargs["modelId"] == "confirmed-private-model"
    assert sdk.converse.call_args.kwargs["inferenceConfig"]["maxTokens"] == 1024
    assert events[1].delta["tool_calls"][0]["function"]["name"] == "weather"
    assert events[-2].finish_reason == "tool_calls"
    assert events[-1].usage["total_tokens"] == 10


def test_bedrock_stream_maps_text_tools_usage_and_finish():
    sdk = Mock()
    stream = Events(
        [
            {"messageStart": {"role": "assistant"}},
            {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "Let me check"}}},
            {
                "contentBlockStart": {
                    "contentBlockIndex": 2,
                    "start": {"toolUse": {"toolUseId": "call-1", "name": "weather"}},
                }
            },
            {
                "contentBlockDelta": {
                    "contentBlockIndex": 2,
                    "delta": {"toolUse": {"input": '{"city":'}},
                }
            },
            {
                "contentBlockDelta": {
                    "contentBlockIndex": 2,
                    "delta": {"toolUse": {"input": '"Tashkent"}'}},
                }
            },
            {"messageStop": {"stopReason": "tool_use"}},
            {"metadata": {"usage": {"inputTokens": 8, "outputTokens": 10, "totalTokens": 18}}},
        ]
    )
    sdk.converse_stream.return_value = {"stream": stream}
    events = list(BedrockAdapter(client=sdk).events(call(stream=True, tools=TOOLS)))
    sdk.converse.assert_not_called()
    assert events[1].delta == {"content": "Let me check"}
    assert events[2].delta["tool_calls"][0]["index"] == 0
    arguments = "".join(
        e.delta.get("tool_calls", [{}])[0].get("function", {}).get("arguments", "") for e in events
    )
    assert json.loads(arguments) == {"city": "Tashkent"}
    assert events[-2].finish_reason == "tool_calls"
    assert events[-1].usage["total_tokens"] == 18
    assert stream.closed


def test_bedrock_stream_exception_is_safe_and_closes():
    sdk = Mock()
    stream = Events(
        [
            {"messageStart": {"role": "assistant"}},
            {"internalServerException": {"message": "private-secret-value"}},
        ]
    )
    sdk.converse_stream.return_value = {"stream": stream}
    with pytest.raises(ProviderError) as error:
        list(BedrockAdapter(client=sdk).events(call(stream=True)))
    assert error.value.code == "provider_error"
    assert "private-secret-value" not in str(error.value.as_dict())
    assert stream.closed


@pytest.mark.parametrize(
    "code,status,unknown",
    [
        ("ThrottlingException", 503, False),
        ("ServiceUnavailableException", 503, False),
        ("AccessDeniedException", 502, False),
        ("ValidationException", 502, False),
        ("ModelTimeoutException", 502, True),
    ],
)
def test_bedrock_sdk_errors_are_normalized(code, status, unknown):
    sdk = Mock()
    sdk.converse.side_effect = ClientError(
        {"Error": {"Code": code, "Message": "private-secret"}}, "Converse"
    )
    with pytest.raises(ProviderError) as error:
        list(BedrockAdapter(client=sdk).events(call()))
    assert error.value.status_code == status
    assert error.value.usage_unknown is unknown
    assert "private-secret" not in json.dumps(error.value.as_dict())


def test_bedrock_network_failure():
    sdk = Mock()
    sdk.converse.side_effect = EndpointConnectionError(endpoint_url="https://example.invalid")
    with pytest.raises(ProviderError, match="provider_unavailable"):
        list(BedrockAdapter(client=sdk).events(call()))


def test_bedrock_developer_text_parts_and_multiple_tool_results():
    request = ChatRequest(
        model="alias",
        messages=[
            {"role": "system", "content": "System"},
            {"role": "developer", "content": "Developer"},
            {"role": "user", "content": [{"type": "text", "text": "Weather?"}]},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "a",
                        "function": {"name": "weather", "arguments": '{"city":"Tashkent"}'},
                    },
                    {
                        "id": "b",
                        "function": {"name": "weather", "arguments": '{"city":"Samarkand"}'},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "a", "content": "Sunny"},
            {"role": "tool", "tool_call_id": "b", "content": "Cloudy"},
        ],
        tools=TOOLS,
    )
    payload = bedrock_payload(request)
    assert payload["system"] == [{"text": "System"}, {"text": "Developer"}]
    assert len(payload["messages"]) == 3
    assert len(payload["messages"][-1]["content"]) == 2
    assert payload["messages"][-1]["content"][1]["toolResult"]["toolUseId"] == "b"
    shape = (
        botocore.session.get_session()
        .get_service_model("bedrock-runtime")
        .operation_model("Converse")
        .input_shape
    )
    validate_parameters({"modelId": "confirmed-model", **payload}, shape)


def test_bedrock_native_json_schema_matches_installed_sdk():
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    request = call(
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "response", "strict": True, "schema": schema},
        }
    )
    payload = bedrock_payload(request.request)
    assert (
        json.loads(payload["outputConfig"]["textFormat"]["structure"]["jsonSchema"]["schema"])
        == schema
    )
    assert payload["messages"] == [{"role": "user", "content": [{"text": "Hi"}]}]
    shape = (
        botocore.session.get_session()
        .get_service_model("bedrock-runtime")
        .operation_model("Converse")
        .input_shape
    )
    validate_parameters({"modelId": request.model, **payload}, shape)


def test_vision_conversion_preserves_content_order_and_bytes():
    request = call(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Before"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + PNG}},
                    {"type": "text", "text": "After"},
                ],
            }
        ]
    )
    BedrockAdapter(client=Mock()).validate(request)
    blocks = bedrock_payload(request.request)["messages"][0]["content"]
    assert blocks[0] == {"text": "Before"} and blocks[2] == {"text": "After"}
    assert blocks[1]["image"] == {"format": "png", "source": {"bytes": base64.b64decode(PNG)}}


@pytest.mark.parametrize(
    "image",
    [
        {"url": "https://example.invalid/test.png"},
        {"url": "data:image/png;base64," + PNG, "detail": "low"},
    ],
)
def test_bedrock_does_not_silently_ignore_image_options(image):
    request = call(
        messages=[{"role": "user", "content": [{"type": "image_url", "image_url": image}]}]
    )
    with pytest.raises(ProviderError, match="unsupported_capability"):
        BedrockAdapter(client=Mock()).validate(request)


@pytest.mark.parametrize(
    "url",
    [
        "data:image/png;base64,AAAA",
        "data:image/svg+xml;base64,AAAA",
        "data:image/jpeg;base64," + PNG,
    ],
)
def test_bedrock_invalid_image_sources(url):
    request = call(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": url}},
                ],
            }
        ]
    )
    with pytest.raises(ProviderError, match="invalid_request"):
        BedrockAdapter(client=Mock()).validate(request)
