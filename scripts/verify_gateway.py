"""Public API smoke with optional real Nova Micro calls; ephemeral key, safe output only."""

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import httpx2 as httpx
from openai import OpenAI

from app.core.config import get_settings


def verify(base_url, real_bedrock=False):
    url = urlsplit(base_url)
    local = url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost"}
    public = url.scheme == "https" and url.hostname == "api.myais.uz" and url.port in (None, 443)
    if not (local or public) or url.username or url.password or url.path not in ("", "/"):
        raise ValueError("Use a loopback URL or the existing public AiS domain")
    token = get_settings().admin_token
    if not token:
        raise ValueError("Admin credential is not configured")
    key_id = None
    results = {
        "health": False,
        "auth": False,
        "models": False,
        "real_bedrock": real_bedrock,
        "calls": [],
        "key_revoked": False,
    }
    with httpx.Client(
        base_url=base_url,
        timeout=90,
        trust_env=False,
        headers={"Authorization": "Bearer " + token.get_secret_value()},
    ) as admin:
        try:
            health = admin.get("/health")
            health.raise_for_status()
            assert health.json() == {"status": "ok"}
            results["health"] = True
            invalid = admin.get("/v1/models", headers={"Authorization": "Bearer sk-myais-invalid"})
            assert (
                invalid.status_code == 401 and invalid.json()["error"]["code"] == "invalid_api_key"
            )
            results["auth"] = True
            overview = admin.get("/admin/api/overview")
            overview.raise_for_status()
            route = next(m for m in overview.json()["models"] if m["alias"] == "ais-fast")
            assert route["enabled"] and route["provider"] == "bedrock"
            assert route["upstream_model"] == "amazon.nova-micro-v1:0"
            created = admin.post(
                "/admin/api/keys",
                json={
                    "name": "Gateway verification (temporary)",
                    "models": ["ais-fast"],
                    "rpm": 10,
                    "tpm": 100000,
                    "requests_per_day": 10,
                    "requests_per_month": 10,
                    "tokens_per_day": 100000,
                    "tokens_per_month": 100000,
                    "max_concurrent_requests": 1,
                    "budget_usd": "0.05",
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
                },
            )
            created.raise_for_status()
            issued = created.json()
            key_id = issued["id"]
            with OpenAI(
                api_key=issued["key"],
                base_url=base_url.rstrip("/") + "/v1",
                max_retries=0,
                timeout=90,
                http_client=httpx.Client(trust_env=False),
            ) as sdk:
                assert [m.id for m in sdk.models.list()] == ["ais-fast"]
                results["models"] = True
                if real_bedrock:
                    request = {
                        "model": "ais-fast",
                        "messages": [{"role": "user", "content": "Reply with exactly OK"}],
                        "max_tokens": 16,
                        "temperature": 0,
                    }
                    response = sdk.chat.completions.create(**request)
                    assert response.choices[0].message.content.strip() == "OK"
                    assert response.model == "ais-fast" and response.usage.total_tokens > 0
                    results["calls"].append({"kind": "text", "tokens": response.usage.total_tokens})
                    text, usage, reason = [], None, None
                    with sdk.chat.completions.create(
                        **request, stream=True, stream_options={"include_usage": True}
                    ) as stream:
                        for chunk in stream:
                            assert chunk.model == "ais-fast"
                            if chunk.usage:
                                usage = chunk.usage
                            for choice in chunk.choices:
                                text.append(choice.delta.content or "")
                                reason = choice.finish_reason or reason
                    assert "".join(text).strip() == "OK" and usage and reason == "stop"
                    results["calls"].append({"kind": "stream", "tokens": usage.total_tokens})
                    tools = [
                        {
                            "type": "function",
                            "function": {
                                "name": "smoke_echo",
                                "description": "Return the supplied text.",
                                "parameters": {
                                    "type": "object",
                                    "properties": {"text": {"type": "string", "enum": ["OK"]}},
                                    "required": ["text"],
                                    "additionalProperties": False,
                                },
                            },
                        }
                    ]
                    messages = [
                        {
                            "role": "user",
                            "content": "Call smoke_echo with text OK. "
                            "You must use this tool before answering. "
                            "After receiving the tool result, reply with exactly OK "
                            "and nothing else.",
                        }
                    ]
                    tool_calls, text, usage, reason = {}, [], None, None
                    with sdk.chat.completions.create(
                        model="ais-fast",
                        messages=messages,
                        tools=tools,
                        tool_choice="auto",
                        stream=True,
                        stream_options={"include_usage": True},
                        max_tokens=128,
                        temperature=0,
                    ) as stream:
                        for chunk in stream:
                            if chunk.usage:
                                usage = chunk.usage
                            for choice in chunk.choices:
                                text.append(choice.delta.content or "")
                                reason = choice.finish_reason or reason
                                for delta in choice.delta.tool_calls or []:
                                    stored = tool_calls.setdefault(
                                        delta.index,
                                        {
                                            "id": "",
                                            "type": "function",
                                            "function": {"name": "", "arguments": ""},
                                        },
                                    )
                                    if delta.id:
                                        stored["id"] = delta.id
                                    if delta.function:
                                        stored["function"]["name"] += delta.function.name or ""
                                        stored["function"]["arguments"] += (
                                            delta.function.arguments or ""
                                        )
                    assert len(tool_calls) == 1 and reason == "tool_calls" and usage
                    call = tool_calls[0]
                    assert call["function"]["name"] == "smoke_echo"
                    assert json.loads(call["function"]["arguments"]) == {"text": "OK"}
                    results["calls"].append(
                        {"kind": "stream_tool_call", "tokens": usage.total_tokens}
                    )
                    messages.extend(
                        [
                            {
                                "role": "assistant",
                                "content": "".join(text) or None,
                                "tool_calls": [call],
                            },
                            {"role": "tool", "tool_call_id": call["id"], "content": "OK"},
                        ]
                    )
                    response = sdk.chat.completions.create(
                        model="ais-fast",
                        messages=messages,
                        tools=tools,
                        max_tokens=64,
                        temperature=0,
                    )
                    results["tool_result_finish_reason"] = response.choices[0].finish_reason
                    assert response.choices[0].finish_reason == "stop"
                    assert "OK" in response.choices[0].message.content
                    results["calls"].append(
                        {"kind": "tool_result", "tokens": response.usage.total_tokens}
                    )
            overview = admin.get("/admin/api/overview").json()
            key = next(k for k in overview["keys"] if k["id"] == key_id)
            entries = [u for u in overview["usage"] if u["key_id"] == key_id]
            assert float(key["reserved_usd"]) == 0 and key["active_requests"] == 0
            assert len(entries) == len(results["calls"])
            assert all(u["status"] == "completed" for u in entries)
            results["reservations_released"] = True
        except Exception as exc:
            exc.verification_results = results
            raise
        finally:
            if key_id is not None:
                revoked = admin.post("/admin/api/keys/" + key_id + "/revoke")
                results["key_revoked"] = revoked.status_code == 200
                if not results["key_revoked"]:
                    raise RuntimeError("Temporary key revocation failed")
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--real-bedrock", action="store_true", help="Make four small paid Nova calls"
    )
    options = parser.parse_args()
    try:
        print(json.dumps(verify(options.base_url, options.real_bedrock)))
        return 0
    except Exception as exc:
        # APIError/HTTPError reprs can include request details. Do not print them.
        print(
            json.dumps(
                {
                    "smoke": "failed",
                    "error_type": type(exc).__name__,
                    "http_status": getattr(exc, "status_code", None),
                    "verification": getattr(exc, "verification_results", None),
                }
            )
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
