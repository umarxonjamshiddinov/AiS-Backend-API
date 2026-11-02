import secrets
import threading
from datetime import UTC, datetime

from dotenv import set_key
from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import delete, func, select

from app.api.dependencies import get_inference_service
from app.core.config import PROJECT_ROOT, get_settings
from app.core.public_url import deployment_api_base_url, public_api_base_url
from app.db.models import ApiKey, KeyModel, ModelRoute, Usage, User
from app.db.session import session_factory
from app.gateway.capabilities import ModelCapabilities
from app.gateway.providers import registry
from app.gateway.quota import release_estimate
from app.gateway.schemas import CredentialInput, KeyInput, ModelInput, UserInput, UserUpdate
from app.providers.base import ProviderError
from app.providers.mantle import base_url_for
from app.services.accounts import check_models, get_user, serialize_user
from app.services.accounts import create_key as issue_key

credential_lock = threading.Lock()


def require_admin(authorization: str | None = Header(default=None)):
    token = get_settings().admin_token
    if not token or not token.get_secret_value():
        raise ProviderError("admin_not_configured", 503)
    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(
        supplied.encode(), token.get_secret_value().encode()
    ):
        raise ProviderError("invalid_admin_token", 401)


router = APIRouter(prefix="/admin/api", dependencies=[Depends(require_admin)], tags=["admin"])
page_router = APIRouter(include_in_schema=False)


@page_router.get("/admin/")
@page_router.get("/admin")
def admin_page(request: Request):
    return HTMLResponse(
        request.app.state.admin_assets.html,
        headers={
            "Cache-Control": "no-store",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; "
            "style-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'",
        },
    )


@page_router.get("/admin/assets/{digest}/{name}")
def admin_asset(
    request: Request, digest: str, name: str, if_none_match: str | None = Header(default=None)
):
    return request.app.state.admin_assets.response(digest, name, if_none_match)


def serialize_model(model):
    data = {
        field: getattr(model, field)
        for field in (
            "alias",
            "provider",
            "upstream_model",
            "input_price",
            "output_price",
            "max_output_tokens",
            "max_context_tokens",
            "enabled",
            "external_api_allowed",
            "provider_transport",
            "model_key",
            "display_name",
            "manufacturer",
            "region",
            "api_route",
            "model_type",
            "is_public",
            "supports_vision",
            "supports_reasoning",
            "reasoning_efforts",
            "vision_behavior",
            "catalog_metadata",
            "created_at",
            "updated_at",
        )
    }
    data["capabilities"] = ModelCapabilities.model_validate(model.capabilities).model_dump()
    return data


def serialize_key(db, key, active_requests=0):
    data = {
        field: getattr(key, field)
        for field in (
            "id",
            "user_id",
            "name",
            "prefix",
            "active",
            "budget_usd",
            "spent_usd",
            "reserved_usd",
            "rpm",
            "tpm",
            "created_at",
            "expires_at",
            "revoked_at",
            "requests_per_day",
            "requests_per_month",
            "tokens_per_day",
            "tokens_per_month",
            "max_concurrent_requests",
            "last_used_at",
            "all_public_models",
            "safety_enabled",
        )
    }
    data["models"] = db.scalars(
        select(KeyModel.alias).where(KeyModel.key_id == key.id).order_by(KeyModel.alias)
    ).all()
    data["active_requests"] = active_requests
    return data


@router.get("/overview")
def overview(request: Request):
    at = datetime.now(UTC)
    day = at.replace(hour=0, minute=0, second=0, microsecond=0)
    details = {
        name: {
            "configured": adapter.configured(),
            "transport": adapter.transport,
            "supported_capabilities": adapter.supported_capabilities.model_dump(),
        }
        for name, adapter in registry.providers.items()
    }
    with session_factory()() as db:
        active = dict(
            db.execute(
                select(Usage.key_id, func.count())
                .where(
                    Usage.status == "reserved",
                    Usage.lease_expires_at > at,
                )
                .group_by(Usage.key_id)
            ).all()
        )
        stats = (
            db.execute(
                select(
                    func.count().label("requests_today"),
                    func.coalesce(func.sum(Usage.accounted_tokens), 0).label("tokens_today"),
                    func.count().filter(Usage.error_code.is_not(None)).label("errors_today"),
                ).where(Usage.created_at >= day)
            )
            .one()
            ._mapping
        )
        return {
            "public_api_base_url": public_api_base_url(request),
            "deployment_api_base_url": deployment_api_base_url(request),
            "providers": {name: data["configured"] for name, data in details.items()},
            "provider_details": details,
            "capability_defaults": ModelCapabilities().model_dump(),
            "users": [serialize_user(u) for u in db.scalars(select(User).order_by(User.username))],
            "stats": {
                **dict(stats),
                "active_requests": sum(active.values()),
                "period_timezone": "UTC",
            },
            "models": [
                serialize_model(m)
                for m in db.scalars(
                    select(ModelRoute)
                    .where(ModelRoute.provider.in_(registry.providers))
                    .order_by(ModelRoute.alias)
                )
            ],
            "keys": [
                serialize_key(db, k, active.get(k.id, 0))
                for k in db.scalars(select(ApiKey).order_by(ApiKey.created_at.desc()))
            ],
            "usage": [
                {
                    field: getattr(u, field)
                    for field in (
                        "id",
                        "key_id",
                        "user_id",
                        "model_key",
                        "region",
                        "api_route",
                        "purpose",
                        "execution_metadata",
                        "alias",
                        "provider",
                        "upstream_model",
                        "status",
                        "created_at",
                        "finished_at",
                        "input_tokens",
                        "output_tokens",
                        "accounted_tokens",
                        "reserved_tokens",
                        "cost_usd",
                        "reserved_cost",
                        "latency_ms",
                        "error_code",
                        "usage_estimated",
                        "lease_expires_at",
                        "dispatched_at",
                    )
                }
                for u in db.scalars(select(Usage).order_by(Usage.created_at.desc()).limit(100))
            ],
        }


@router.put("/models")
def put_model(payload: ModelInput):
    with session_factory().begin() as db:
        model = db.get(ModelRoute, payload.alias)
        existing = model is not None
        provider = (
            payload.provider
            if not existing or "provider" in payload.model_fields_set
            else model.provider
        )
        adapter = registry.get(provider)
        transport = payload.provider_transport or (
            model.provider_transport
            if existing and model.provider == provider
            else adapter.transport
        )
        caps = payload.capabilities or (
            ModelCapabilities.model_validate(model.capabilities)
            if existing
            else ModelCapabilities(system_prompt=True, temperature=False)
            if provider == "amazon_bedrock"
            else ModelCapabilities()
        )
        context = payload.max_context_tokens
        if existing and "max_context_tokens" not in payload.model_fields_set:
            context = model.max_context_tokens
        registry.validate_model(provider, transport, caps, context)
        values = payload.model_dump(
            exclude={"capabilities", "provider_transport"}, exclude_unset=existing
        )
        values.update(provider_transport=transport, capabilities=caps.model_dump())
        if existing and model.model_key is not None:
            if payload.upstream_model != model.upstream_model or (
                "model_key" in payload.model_fields_set and payload.model_key != model.model_key
            ):
                raise ProviderError(
                    "invalid_request",
                    400,
                    param="model_key",
                    message="Catalog identities are immutable; create another model for a new ID.",
                )
        if model is None:
            model = ModelRoute(**values)
            db.add(model)
        else:
            for field, value in values.items():
                setattr(model, field, value)
        if model.model_type == "safeguard" or model.upstream_model.startswith(
            "openai.gpt-oss-safeguard-"
        ):
            model.is_public = False
            model.external_api_allowed = False
            model.vision_behavior = "none"
        if provider == "amazon_bedrock":
            if not model.max_context_tokens or not model.region or not model.api_route:
                raise ProviderError(
                    "invalid_request",
                    400,
                    param="region",
                    message="Mantle routes require region, api_route and context limit.",
                )
            base_url_for(model.region, model.api_route)
            if model.supports_vision != caps.image_input:
                raise ProviderError(
                    "invalid_request",
                    400,
                    param="supports_vision",
                    message="Native vision metadata must match image capabilities.",
                )
            if (model.vision_behavior == "native_vision") != model.supports_vision:
                raise ProviderError(
                    "invalid_request",
                    400,
                    param="vision_behavior",
                    message="Native vision requires the native_vision route.",
                )
        elif model.vision_behavior == "vision_fallback":
            raise ProviderError(
                "unsupported_capability",
                400,
                param="vision_behavior",
                message="Qwen image fallback requires the Mantle transport.",
            )
        if (
            model.max_context_tokens
            and model.max_output_tokens
            and model.max_output_tokens > model.max_context_tokens
        ):
            raise ProviderError(
                "invalid_request",
                400,
                param="max_context_tokens",
                message="Context limit cannot be smaller than output limit.",
            )
    return {"status": "saved"}


@router.get("/users")
def users():
    with session_factory()() as db:
        return {
            "data": [serialize_user(u) for u in db.scalars(select(User).order_by(User.username))]
        }


@router.post("/users")
def create_user(payload: UserInput):
    with session_factory().begin() as db:
        if db.scalar(select(User.id).where(User.username == payload.username)):
            raise ProviderError("user_already_exists", 409)
        user = User(**payload.model_dump())
        db.add(user)
        db.flush()
        return serialize_user(user)


@router.put("/users/{user_id}")
def edit_user(user_id: str, payload: UserUpdate):
    with session_factory().begin() as db:
        user = get_user(db, user_id)
        user.active = payload.active
        return serialize_user(user)


@router.get("/users/{user_id}/keys")
def user_keys(user_id: str):
    with session_factory()() as db:
        user = get_user(db, user_id)
        return {
            "data": [
                serialize_key(db, key)
                for key in db.scalars(
                    select(ApiKey)
                    .where(ApiKey.user_id == user.id)
                    .order_by(ApiKey.created_at.desc())
                )
            ]
        }


@router.post("/keys")
def create_key(payload: KeyInput):
    with session_factory().begin() as db:
        return issue_key(db, payload)


@router.put("/keys/{key_id}")
def edit_key(key_id: str, payload: KeyInput):
    with session_factory().begin() as db:
        key = db.scalar(select(ApiKey).where(ApiKey.id == key_id).with_for_update())
        if key is None:
            raise ProviderError("key_not_found", 404)
        values = payload.model_dump(exclude={"models"}, exclude_unset=True)
        owner = values.pop("user_id", None)
        if owner is not None and get_user(db, owner).id != key.user_id:
            raise ProviderError(
                "invalid_request",
                400,
                param="user_id",
                message="Create a new key to assign another owner.",
            )
        if key.revoked_at is not None and values.get("active"):
            raise ProviderError(
                "revoked_api_key", 409, message="Create a new key to replace a revoked key."
            )
        if (
            values.get("budget_usd") is not None
            and values["budget_usd"] < key.spent_usd + key.reserved_usd
        ):
            raise ProviderError("budget_below_committed_usage", 400)
        for field, value in values.items():
            setattr(key, field, value)
        if "models" in payload.model_fields_set:
            check_models(db, payload.models)
            db.execute(delete(KeyModel).where(KeyModel.key_id == key_id))
            for alias in payload.models:
                db.add(KeyModel(key_id=key_id, alias=alias))
    return {"status": "saved"}


@router.post("/keys/{key_id}/revoke")
def revoke(key_id: str):
    with session_factory().begin() as db:
        key = db.scalar(select(ApiKey).where(ApiKey.id == key_id).with_for_update())
        if key is None:
            raise ProviderError("key_not_found", 404)
        key.active = False
        key.revoked_at = key.revoked_at or datetime.now(UTC)
    return {"status": "revoked"}


@router.post("/usage/{request_id}/release")
def release(request_id: str):
    release_estimate(request_id)
    return {"status": "released"}


@router.put("/credentials")
def credentials(payload: CredentialInput):
    value = payload.api_key.get_secret_value()
    if any(c in value for c in "\r\n\x00"):
        raise ProviderError("invalid_credential", 400)
    variable = registry.get(payload.provider).credential_variable
    import os

    with credential_lock:
        set_key(str(PROJECT_ROOT / ".env"), variable, value)
        os.environ[variable] = value
        get_settings.cache_clear()
        get_inference_service.cache_clear()
    return {"status": "saved"}
