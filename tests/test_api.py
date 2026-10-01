from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from fastapi.testclient import TestClient

from app.admin.routes import require_admin
from app.api.dependencies import get_inference_service
from app.core.config import Settings
from app.main import create_app
from app.providers.base import ProviderError
from app.providers.bedrock import BedrockProvider
from app.schemas.inference import InferenceRequest, InferenceResponse
from app.services.inference import InferenceService


@pytest.fixture
def api():
    application = create_app()
    application.dependency_overrides[require_admin] = lambda: None
    service = Mock(spec=InferenceService)
    service.generate.return_value = InferenceResponse(provider="fake", model="fake", text="OK")
    application.dependency_overrides[get_inference_service] = lambda: service
    with TestClient(application) as client:
        yield client, service


def test_health():
    with TestClient(create_app()) as client:
        assert client.get("/health").json() == {"status": "ok"}


def test_inference_endpoint(api):
    client, service = api
    response = client.post("/internal/test/bedrock", json={"prompt": "Hello"})
    assert response.status_code == 200
    assert response.json()["text"] == "OK"
    assert service.generate.call_args.args[0].prompt == "Hello"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"prompt": ""},
        {"prompt": "   "},
        {"prompt": "x", "max_tokens": 0},
        {"prompt": "x", "temperature": 2},
        {"prompt": "x" * 32001},
    ],
)
def test_invalid_request_does_not_call_provider(api, payload):
    client, service = api
    assert client.post("/internal/test/bedrock", json=payload).status_code == 422
    service.generate.assert_not_called()


def test_safe_error_response(api):
    client, service = api
    service.generate.side_effect = ProviderError("bedrock_access_denied", 502)
    response = client.post("/internal/test/bedrock", json={"prompt": "Hello"})
    assert response.status_code == 502
    assert response.json() == {"detail": {"code": "bedrock_access_denied"}}


def test_converse_normalizes_response():
    sdk = Mock()
    sdk.converse.return_value = {
        "output": {"message": {"content": [{"text": "Hello"}, {"text": " world"}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 2, "outputTokens": 3, "totalTokens": 5},
    }
    provider = BedrockProvider(Settings(_env_file=None), client=sdk)
    result = InferenceService(provider, model="test-model").generate(InferenceRequest(prompt="Hi"))
    assert result.text == "Hello world"
    assert result.usage.total_tokens == 5
    assert result.model == "test-model"
    sdk.converse.assert_called_once_with(
        modelId="test-model",
        messages=[{"role": "user", "content": [{"text": "Hi"}]}],
        inferenceConfig={"maxTokens": 256, "temperature": 0.2},
    )


def test_sdk_error_does_not_expose_message():
    sdk = Mock()
    sdk.converse.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "sensitive upstream content"}},
        "Converse",
    )
    provider = BedrockProvider(Settings(_env_file=None), client=sdk)
    with pytest.raises(ProviderError) as caught:
        provider.generate(InferenceRequest(prompt="Hi"), model="test")
    assert str(caught.value) == "bedrock_access_denied"
    assert caught.value.__suppress_context__


def test_missing_token():
    provider = BedrockProvider(Settings(_env_file=None, aws_bearer_token_bedrock=None))
    with pytest.raises(ProviderError, match="bedrock_not_configured"):
        provider.generate(InferenceRequest(prompt="Hi"), model="test")


def test_network_failure():
    sdk = Mock()
    sdk.converse.side_effect = EndpointConnectionError(endpoint_url="https://example.invalid")
    provider = BedrockProvider(Settings(_env_file=None), client=sdk)
    with pytest.raises(ProviderError, match="bedrock_connection_failed"):
        provider.generate(InferenceRequest(prompt="Hi"), model="test")


def test_invalid_upstream_response():
    sdk = Mock()
    sdk.converse.return_value = {}
    provider = BedrockProvider(Settings(_env_file=None), client=sdk)
    with pytest.raises(ProviderError, match="bedrock_invalid_response"):
        provider.generate(InferenceRequest(prompt="Hi"), model="test")


def test_secret_is_masked():
    settings = Settings(_env_file=None, aws_bearer_token_bedrock="test-secret")
    assert "test-secret" not in repr(settings)
