"""OpenAI chat semantics over the real Bedrock Converse APIs."""

import json
from contextlib import suppress

from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import get_settings
from app.gateway.capabilities import ModelCapabilities, unsupported
from app.gateway.schemas import ImagePart, NamedToolChoice, TextPart
from app.providers.base import ChatEvent, ChatInput, ProviderError
from app.providers.bedrock import BedrockProvider
from app.providers.images import decode_image


def finish_reason(value):
    reasons = {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "max_tokens": "length",
        "tool_use": "tool_calls",
        "guardrail_intervened": "content_filter",
        "content_filtered": "content_filter",
        "model_context_window_exceeded": "length",
    }
    if value not in reasons:
        raise ProviderError("provider_error", message="Provider returned an invalid finish reason.")
    return reasons[value]


def token_usage(usage):
    return {
        "prompt_tokens": usage["inputTokens"],
        "completion_tokens": usage["outputTokens"],
        "total_tokens": usage["totalTokens"],
    }


def bedrock_payload(request):
    messages, system = [], []
    for message in request.messages:
        if message.role in ("system", "developer"):
            system.append({"text": message.text()})
            continue
        blocks = []
        if message.role == "tool":
            result = {"text": message.text()} if message.text() else {"json": ""}
            blocks.append({"toolResult": {"toolUseId": message.tool_call_id, "content": [result]}})
        else:
            if isinstance(message.content, str) and message.content:
                blocks.append({"text": message.content})
            elif isinstance(message.content, list):
                for part in message.content:
                    if isinstance(part, TextPart) and part.text:
                        blocks.append({"text": part.text})
                    elif isinstance(part, ImagePart):
                        image_format, image = decode_image(part.image_url.url)
                        blocks.append(
                            {"image": {"format": image_format, "source": {"bytes": image}}}
                        )
            for call in message.tool_calls or []:
                blocks.append(
                    {
                        "toolUse": {
                            "toolUseId": call.id,
                            "name": call.function.name,
                            "input": json.loads(call.function.arguments),
                        }
                    }
                )
        role = "assistant" if message.role == "assistant" else "user"
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"].extend(blocks)
        else:
            messages.append({"role": role, "content": blocks})
    if not messages or messages[0]["role"] != "user":
        raise ProviderError(
            "invalid_request",
            400,
            param="messages",
            message="This model requires a user message before assistant messages.",
            usage_unknown=False,
        )
    payload = {"messages": messages, "inferenceConfig": {"maxTokens": request.output_limit()}}
    if request.temperature is not None:
        payload["inferenceConfig"]["temperature"] = request.temperature
    if system:
        payload["system"] = system
    if request.tools and request.tool_choice != "none":
        tools = []
        for tool in request.tools:
            fn = tool.function
            spec = {"name": fn.name, "inputSchema": {"json": fn.parameters}}
            if fn.description:
                spec["description"] = fn.description
            if fn.strict:
                spec["strict"] = fn.strict
            tools.append({"toolSpec": spec})
        config = {"tools": tools}
        choice = request.tool_choice
        if isinstance(choice, NamedToolChoice):
            config["toolChoice"] = {"tool": {"name": choice.function.name}}
        elif choice == "required":
            config["toolChoice"] = {"any": {}}
        elif choice == "auto":
            config["toolChoice"] = {"auto": {}}
        payload["toolConfig"] = config
    if request.response_format and request.response_format.type == "json_schema":
        schema = request.response_format.json_schema
        definition = {"name": schema.name, "schema": json.dumps(schema.schema_)}
        if schema.description:
            definition["description"] = schema.description
        payload["outputConfig"] = {
            "textFormat": {"type": "json_schema", "structure": {"jsonSchema": definition}}
        }
    return payload


class BedrockAdapter:
    transport = "converse"
    credential_variable = "AWS_BEARER_TOKEN_BEDROCK"
    # This is an implementation ceiling, NOT a claim about any particular Bedrock model.
    supported_capabilities = ModelCapabilities(
        streaming=True,
        system_prompt=True,
        tools=True,
        tool_choice=True,
        tool_choice_modes=["auto", "none", "required", "function"],
        strict_tools=True,
        structured_output=True,
        json_schema=True,
        vision=True,
        image_input=True,
    )

    def __init__(self, client=None):
        self.client = client

    def configured(self):
        return self.client is not None or bool(get_settings().aws_bearer_token_bedrock)

    def validate(self, call: ChatInput):
        request = call.request
        if request.temperature is not None and request.temperature > 1:
            raise ProviderError(
                "unsupported_capability",
                400,
                param="temperature",
                message="This transport accepts temperature between 0 and 1.",
                usage_unknown=False,
            )
        if any(m.name for m in request.messages):
            unsupported("messages", "named messages on this transport")
        for message in request.messages:
            for image in message.images():
                if image.image_url.detail != "auto":
                    unsupported("messages", "an explicit image detail setting on this transport")
                if not image.image_url.url.startswith("data:"):
                    unsupported("messages", "remote image URLs; use a base64 image data URL")
        if request.tool_choice == "none" and any(m.tool_calls for m in request.messages):
            unsupported("tool_choice", "disabling tools while replaying tool history")
        if any(m.tool_calls for m in request.messages) and not request.tools:
            raise ProviderError(
                "invalid_request",
                400,
                param="tools",
                message="Tool definitions are required when replaying tool history.",
                usage_unknown=False,
            )
        if request.response_format and request.response_format.type == "json_schema":
            if not request.response_format.json_schema.strict:
                unsupported("response_format", "non-strict schema mode on this transport")
        bedrock_payload(request)  # Validate/convert before a quota reservation or provider call.

    def events(self, call: ChatInput):
        stream, client = None, self.client
        try:
            payload = bedrock_payload(call.request)
            if client is None:
                client = BedrockProvider(get_settings())._get_client()
            if not call.request.stream:
                response = client.converse(modelId=call.model, **payload)
                yield ChatEvent(delta={"role": "assistant"})
                index = 0
                for block in response["output"]["message"]["content"]:
                    if "text" in block:
                        yield ChatEvent(delta={"content": block["text"]})
                    if "toolUse" in block:
                        tool = block["toolUse"]
                        yield ChatEvent(
                            delta={
                                "tool_calls": [
                                    {
                                        "index": index,
                                        "id": tool["toolUseId"],
                                        "type": "function",
                                        "function": {
                                            "name": tool["name"],
                                            "arguments": json.dumps(tool["input"]),
                                        },
                                    }
                                ]
                            }
                        )
                        index += 1
                yield ChatEvent(finish_reason=finish_reason(response["stopReason"]))
                yield ChatEvent(usage=token_usage(response["usage"]))
                return
            response = client.converse_stream(modelId=call.model, **payload)
            stream = response["stream"]
            tool_indexes = {}
            for event in stream:
                if any(k.endswith("Exception") for k in event):
                    unavailable = (
                        "throttlingException" in event or "serviceUnavailableException" in event
                    )
                    raise ProviderError(
                        "provider_unavailable" if unavailable else "provider_error",
                        503 if unavailable else 502,
                        message="The provider interrupted the stream.",
                    )
                if "messageStart" in event:
                    yield ChatEvent(delta={"role": "assistant"})
                if "contentBlockStart" in event:
                    start = event["contentBlockStart"]
                    tool = start.get("start", {}).get("toolUse")
                    if tool:
                        index = len(tool_indexes)
                        tool_indexes[start["contentBlockIndex"]] = index
                        yield ChatEvent(
                            delta={
                                "tool_calls": [
                                    {
                                        "index": index,
                                        "id": tool["toolUseId"],
                                        "type": "function",
                                        "function": {"name": tool["name"], "arguments": ""},
                                    }
                                ]
                            }
                        )
                if "contentBlockDelta" in event:
                    block = event["contentBlockDelta"]
                    delta = block["delta"]
                    if "text" in delta:
                        yield ChatEvent(delta={"content": delta["text"]})
                    if "toolUse" in delta:
                        index = tool_indexes[block["contentBlockIndex"]]
                        yield ChatEvent(
                            delta={
                                "tool_calls": [
                                    {
                                        "index": index,
                                        "function": {"arguments": delta["toolUse"]["input"]},
                                    }
                                ]
                            }
                        )
                if "messageStop" in event:
                    yield ChatEvent(finish_reason=finish_reason(event["messageStop"]["stopReason"]))
                if "metadata" in event:
                    yield ChatEvent(usage=token_usage(event["metadata"]["usage"]))
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            unavailable = code in {
                "ThrottlingException",
                "ServiceUnavailableException",
                "ModelNotReadyException",
            }
            unknown = code in {
                "ModelTimeoutException",
                "InternalServerException",
                "ModelErrorException",
            }
            raise ProviderError(
                "provider_unavailable" if unavailable else "provider_error",
                503 if unavailable else 502,
                usage_unknown=unknown,
                message="The provider could not complete this request.",
            ) from None
        except BotoCoreError:
            raise ProviderError(
                "provider_unavailable", 503, message="The provider connection failed."
            ) from None
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ProviderError(
                "provider_error", message="The provider returned an invalid response."
            ) from None
        finally:
            if stream is not None:
                with suppress(Exception):
                    stream.close()
            if self.client is None and client is not None:
                with suppress(Exception):
                    client.close()
