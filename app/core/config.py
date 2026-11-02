from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8-sig",
        extra="ignore",
        hide_input_in_errors=True,
    )

    aws_bearer_token_bedrock: SecretStr | None = None
    aws_region: str = Field(default="us-east-1", min_length=1)
    openai_api_key: SecretStr | None = None
    openai_base_url: str = "https://bedrock-mantle.us-east-1.api.aws/v1"
    ai_default_max_output_tokens: int = Field(default=4096, ge=1, le=1000000)
    ai_retry_count: int = Field(default=2, ge=0, le=5)
    ai_request_timeout_seconds: float = Field(default=120, gt=0, le=600)
    ai_retry_backoff_seconds: float = Field(default=0.5, ge=0, le=10)
    vision_helper_model_key: str = "qwen3_vl_235b_a22b"
    moderation_model_key: str = "gpt_oss_safeguard_20b"
    database_url: SecretStr | None = None
    admin_token: SecretStr | None = None
    public_api_base_url: str | None = None

    bedrock_model_id: str = Field(default="amazon.nova-micro-v1:0", min_length=1)
    reservation_ttl_seconds: int = Field(default=180, ge=90, le=3600)
    reservation_heartbeat_seconds: int = Field(default=30, ge=5, le=60)
    reservation_reaper_seconds: int = Field(default=30, ge=5, le=60)

    @field_validator("public_api_base_url")
    @classmethod
    def validate_public_api_base_url(cls, value):
        if value is None or not value.strip():
            return None
        try:
            parsed = urlsplit(value.strip())
            valid = (
                parsed.scheme.lower() in ("http", "https")
                and bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
                and not parsed.netloc.endswith(":")
                and not any(char.isspace() or ord(char) < 33 for char in parsed.netloc)
                and not parsed.query
                and not parsed.fragment
                and (parsed.port is None or 1 <= parsed.port <= 65535)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(
                "PUBLIC_API_BASE_URL must be an absolute HTTP(S) URL without credentials, "
                "query, or fragment"
            )
        path = parsed.path.rstrip("/")
        if not path.endswith("/v1"):
            path += "/v1"
        return urlunsplit((parsed.scheme.lower(), parsed.netloc, path, "", ""))

    @property
    def mantle_api_key(self) -> SecretStr | None:
        # The existing installation already keeps this same server-side Bedrock
        # bearer under AWS_BEARER_TOKEN_BEDROCK. OPENAI_API_KEY always takes priority.
        return self.openai_api_key or self.aws_bearer_token_bedrock


@lru_cache
def get_settings() -> Settings:
    return Settings()
