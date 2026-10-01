"""Database-backed catalog resolution, visibility and centralized output limits."""

from sqlalchemy import or_, select

from app.core.config import get_settings
from app.db.models import KeyModel, ModelRoute
from app.providers.base import ProviderError


def resolve_model(db, identifier):
    route = db.get(ModelRoute, identifier)
    if route is None:
        route = db.scalar(select(ModelRoute).where(ModelRoute.model_key == identifier))
    if route is None:
        raise ProviderError("model_not_found", 404, param="model", usage_unknown=False)
    return route


def public_route(db, identifier):
    route = resolve_model(db, identifier)
    # Internal status is independent from the external enable switch or a key grant.
    protected = db.scalar(
        select(ModelRoute.alias)
        .where(
            ModelRoute.upstream_model == route.upstream_model,
            ModelRoute.provider == route.provider,
            ModelRoute.is_public.is_(False),
        )
        .limit(1)
    )
    if not route.is_public or route.model_type == "safeguard" or protected:
        raise ProviderError("model_not_allowed", 403, param="model", usage_unknown=False)
    if not route.enabled or not route.external_api_allowed:
        raise ProviderError("model_not_found", 404, param="model", usage_unknown=False)
    return route


def permitted(db, key, route):
    return (key.all_public_models and route.model_key is not None) or db.get(
        KeyModel, (key.id, route.alias)
    ) is not None


def accessible_models(db, key, providers):
    explicit = (
        select(KeyModel.key_id)
        .where(KeyModel.key_id == key.id, KeyModel.alias == ModelRoute.alias)
        .exists()
    )
    allowed = (
        or_(explicit, ModelRoute.model_key.is_not(None)) if key.all_public_models else explicit
    )
    return db.scalars(
        select(ModelRoute)
        .where(
            ModelRoute.enabled.is_(True),
            ModelRoute.external_api_allowed.is_(True),
            ModelRoute.is_public.is_(True),
            ModelRoute.provider.in_(providers),
            allowed,
        )
        .order_by(ModelRoute.alias)
    ).all()


def output_limit(route, request, *, internal=False):
    if route.provider == "amazon_bedrock":
        default = get_settings().ai_default_max_output_tokens
        maximum = min(
            default,
            route.max_output_tokens or default,
            max(256, route.max_context_tokens // 4) if route.max_context_tokens else default,
        )
        if internal:
            maximum = min(route.max_output_tokens or default, route.max_context_tokens or default)
    else:
        maximum = route.max_output_tokens or 4096
        default = min(1024, maximum)
    value = request.output_limit(min(default, maximum))
    if value > maximum:
        param = "max_tokens" if request.max_tokens is not None else "max_completion_tokens"
        raise ProviderError(
            "invalid_request",
            400,
            param=param,
            usage_unknown=False,
            message=f"The selected model's output limit is {maximum} tokens.",
        )
    return value


def vision_helper(db):
    route = resolve_model(db, get_settings().vision_helper_model_key)
    if not (
        route.enabled
        and route.supports_vision
        and route.provider == "amazon_bedrock"
        and route.vision_behavior == "native_vision"
    ):
        raise ProviderError(
            "provider_unavailable",
            503,
            usage_unknown=False,
            message="The vision helper is unavailable.",
        )
    return route
