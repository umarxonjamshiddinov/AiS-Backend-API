"""Adapter registry and model routing; no provider branches in the public API."""

from dataclasses import replace

from pydantic import ValidationError

from app.gateway.capabilities import ModelCapabilities, validate_request
from app.providers.base import ChatInput, ProviderError
from app.providers.bedrock_chat import BedrockAdapter, bedrock_payload
from app.providers.mantle import MantleAdapter, helper_request
from app.services.models import output_limit

# Compatibility imports for existing integrations.
BedrockChatProvider = BedrockAdapter
__all__ = [
    "BedrockAdapter",
    "BedrockChatProvider",
    "bedrock_payload",
    "registry",
]


class ProviderRegistry:
    def __init__(self):
        self.providers = {"bedrock": BedrockAdapter(), "amazon_bedrock": MantleAdapter()}

    def get(self, name):
        provider = self.providers.get(name)
        if provider is None:
            raise ProviderError(
                "provider_unavailable",
                503,
                usage_unknown=False,
                message="The model's provider is unavailable.",
            )
        return provider

    def validate_model(self, provider, transport, capabilities, max_context_tokens=None):
        adapter = self.get(provider)
        if transport != adapter.transport:
            raise ProviderError(
                "unsupported_capability",
                400,
                param="provider_transport",
                message="The adapter does not implement this transport.",
                usage_unknown=False,
            )
        ceiling = adapter.supported_capabilities.model_dump()
        for field, value in capabilities.model_dump().items():
            supported = ceiling[field]
            if (isinstance(value, bool) and value and not supported) or (
                isinstance(value, list) and not set(value).issubset(supported)
            ):
                raise ProviderError(
                    "unsupported_capability",
                    400,
                    param="capabilities." + field,
                    message="This capability is not implemented by the adapter.",
                    usage_unknown=False,
                )
        if capabilities.image_input and not max_context_tokens:
            raise ProviderError(
                "invalid_request",
                400,
                param="max_context_tokens",
                message="Vision routes require a confirmed context token limit.",
                usage_unknown=False,
            )
        return adapter

    def prepare(self, route, request, *, helper_route=None, internal=False):
        try:
            caps = ModelCapabilities.model_validate(route.capabilities)
        except ValidationError:
            raise ProviderError(
                "provider_unavailable",
                503,
                usage_unknown=False,
                message="The model route has invalid capability metadata.",
            ) from None
        adapter = self.validate_model(
            route.provider, route.provider_transport, caps, route.max_context_tokens
        )
        fallback = route.provider == "amazon_bedrock" and route.vision_behavior == "vision_fallback"
        validate_request(request, caps, vision_fallback=fallback)
        output = output_limit(route, request, internal=internal)
        normalized = request.model_copy(
            update={"max_tokens": None, "max_completion_tokens": output}
        )
        call = ChatInput(
            normalized,
            route.upstream_model,
            caps,
            route.provider_transport,
            region=route.region,
            api_route=route.api_route,
            model_key=route.model_key,
            vision_behavior=route.vision_behavior,
            max_context_tokens=route.max_context_tokens,
        )
        if fallback and any(m.images() for m in normalized.messages):
            if helper_route is None:
                raise ProviderError(
                    "provider_unavailable",
                    503,
                    usage_unknown=False,
                    message="The vision helper is unavailable.",
                )
            _, helper_call = self.prepare(
                helper_route, helper_request(normalized, helper_route.alias), internal=True
            )
            call = replace(call, vision_helper=helper_call)
        adapter.validate(call)
        if not adapter.configured():
            raise ProviderError(
                "provider_unavailable",
                503,
                usage_unknown=False,
                message="The model's provider is not configured.",
            )
        return adapter, call


registry = ProviderRegistry()
