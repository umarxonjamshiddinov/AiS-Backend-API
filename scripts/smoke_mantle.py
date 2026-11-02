"""Exercise a local API using one temporary key; --live makes two small paid calls."""

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import uuid4

import httpx2
from openai import OpenAI

from app.core.config import get_settings
from app.providers.mantle import silence_sdk_payload_logs

SMOKE_MODELS = (("xai.grok-4.3", 512), ("google.gemma-3-4b-it", 64))
SAFE_CODES = {
    "upstream_rate_limit",
    "upstream_timeout",
    "upstream_error",
    "invalid_api_key",
    "model_not_found",
    "model_not_allowed",
    "provider_unavailable",
    "content_filter",
}


def verify(base_url, *, live=False):
    parsed = urlsplit(base_url)
    allowed = parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost")
    if (
        not allowed
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ValueError("Use a loopback URL for this smoke test")
    config = get_settings()
    if not config.admin_token:
        raise ValueError("ADMIN_TOKEN is required")
    silence_sdk_payload_logs()
    result = {
        "base_url": base_url,
        "live_requested": live,
        "calls": [],
        "temporary_key_revoked": False,
        "temporary_user_disabled": False,
    }
    key, user_id = None, None
    with httpx2.Client(
        base_url=base_url,
        timeout=15,
        trust_env=False,
        follow_redirects=False,
        headers={"Authorization": "Bearer " + config.admin_token.get_secret_value()},
    ) as admin:
        try:
            ready = admin.get("/ready")
            ready.raise_for_status()
            assert ready.json()["catalog_models"] == 42
            result["ready"] = True
            created = admin.post("/admin/api/users", json={"username": "smoke-" + uuid4().hex})
            created.raise_for_status()
            user_id = created.json()["id"]
            created = admin.post(
                "/admin/api/keys",
                json={
                    "user_id": user_id,
                    "name": "Temporary Mantle smoke",
                    "all_public_models": True,
                    "rpm": 10,
                    "tpm": 2000000,
                    "max_concurrent_requests": 1,
                    "requests_per_day": 10,
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
                },
            )
            created.raise_for_status()
            key = created.json()
            result["key_id"] = key["id"]  # Identifier only, useful for audit/revocation.
            with OpenAI(
                api_key=key["key"],
                base_url=base_url.rstrip("/") + "/v1",
                timeout=400,
                max_retries=0,
                http_client=httpx2.Client(trust_env=False, follow_redirects=False),
            ) as sdk:
                ids = {m.id for m in sdk.models.list()}
                assert len(ids) == 40 and not any("safeguard" in model for model in ids)
                result["public_models"] = len(ids)
                if live and not config.mantle_api_key:
                    result["live_skipped"] = "Server upstream credentials are absent"
                elif live:
                    for model, output_limit in SMOKE_MODELS:
                        try:
                            response = sdk.chat.completions.create(
                                model=model,
                                messages=[{"role": "user", "content": "Reply with exactly OK."}],
                                max_tokens=output_limit,
                            )
                            content = response.choices[0].message.content
                            assert content and content.strip() and response.model == model
                            result["calls"].append(
                                {
                                    "model": model,
                                    "status": "completed",
                                    "has_text": True,
                                    "text_is_OK": content.strip() == "OK",
                                    "finish_reason": response.choices[0].finish_reason,
                                    "usage": response.usage.model_dump()
                                    if response.usage
                                    else None,
                                }
                            )
                        except Exception as exc:
                            code = getattr(exc, "code", None)
                            result["calls"].append(
                                {
                                    "model": model,
                                    "status": "failed",
                                    "http_status": getattr(exc, "status_code", None),
                                    "error": code
                                    if isinstance(code, str) and code in SAFE_CODES
                                    else type(exc).__name__,
                                }
                            )
            overview = admin.get("/admin/api/overview")
            overview.raise_for_status()
            usage = [u for u in overview.json()["usage"] if u["key_id"] == key["id"]]
            result["usage_records"] = len(usage)
            result["usage_linked_to_owner"] = all(u["user_id"] == user_id for u in usage)
            result["reservations_released"] = all(u["status"] != "reserved" for u in usage)
        finally:
            if key:
                revoked = admin.post("/admin/api/keys/" + key["id"] + "/revoke")
                result["temporary_key_revoked"] = revoked.status_code == 200
                check = admin.get("/v1/models", headers={"Authorization": "Bearer " + key["key"]})
                result["revoked_key_rejected"] = check.status_code == 401
            if user_id:
                disabled = admin.put("/admin/api/users/" + user_id, json={"active": False})
                result["temporary_user_disabled"] = disabled.status_code == 200
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    try:
        result = verify(args.base_url, live=args.live)
        print(json.dumps(result, ensure_ascii=False))
        succeeded = result["temporary_key_revoked"] and result["revoked_key_rejected"]
        if args.live:
            succeeded = (
                succeeded
                and len(result["calls"]) == 2
                and all(c["status"] == "completed" for c in result["calls"])
            )
        return 0 if succeeded else 1
    except Exception as exc:
        print(json.dumps({"smoke": "failed", "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
