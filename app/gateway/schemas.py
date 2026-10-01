import json
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from app.gateway.capabilities import ModelCapabilities, ReasoningEffort


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class TextPart(StrictModel):
    type: Literal["text"]
    text: str = Field(max_length=100000)


class ImageURL(StrictModel):
    url: str = Field(min_length=1, max_length=6000000)
    detail: Literal["auto", "low", "high"] = "auto"


class ImagePart(StrictModel):
    type: Literal["image_url"]
    image_url: ImageURL


ContentPart = Annotated[TextPart | ImagePart, Field(discriminator="type")]


class CalledFunction(StrictModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    arguments: str = Field(max_length=100000)


class ToolCall(StrictModel):
    id: str = Field(min_length=1, max_length=200)
    type: Literal["function"] = "function"
    function: CalledFunction


class Message(StrictModel):
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | list[ContentPart] | None = None
    tool_calls: list[ToolCall] | None = Field(default=None, max_length=50)
    tool_call_id: str | None = Field(default=None, min_length=1, max_length=200)
    name: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,64}$")

    def text(self) -> str:
        if isinstance(self.content, str):
            return self.content
        return "".join(p.text for p in self.content or [] if isinstance(p, TextPart))

    def images(self) -> list[ImagePart]:
        if not isinstance(self.content, list):
            return []
        return [p for p in self.content if isinstance(p, ImagePart)]

    @model_validator(mode="after")
    def valid_message(self):
        if isinstance(self.content, str) and len(self.content) > 100000:
            raise ValueError("Message is too long")
        if isinstance(self.content, list) and len(self.content) > 40:
            raise ValueError("Too many content parts")
        if self.role == "tool" and not self.tool_call_id:
            raise ValueError("tool_call_id required")
        if self.role != "tool" and self.tool_call_id is not None:
            raise ValueError("tool_call_id is only valid for tool results")
        if self.tool_calls and self.role != "assistant":
            raise ValueError("Only assistant messages may contain tool_calls")
        if self.images() and self.role != "user":
            raise ValueError("Images are supported in user messages only")
        if not self.content and not self.tool_calls and self.role != "tool":
            raise ValueError("content or tool_calls required")
        return self


class FunctionDefinition(StrictModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    description: str | None = Field(default=None, max_length=10000)
    parameters: dict = Field(default_factory=lambda: {"type": "object", "properties": {}})
    strict: bool | None = None


class ToolDefinition(StrictModel):
    type: Literal["function"] = "function"
    function: FunctionDefinition


class FunctionChoice(StrictModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")


class NamedToolChoice(StrictModel):
    type: Literal["function"] = "function"
    function: FunctionChoice


class JSONSchema(StrictModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    description: str | None = Field(default=None, max_length=10000)
    schema_: dict = Field(alias="schema")
    strict: bool = True


class ResponseFormat(StrictModel):
    type: Literal["text", "json_object", "json_schema"]
    json_schema: JSONSchema | None = None

    @model_validator(mode="after")
    def correct_schema(self):
        if (self.type == "json_schema") != (self.json_schema is not None):
            raise ValueError("json_schema is required only for type=json_schema")
        return self


class StreamOptions(StrictModel):
    include_usage: bool = False


class ChatRequest(StrictModel):
    safety_receipt: str | None = Field(default=None, max_length=100)
    model: str = Field(min_length=1, max_length=100)
    messages: list[Message] = Field(min_length=1, max_length=100)
    max_tokens: int | None = Field(default=None, ge=1, le=1000000)
    max_completion_tokens: int | None = Field(default=None, ge=1, le=1000000)
    temperature: float | None = Field(default=None, ge=0, le=2)
    stream: bool = False
    stream_options: StreamOptions | None = None
    tools: list[ToolDefinition] | None = Field(default=None, min_length=1, max_length=50)
    tool_choice: Literal["auto", "none", "required"] | NamedToolChoice | None = None
    parallel_tool_calls: bool | None = None
    response_format: ResponseFormat | None = None
    reasoning_effort: ReasoningEffort | None = None
    n: Literal[1] = 1

    @model_validator(mode="before")
    @classmethod
    def unsupported_media(cls, value):
        if isinstance(value, dict):
            for message in (
                value.get("messages", []) if isinstance(value.get("messages"), list) else []
            ):
                content = message.get("content") if isinstance(message, dict) else None
                for part in content if isinstance(content, list) else []:
                    if isinstance(part, dict) and part.get("type") in (
                        "input_audio",
                        "audio_url",
                        "video_url",
                        "input_video",
                        "file",
                        "document",
                    ):
                        from app.gateway.capabilities import unsupported

                        unsupported("messages", "audio, video or native document payloads")
        return value

    @model_validator(mode="after")
    def bounded_request(self):
        if self.max_tokens is not None and self.max_completion_tokens is not None:
            raise ValueError("Use only one output token limit")
        if self.stream_options is not None and not self.stream:
            raise ValueError("stream_options requires stream=true")
        if (
            self.tool_choice is not None or self.parallel_tool_calls is not None
        ) and not self.tools:
            raise ValueError("Tool options require tools")
        names = [t.function.name for t in self.tools or []]
        if len(names) != len(set(names)):
            raise ValueError("Tool names must be unique")
        if (
            isinstance(self.tool_choice, NamedToolChoice)
            and self.tool_choice.function.name not in names
        ):
            raise ValueError("Named tool must be defined")
        pending, seen = set(), set()
        for message in self.messages:
            if pending and message.role != "tool":
                raise ValueError("Every tool call requires a following tool result")
            if message.role == "tool":
                if message.tool_call_id not in pending:
                    raise ValueError("Tool result must match a pending tool call")
                pending.remove(message.tool_call_id)
            for call in message.tool_calls or []:
                if call.id in seen:
                    raise ValueError("Tool call IDs must be unique")
                seen.add(call.id)
                pending.add(call.id)
                try:
                    arguments = json.loads(call.function.arguments)
                    if not isinstance(arguments, dict):
                        raise ValueError()
                    json.dumps(arguments, allow_nan=False)
                except (ValueError, TypeError):
                    raise ValueError("Tool arguments must be a JSON object") from None
        if pending:
            raise ValueError("Missing tool results")
        data = self.model_dump(exclude_none=True, by_alias=True)
        images = 0
        for message in data["messages"]:
            if isinstance(message.get("content"), list):
                for part in message["content"]:
                    if part["type"] == "image_url":
                        images += 1
                        part["image_url"]["url"] = "<image>"
        if images > 20 or len(json.dumps(data, allow_nan=False).encode()) > 512000:
            raise ValueError("Request is too large")
        return self

    def output_limit(self, default=1024):
        return self.max_completion_tokens or self.max_tokens or default


class ModelInput(StrictModel):
    alias: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,99}$")
    provider: Literal["bedrock", "amazon_bedrock"] = "bedrock"
    upstream_model: str = Field(min_length=1, max_length=200)
    input_price: Decimal = Field(
        default=Decimal(0), ge=0, le=10000, max_digits=18, decimal_places=8
    )
    output_price: Decimal = Field(
        default=Decimal(0), ge=0, le=10000, max_digits=18, decimal_places=8
    )
    max_output_tokens: int | None = Field(default=4096, ge=1, le=1000000)
    max_context_tokens: int | None = Field(default=None, ge=1, le=10000000)
    enabled: bool = True
    external_api_allowed: bool = True
    provider_transport: str | None = Field(default=None, max_length=40)
    capabilities: ModelCapabilities | None = None
    model_key: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,99}$")
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    manufacturer: str | None = Field(default=None, min_length=1, max_length=100)
    region: str | None = Field(default=None, max_length=40)
    api_route: Literal["mantle_openai", "mantle_default"] | None = None
    model_type: str | None = Field(default=None, max_length=40)
    is_public: bool = True
    supports_vision: bool = False
    supports_reasoning: bool = False
    reasoning_efforts: list[ReasoningEffort] = Field(default_factory=list, max_length=6)
    vision_behavior: Literal["none", "native_vision", "vision_fallback"] = "none"

    @model_validator(mode="after")
    def context_limit(self):
        if (
            self.max_context_tokens
            and self.max_output_tokens
            and self.max_output_tokens > self.max_context_tokens
        ):
            raise ValueError("Output limit cannot exceed context limit")
        return self


class KeyInput(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    user_id: str | None = Field(default=None, min_length=1, max_length=100)
    safety_enabled: bool = False
    all_public_models: bool = False
    models: list[str] = Field(default_factory=list, max_length=100)
    budget_usd: Decimal | None = Field(
        default=None, gt=0, le=1000000, max_digits=18, decimal_places=8
    )
    rpm: int | None = Field(default=30, ge=1, le=100000, description="null disables the RPM limit")
    tpm: int | None = Field(
        default=2000000, ge=1, le=1000000000, description="null disables the TPM limit"
    )
    requests_per_day: int | None = Field(default=None, ge=1, le=1000000000)
    requests_per_month: int | None = Field(default=None, ge=1, le=10000000000)
    tokens_per_day: int | None = Field(default=None, ge=1, le=1000000000000)
    tokens_per_month: int | None = Field(default=None, ge=1, le=10000000000000)
    max_concurrent_requests: int | None = Field(default=5, ge=1, le=10000)
    expires_at: datetime | None = None
    active: bool = True

    @model_validator(mode="after")
    def timezone_required(self):
        if self.expires_at and self.expires_at.tzinfo is None:
            raise ValueError("expires_at must include timezone")
        return self


class CredentialInput(StrictModel):
    provider: Literal["bedrock", "amazon_bedrock"] = "bedrock"
    api_key: SecretStr = Field(min_length=10, max_length=10000)


class UserInput(StrictModel):
    username: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._@-]*$")
    active: bool = True


class UserUpdate(StrictModel):
    active: bool
