"""Model capabilities are declarations bounded by each adapter's implementation."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.providers.base import ProviderError

ToolChoiceMode = Literal["auto", "none", "required", "function"]
ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh"]


class ModelCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text_input: bool = True
    text_output: bool = True
    streaming: bool = False
    system_prompt: bool = False
    tools: bool = False
    tool_choice: bool = False
    tool_choice_modes: list[ToolChoiceMode] = Field(default_factory=list, max_length=4)
    parallel_tool_calls: bool = False
    strict_tools: bool = False
    structured_output: bool = False
    json_mode: bool = False
    json_schema: bool = False
    vision: bool = False
    image_input: bool = False
    reasoning: bool = False
    reasoning_efforts: list[ReasoningEffort] = Field(default_factory=list, max_length=6)
    temperature: bool = True

    @model_validator(mode="after")
    def consistent(self):
        if (self.tool_choice or self.parallel_tool_calls or self.strict_tools) and not self.tools:
            raise ValueError("Tool capabilities require tools")
        if bool(self.tool_choice_modes) != self.tool_choice:
            raise ValueError("tool_choice requires explicit tool_choice_modes")
        if self.structured_output != self.json_schema:
            raise ValueError("structured_output means native JSON schema support")
        if self.vision != self.image_input:
            raise ValueError("This chat API supports vision through image input")
        if bool(self.reasoning_efforts) != self.reasoning:
            raise ValueError("reasoning requires explicit reasoning_efforts")
        return self


def legacy_capabilities(provider: str) -> ModelCapabilities:
    """Preserve features of existing text/tool routes without inferring new capabilities."""
    if provider != "bedrock":
        raise ValueError("Only Bedrock upstream is supported")
    modes = ["auto", "none"]
    return ModelCapabilities(
        streaming=True, system_prompt=True, tools=True, tool_choice=True, tool_choice_modes=modes
    )


def unsupported(param: str, feature: str):
    raise ProviderError(
        "unsupported_capability",
        400,
        param=param,
        message=f"The selected model does not support {feature}.",
        usage_unknown=False,
    )


def validate_request(request, caps: ModelCapabilities, *, vision_fallback=False):
    if not caps.text_output:
        unsupported("model", "chat text output")
    if not caps.text_input and any(message.text() for message in request.messages):
        unsupported("messages", "text input")
    if request.stream and not caps.streaming:
        unsupported("stream", "streaming")
    if any(m.role in ("system", "developer") for m in request.messages) and not caps.system_prompt:
        unsupported("messages", "system/developer prompts")
    if any(m.images() for m in request.messages) and not (caps.image_input or vision_fallback):
        unsupported("messages", "image input")
    uses_tools = request.tools or any(m.tool_calls or m.role == "tool" for m in request.messages)
    if uses_tools and not caps.tools:
        unsupported("tools", "function tools and tool results")
    if request.tool_choice is not None:
        choice = request.tool_choice if isinstance(request.tool_choice, str) else "function"
        if choice not in caps.tool_choice_modes:
            unsupported("tool_choice", f"tool_choice={choice}")
    if request.parallel_tool_calls is not None and not caps.parallel_tool_calls:
        unsupported("parallel_tool_calls", "controlling parallel tool calls")
    if any(t.function.strict for t in request.tools or []) and not caps.strict_tools:
        unsupported("tools", "strict function schemas")
    if request.response_format:
        kind = request.response_format.type
        if kind == "json_object" and not caps.json_mode:
            unsupported("response_format", "native JSON mode")
        if kind == "json_schema" and not caps.json_schema:
            unsupported("response_format", "native JSON schema structured output")
    if request.reasoning_effort is not None:
        if request.reasoning_effort not in caps.reasoning_efforts:
            unsupported("reasoning_effort", "this reasoning effort")
    if request.temperature is not None and not caps.temperature:
        unsupported("temperature", "temperature sampling")
