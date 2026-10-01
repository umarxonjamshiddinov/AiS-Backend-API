"""Atomic PostgreSQL admission, lease renewal and idempotent finalization."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal

from sqlalchemy import func, or_, select, update

from app.core.config import get_settings
from app.db.models import ApiKey, ModelRoute, Usage
from app.db.session import session_factory
from app.gateway.providers import registry
from app.providers.base import ChatAdapter, ChatInput, ProviderError
from app.services.models import accessible_models, permitted, public_route, vision_helper


def utcnow():
    return datetime.now(UTC)


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def cost(input_tokens, output_tokens, input_price, output_price):
    return (
        (Decimal(input_tokens) * input_price + Decimal(output_tokens) * output_price)
        / Decimal(1000000)
    ).quantize(Decimal("0.00000001"), rounding=ROUND_CEILING)


def check_key(key, at):
    if key is None:
        raise ProviderError("invalid_api_key", 401, usage_unknown=False)
    if key.revoked_at is not None:
        raise ProviderError("revoked_api_key", 401, usage_unknown=False)
    if not key.active:
        raise ProviderError("disabled_api_key", 401, usage_unknown=False)
    if key.expires_at and key.expires_at <= at:
        raise ProviderError("expired_api_key", 401, usage_unknown=False)
    if key.user is None or not key.user.active:
        raise ProviderError("inactive_user", 403, usage_unknown=False)


def authenticate(token):
    if not token or not token.startswith("sk-myais-") or len(token) > 200:
        raise ProviderError("invalid_api_key", 401, usage_unknown=False)
    with session_factory().begin() as db:
        key = db.scalar(select(ApiKey).where(ApiKey.key_hash == digest(token)))
        check_key(key, utcnow())
        key.last_used_at = utcnow()
        return key.id


@dataclass(frozen=True)
class Admission:
    id: str
    route: ModelRoute
    adapter: ChatAdapter
    call: ChatInput
    key_id: str | None = None


def input_token_bound(request, route):
    # UTF-8 bytes plus framing/tool overhead deliberately overestimate text tokens.
    payload = request.model_dump(exclude_none=True, by_alias=True)
    has_images = False
    for message in payload["messages"]:
        if isinstance(message.get("content"), list):
            for part in message["content"]:
                if part["type"] == "image_url":
                    has_images = True
                    part["image_url"]["url"] = "<image>"
    text_bound = len(json.dumps(payload, ensure_ascii=False).encode()) * 2
    mantle = (
        getattr(route, "provider", None) == "amazon_bedrock"
        or getattr(route, "transport", None) == "mantle_chat"
    )
    framing = 64 if mantle else 1024
    text_bound += framing * len(request.messages) + framing
    if route.max_context_tokens and text_bound + request.output_limit() > route.max_context_tokens:
        raise ProviderError(
            "invalid_request",
            400,
            param="messages",
            message="The conservative input estimate exceeds the model context limit.",
            usage_unknown=False,
        )
    # No universal image tokenizer exists. Reserve the full configured context for
    # vision until an adapter provides a verified CountTokens implementation.
    if has_images:
        if getattr(route, "vision_behavior", None) == "vision_fallback":
            # Only the helper sees pixels. Bound its 2048-token description for
            # a different tokenizer without charging the helper's own inference.
            bound = text_bound + 8192
            if bound + request.output_limit() > route.max_context_tokens:
                raise ProviderError(
                    "invalid_request",
                    400,
                    param="messages",
                    usage_unknown=False,
                    message="Image description would exceed the context limit.",
                )
            return bound
        return route.max_context_tokens - request.output_limit()
    return text_bound


def _finalize_locked(
    key, entry, usage, latency_ms, *, status, error_code=None, usage_unknown=True, at=None
):
    was_reserved = entry.status == "reserved"
    if not was_reserved and not (entry.status == "expired" and usage is not None):
        return False
    at = at or utcnow()
    if was_reserved:
        key.reserved_usd -= entry.reserved_cost
    else:
        # A response can arrive just after lease cleanup. Replace its estimate with
        # actual usage without reopening concurrency or charging the request twice.
        key.spent_usd -= entry.cost_usd
    entry.usage_estimated = usage is None and usage_unknown and entry.dispatched_at is not None
    if usage is not None:
        entry.input_tokens = usage["prompt_tokens"]
        entry.output_tokens = usage["completion_tokens"]
        entry.accounted_tokens = entry.input_tokens + entry.output_tokens
        entry.cost_usd = cost(
            entry.input_tokens, entry.output_tokens, entry.input_price, entry.output_price
        )
    elif entry.usage_estimated:
        entry.accounted_tokens = entry.reserved_tokens
        entry.cost_usd = entry.reserved_cost
    else:
        entry.accounted_tokens = 0
        entry.cost_usd = Decimal(0)
    key.spent_usd += entry.cost_usd
    entry.status = status
    entry.error_code = error_code
    entry.latency_ms = min(2147483647, max(0, latency_ms))
    entry.finished_at = at
    entry.lease_expires_at = None
    return True


def _expire_locked(db, key, at):
    entries = db.scalars(
        select(Usage)
        .where(
            Usage.key_id == key.id,
            Usage.status == "reserved",
            or_(Usage.lease_expires_at <= at, Usage.lease_expires_at.is_(None)),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    for entry in entries:
        _finalize_locked(
            key,
            entry,
            None,
            max(0, int((at - entry.created_at).total_seconds() * 1000)),
            status="expired",
            error_code="reservation_expired",
            at=at,
        )
    return len(entries)


def limit_error(param, kind, retry_after):
    raise ProviderError(
        kind,
        429,
        param=param,
        usage_unknown=False,
        message=f"The API key's {param} limit has been reached.",
        retry_after=max(1, retry_after),
    )


def reserve(key_id, request, *, purpose="chat"):
    with session_factory().begin() as db:
        key = db.scalar(select(ApiKey).where(ApiKey.id == key_id).with_for_update())
        at = utcnow()
        check_key(key, at)
        _expire_locked(db, key, at)
        if purpose == "safety" and not key.safety_enabled:
            raise ProviderError("safety_not_allowed", 403, usage_unknown=False)
        route = public_route(db, request.model)
        if route.provider not in registry.providers:
            raise ProviderError(
                "model_not_found",
                404,
                param="model",
                usage_unknown=False,
                message="The requested model is not available.",
            )
        if not permitted(db, key, route):
            raise ProviderError(
                "model_not_allowed",
                403,
                param="model",
                usage_unknown=False,
                message="This API key is not allowed to use the requested model.",
            )
        receipt = None
        if request.safety_receipt:
            if not key.safety_enabled:
                raise ProviderError("safety_not_allowed", 403, usage_unknown=False)
            from app.safety.receipts import consume

            receipt = consume(request.safety_receipt, key_id, route.alias, request)
            request = request.model_copy(update={"safety_receipt": None})
        helper = (
            vision_helper(db)
            if (
                route.vision_behavior == "vision_fallback"
                and any(m.images() for m in request.messages)
            )
            else None
        )
        adapter, call = registry.prepare(route, request, helper_route=helper)
        if receipt:
            call.metrics["safety_receipt"] = receipt
        input_bound = input_token_bound(call.request, route)
        output_bound = call.request.output_limit()
        token_bound = 0 if purpose == "safety" else input_bound + output_bound
        day = at.replace(hour=0, minute=0, second=0, microsecond=0)
        month = day.replace(day=1)
        next_month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
        minute = at - timedelta(seconds=60)
        active = Usage.status == "reserved"
        columns = [func.count().filter(active).label("concurrent")]
        for name, start in (("minute", minute), ("day", day), ("month", month)):
            columns.extend(
                (
                    func.count()
                    .filter(
                        Usage.purpose == purpose,
                        Usage.created_at > start if name == "minute" else Usage.created_at >= start,
                    )
                    .label(name + "_requests"),
                    func.coalesce(
                        func.sum(Usage.accounted_tokens).filter(
                            or_(
                                active,
                                Usage.created_at > start
                                if name == "minute"
                                else Usage.created_at >= start,
                            )
                        ),
                        0,
                    ).label(name + "_tokens"),
                )
            )
        totals = (
            db.execute(
                select(*columns).where(
                    Usage.key_id == key_id,
                    or_(Usage.created_at >= min(month, minute), active),
                )
            )
            .one()
            ._mapping
        )
        if (
            key.max_concurrent_requests is not None
            and totals["concurrent"] >= key.max_concurrent_requests
        ):
            limit_error("max_concurrent_requests", "concurrency_limit_exceeded", 1)
        checks = (
            ("rpm", key.rpm, totals["minute_requests"] + 1, "rate_limit_exceeded", 60),
            ("tpm", key.tpm, totals["minute_tokens"] + token_bound, "token_limit_exceeded", 60),
            (
                "requests_per_day",
                key.requests_per_day,
                totals["day_requests"] + 1,
                "rate_limit_exceeded",
                int((day + timedelta(days=1) - at).total_seconds()),
            ),
            (
                "requests_per_month",
                key.requests_per_month,
                totals["month_requests"] + 1,
                "rate_limit_exceeded",
                int((next_month - at).total_seconds()),
            ),
            (
                "tokens_per_day",
                key.tokens_per_day,
                totals["day_tokens"] + token_bound,
                "token_limit_exceeded",
                int((day + timedelta(days=1) - at).total_seconds()),
            ),
            (
                "tokens_per_month",
                key.tokens_per_month,
                totals["month_tokens"] + token_bound,
                "token_limit_exceeded",
                int((next_month - at).total_seconds()),
            ),
        )
        for param, limit, value, code, retry in checks:
            if limit is None:
                continue
            if value > limit:
                limit_error(param, code, retry)
        reserved_cost = (
            Decimal(0)
            if purpose == "safety"
            else cost(input_bound, output_bound, route.input_price, route.output_price)
        )
        if (
            key.budget_usd is not None
            and key.spent_usd + key.reserved_usd + reserved_cost > key.budget_usd
        ):
            limit_error("budget_usd", "budget_exceeded", 60)
        key.reserved_usd += reserved_cost
        key.last_used_at = at
        entry = Usage(
            key_id=key_id,
            purpose=purpose,
            user_id=key.user_id,
            alias=route.alias,
            model_key=route.model_key,
            region=route.region,
            api_route=route.api_route,
            provider=route.provider,
            upstream_model=route.upstream_model,
            created_at=at,
            reserved_tokens=token_bound,
            accounted_tokens=token_bound,
            reserved_cost=reserved_cost,
            input_price=route.input_price,
            output_price=route.output_price,
            lease_expires_at=at + timedelta(seconds=get_settings().reservation_ttl_seconds),
        )
        db.add(entry)
        db.flush()
        return Admission(entry.id, route, adapter, call, key_id)


def mark_dispatched(request_id):
    at = utcnow()
    with session_factory().begin() as db:
        result = db.execute(
            update(Usage)
            .where(
                Usage.id == request_id,
                Usage.status == "reserved",
                Usage.lease_expires_at > at,
            )
            .values(dispatched_at=at)
        )
        if result.rowcount != 1:
            raise ProviderError(
                "provider_unavailable",
                503,
                usage_unknown=False,
                message="The request reservation has expired.",
            )


def renew(request_id):
    at = utcnow()
    with session_factory().begin() as db:
        result = db.execute(
            update(Usage)
            .where(
                Usage.id == request_id,
                Usage.status == "reserved",
                Usage.lease_expires_at > at,
            )
            .values(lease_expires_at=at + timedelta(seconds=get_settings().reservation_ttl_seconds))
        )
        return result.rowcount == 1


def settle(
    request_id,
    usage,
    latency_ms,
    *,
    status=None,
    error_code=None,
    usage_unknown=True,
    execution_metadata=None,
):
    with session_factory().begin() as db:
        key_id = db.scalar(select(Usage.key_id).where(Usage.id == request_id))
        if key_id is None:
            return False
        key = db.scalar(select(ApiKey).where(ApiKey.id == key_id).with_for_update())
        entry = db.scalar(select(Usage).where(Usage.id == request_id).with_for_update())
        result = _finalize_locked(
            key,
            entry,
            usage,
            latency_ms,
            status=status or ("completed" if usage is not None else "failed"),
            error_code=error_code,
            usage_unknown=usage_unknown,
        )
        if result and execution_metadata is not None:
            entry.execution_metadata = execution_metadata
        return result


def reap_expired():
    at = utcnow()
    with session_factory()() as db:
        ids = db.scalars(
            select(Usage.key_id)
            .where(
                Usage.status == "reserved",
                or_(Usage.lease_expires_at <= at, Usage.lease_expires_at.is_(None)),
            )
            .distinct()
            .limit(100)
        ).all()
    count = 0
    for key_id in ids:
        with session_factory().begin() as db:
            key = db.scalar(
                select(ApiKey).where(ApiKey.id == key_id).with_for_update(skip_locked=True)
            )
            if key is not None:
                count += _expire_locked(db, key, at)
    return count


def release_estimate(request_id):
    with session_factory().begin() as db:
        key_id = db.scalar(select(Usage.key_id).where(Usage.id == request_id))
        if key_id is None:
            raise ProviderError("request_not_found", 404)
        key = db.scalar(select(ApiKey).where(ApiKey.id == key_id).with_for_update())
        _expire_locked(db, key, utcnow())
        entry = db.scalar(select(Usage).where(Usage.id == request_id).with_for_update())
        if not entry.usage_estimated or entry.status == "released" or entry.status == "reserved":
            raise ProviderError("reservation_not_releasable", 409)
        key.spent_usd -= entry.cost_usd
        entry.cost_usd = Decimal(0)
        entry.accounted_tokens = 0
        entry.status = "released"
        return True


def visible_models(key_id):
    with session_factory()() as db:
        key = db.get(ApiKey, key_id)
        check_key(key, utcnow())
        return [route.alias for route in accessible_models(db, key, registry.providers)]
