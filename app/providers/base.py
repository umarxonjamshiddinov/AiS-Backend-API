from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol

from app.schemas.inference import InferenceRequest, InferenceResponse

if TYPE_CHECKING:
    from app.gateway.capabilities import ModelCapabilities
    from app.gateway.schemas import ChatRequest


class ProviderError(Exception):
    """Safe errors only: never include SDK exception messages or request content."""

    def __init__(
        self,
        code: str,
        status_code: int = 502,
        *,
        message: str | None = None,
        param: str | None = None,
        usage_unknown: bool = True,
        retry_after: int | None = None,
    ):
        self.code = code
        self.status_code = status_code
        self.message = message or code.replace("_", " ").capitalize()
        self.param = param
        self.usage_unknown = usage_unknown
        self.retry_after = retry_after
        super().__init__(code)

    def as_dict(self):
        error_type = "server_error"
        if self.status_code == 401:
            error_type = "authentication_error"
        elif self.status_code == 429:
            error_type = "rate_limit_error"
        elif self.status_code < 500:
            error_type = "invalid_request_error"
        return {
            "error": {
                "message": self.message,
                "type": error_type,
                "param": self.param,
                "code": self.code,
            }
        }


@dataclass(frozen=True)
class ChatInput:
    """Normalized request shared by adapters; the real model never leaves the server."""

    request: "ChatRequest"
    model: str
    capabilities: "ModelCapabilities"
    transport: str
    region: str | None = None
    api_route: str | None = None
    model_key: str | None = None
    vision_behavior: str = "none"
    max_context_tokens: int | None = None
    vision_helper: "ChatInput | None" = None
    # Internal numeric/safe routing metadata only; never messages or credentials.
    metrics: dict = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class ChatEvent:
    """Only these normalized fields may enter a public completion or SSE chunk."""

    delta: dict = field(default_factory=dict)
    finish_reason: Literal["stop", "length", "tool_calls", "content_filter"] | None = None
    usage: dict[str, int] | None = None


class ChatAdapter(Protocol):
    transport: str

    def validate(self, call: ChatInput) -> None: ...

    def events(self, call: ChatInput): ...

    def configured(self) -> bool: ...


class InferenceProvider(Protocol):
    def generate(self, request: InferenceRequest, *, model: str) -> InferenceResponse: ...
