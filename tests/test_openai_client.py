import httpx2 as httpx
import pytest
from conftest import issue
from openai import AuthenticationError, OpenAI


def sdk_client(client, token):
    def send(request):
        response = client.request(
            request.method, request.url.path, content=request.read(), headers=dict(request.headers)
        )
        return httpx.Response(
            response.status_code, headers=response.headers, content=response.content
        )

    return OpenAI(
        api_key=token,
        base_url="http://testserver/v1",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(send)),
    )


def test_official_openai_client_text_stream_and_models(gateway):
    client, _ = gateway
    _, token, _ = issue(client)
    with sdk_client(client, token) as sdk:
        assert [m.id for m in sdk.models.list()] == ["ais-fast"]
        result = sdk.chat.completions.create(
            model="ais-fast", max_completion_tokens=16, messages=[{"role": "user", "content": "Hi"}]
        )
        assert result.model == "ais-fast" and result.choices[0].message.content == "OK"
        assert result.usage.total_tokens == 6
        with sdk.chat.completions.create(
            model="ais-fast",
            stream=True,
            max_tokens=16,
            messages=[{"role": "user", "content": "Hi"}],
            stream_options={"include_usage": True},
        ) as stream:
            chunks = list(stream)
        assert "".join(c.choices[0].delta.content or "" for c in chunks if c.choices) == "OK"
        assert chunks[-1].usage.total_tokens == 6


def test_official_openai_client_recognizes_authentication_errors(gateway):
    client, _ = gateway
    with sdk_client(client, "sk-myais-invalid") as sdk:
        with pytest.raises(AuthenticationError) as error:
            sdk.models.list()
        assert error.value.code == "invalid_api_key"
