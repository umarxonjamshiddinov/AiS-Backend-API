import os
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import Settings
from app.providers.base import ProviderError
from app.schemas.inference import InferenceRequest, InferenceResponse, TokenUsage


class BedrockProvider:
    def __init__(self, settings: Settings, *, client: Any = None):
        self.settings = settings
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            token = self.settings.aws_bearer_token_bedrock
            if token is None or not token.get_secret_value().strip():
                raise ProviderError("bedrock_not_configured", 503)
            # The official SDK reads this variable for Bedrock bearer authentication.
            os.environ["AWS_BEARER_TOKEN_BEDROCK"] = token.get_secret_value()
            self._client = boto3.client(
                "bedrock-runtime",
                region_name=self.settings.aws_region,
                config=Config(
                    connect_timeout=10,
                    read_timeout=60,
                    retries={"mode": "standard", "total_max_attempts": 1},
                ),
            )
        return self._client

    def generate(self, request: InferenceRequest, *, model: str) -> InferenceResponse:
        try:
            response = self._get_client().converse(
                modelId=model,
                messages=[{"role": "user", "content": [{"text": request.prompt}]}],
                inferenceConfig={
                    "maxTokens": request.max_tokens,
                    "temperature": request.temperature,
                },
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            errors = {
                "AccessDeniedException": ("bedrock_access_denied", 502),
                "UnrecognizedClientException": ("bedrock_authentication_failed", 502),
                "ExpiredTokenException": ("bedrock_authentication_failed", 502),
                "ValidationException": ("bedrock_invalid_model_or_request", 502),
                "ResourceNotFoundException": ("bedrock_model_not_found", 502),
                "ThrottlingException": ("bedrock_throttled", 503),
                "ServiceUnavailableException": ("bedrock_unavailable", 503),
            }
            safe_code, status = errors.get(code, ("bedrock_request_failed", 502))
            raise ProviderError(safe_code, status) from None
        except BotoCoreError:
            raise ProviderError("bedrock_connection_failed", 502) from None

        try:
            blocks = response["output"]["message"]["content"]
            text = "".join(block["text"] for block in blocks if "text" in block)
            if not text:
                raise ProviderError("bedrock_empty_response")
            usage = response.get("usage", {})
            return InferenceResponse(
                provider="bedrock",
                model=model,
                text=text,
                stop_reason=response.get("stopReason"),
                usage=TokenUsage(
                    input_tokens=usage.get("inputTokens", 0),
                    output_tokens=usage.get("outputTokens", 0),
                    total_tokens=usage.get("totalTokens", 0),
                ),
            )
        except (KeyError, TypeError, ValueError):
            raise ProviderError("bedrock_invalid_response") from None
