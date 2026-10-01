from pydantic import BaseModel, Field, field_validator


class InferenceRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=32000)
    max_tokens: int = Field(default=256, ge=1, le=4096)
    temperature: float = Field(default=0.2, ge=0, le=1)

    @field_validator("prompt")
    @classmethod
    def reject_blank_prompt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Prompt must contain text")
        return value


class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class InferenceResponse(BaseModel):
    provider: str
    model: str
    text: str
    stop_reason: str | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
