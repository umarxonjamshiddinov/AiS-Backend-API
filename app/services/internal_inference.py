"""Server-only Safety calls through the same DB catalog and Mantle provider.

Callers supply their versioned policy prompt and parse the returned classifier text.
No new policy is silently enabled on public chat, and no user key can call this API.
"""

import time
from datetime import UTC, datetime

from app.core.config import get_settings
from app.db.models import Usage
from app.db.session import session_factory
from app.gateway.providers import registry
from app.gateway.quota import input_token_bound
from app.gateway.schemas import ChatRequest
from app.providers.base import ProviderError
from app.services.models import resolve_model, vision_helper


def complete_safety(messages, *, model_key=None, adaptive=False, factory=None):
    """Use 1024 tokens (v1) or 2048 (adaptive); image assessment uses the Qwen helper."""
    factory = factory or session_factory()
    request = ChatRequest(
        model=model_key or get_settings().moderation_model_key,
        messages=messages,
        max_tokens=2048 if adaptive else 1024,
    )
    with factory.begin() as db:
        if any(m.images() for m in request.messages):
            route = vision_helper(db)
        else:
            route = resolve_model(db, request.model)
            if route.is_public or route.model_type != "safeguard":
                raise ProviderError("model_not_allowed", 403, usage_unknown=False)
        if not route.enabled:
            raise ProviderError("model_not_found", 404, usage_unknown=False)
        adapter, call = registry.prepare(route, request, internal=True)
        input_token_bound(call.request, route)
        entry = Usage(
            key_id=None,
            user_id=None,
            alias=route.alias,
            model_key=route.model_key,
            provider=route.provider,
            region=route.region,
            api_route=route.api_route,
            upstream_model=route.upstream_model,
            purpose="safety",
            status="running",
            reserved_tokens=0,
            accounted_tokens=0,
            reserved_cost=0,
            input_price=0,
            output_price=0,
            dispatched_at=datetime.now(UTC),
        )
        db.add(entry)
        db.flush()
        request_id = entry.id
    started = time.monotonic()
    error, result, usage = None, [], None
    try:
        for event in adapter.events(call):
            if event.delta.get("content"):
                result.append(event.delta["content"])
            if event.usage is not None:
                usage = event.usage
        return {"id": request_id, "model": route.alias, "text": "".join(result), "usage": usage}
    except ProviderError as exc:
        error = exc.code
        raise
    except Exception:
        error = "upstream_error"
        raise ProviderError(error) from None
    finally:
        actual = usage or call.metrics.get("usage")
        with factory.begin() as db:
            entry = db.get(Usage, request_id)
            entry.status = "failed" if error else "completed"
            entry.error_code = error
            entry.latency_ms = int((time.monotonic() - started) * 1000)
            entry.finished_at = datetime.now(UTC)
            entry.execution_metadata = call.metrics
            entry.usage_estimated = actual is None
            if actual:
                entry.input_tokens = actual["prompt_tokens"]
                entry.output_tokens = actual["completion_tokens"]
            if actual:
                from app.gateway.quota import cost

                entry.cost_usd = cost(
                    actual["prompt_tokens"],
                    actual["completion_tokens"],
                    route.input_price,
                    route.output_price,
                )
            entry.accounted_tokens = 0  # Internal Safety never debits a user key's quota.
