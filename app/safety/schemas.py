from typing import Literal

from pydantic import Field

from app.gateway.schemas import StrictModel


class SafetyTurn(StrictModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=12000)


class SafetyContext(StrictModel):
    kind: Literal["text", "document", "image"] = "text"
    user_request: str = Field(min_length=1, max_length=96000)
    untrusted_material: str | None = Field(default=None, max_length=96000)
    recent_conversation: list[SafetyTurn] = Field(default_factory=list, max_length=8)
    previous_refused_requests: list[str] = Field(default_factory=list, max_length=3)
    history_truncated: bool = False


class SafetyRequest(StrictModel):
    selected_model: str = Field(min_length=1, max_length=100)
    policy_version: Literal["ais_safety_v1", "ais_safety_v2"] = "ais_safety_v1"
    context: SafetyContext
    image: str | None = Field(default=None, max_length=6000000)
