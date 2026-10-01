"""Server-owned classification, strict contracts and safe structured output only."""

import json
from dataclasses import asdict

from app.gateway.schemas import ImagePart, ImageURL, Message, TextPart
from app.providers.base import ProviderError
from app.providers.images import decode_image
from app.safety.contracts import (
    ASSESSMENT_PROMPT,
    FORMAT_RETRY_INSTRUCTION,
    POLICY_PROMPT,
    parse_assessment,
    parse_v1,
)
from app.safety.policy import decide
from app.services.internal_inference import complete_safety


def assess(context, *, policy_version="ais_safety_v1", image=None, factory=None):
    values = context.model_dump()
    turns = values.pop("recent_conversation")
    refusals = values.pop("previous_refused_requests")
    truncated = values.pop("history_truncated")
    if policy_version == "ais_safety_v1":
        # Exactly the current Cloud Bot v1 context semantics.
        payload = {"conversation_context": turns, "current_request": values["user_request"]}
    else:
        payload = {
            "current_request": values,
            "recent_conversation": turns,
            "prior_user_request": next(
                (t["content"] for t in reversed(turns) if t["role"] == "user"), None
            ),
            "previous_refused_requests": refusals,
            "history_truncated": truncated,
        }
    text = json.dumps(payload, ensure_ascii=False)
    if len(text.encode()) > 112000:
        raise ProviderError("invalid_request", 400, usage_unknown=False)
    parts = [TextPart(type="text", text=text)]
    if image is not None:
        decode_image(image)
        parts.append(ImagePart(type="image_url", image_url=ImageURL(url=image)))
    adaptive = policy_version == "ais_safety_v2"
    prompt = ASSESSMENT_PROMPT if adaptive else POLICY_PROMPT
    telemetry = []
    for attempt in range(2 if adaptive else 1):
        messages = [
            Message(
                role="system",
                content=prompt + ("\n\n" + FORMAT_RETRY_INSTRUCTION if attempt else ""),
            ),
            Message(role="user", content=parts),
        ]
        try:
            response = complete_safety(messages, adaptive=adaptive, factory=factory)
        except ProviderError as error:
            if error.code == "content_filter":
                return {
                    "policy_version": policy_version,
                    "action": "refuse",
                    "reason_code": "provider_guardrail_without_intent",
                    "assessment": None,
                    "decision": None,
                    "category": None,
                    "safe_mode": None,
                    "contract_retries": attempt,
                    "usage": None,
                    "internal_requests": telemetry,
                }
            raise ProviderError("safety_unavailable", 503, usage_unknown=False) from None
        telemetry.append(response["id"])
        try:
            if adaptive:
                facts = parse_assessment(response["text"])
                decision = decide(facts)
                data = {
                    "assessment": asdict(facts),
                    "action": decision.action.value,
                    "reason_code": decision.reason_code,
                    "safe_mode": decision.safe_mode,
                    "category": facts.category,
                    "decision": None,
                }
            else:
                verdict = parse_v1(response["text"])
                blocked = verdict["decision"] == "blocked"
                data = {
                    "assessment": None,
                    "action": "violation" if blocked else "allow",
                    "reason_code": "policy_violation" if blocked else "policy_allowed",
                    "safe_mode": None,
                    "category": verdict["category"],
                    "decision": verdict,
                }
        except (ValueError, TypeError, KeyError):
            if adaptive and attempt == 0:
                continue
            raise ProviderError("safety_unavailable", 503, usage_unknown=False) from None
        return {
            **data,
            "policy_version": policy_version,
            "contract_retries": attempt,
            "usage": response["usage"],
            "internal_requests": telemetry,
        }
    raise ProviderError("safety_unavailable", 503, usage_unknown=False)


def chat_context(request):
    from app.safety.schemas import SafetyContext, SafetyTurn

    users = [m for m in request.messages if m.role == "user"]
    if not users:
        raise ProviderError("invalid_request", 400, usage_unknown=False)
    last = users[-1]
    text = (
        last.content
        if isinstance(last.content, str)
        else "\n".join(p.text for p in (last.content or []) if isinstance(p, TextPart))
    )
    image = last.images()[0].image_url.url if last.images() else None
    # All images are checked individually by the caller, never silently bypassed.
    turns, size = [], 0
    for message in reversed(request.messages):
        if message is last:
            continue
        if message.role not in {"user", "assistant"} or not isinstance(message.content, str):
            continue
        if size + len(message.content.encode()) > 12000 or len(turns) == 8:
            break
        turns.append(SafetyTurn(role=message.role, content=message.content))
        size += len(message.content.encode())
    return SafetyContext(
        user_request=text or "Analyze the attached image.",
        kind="image" if image else "text",
        recent_conversation=list(reversed(turns)),
    )


def enforce_chat(admission):
    from app.db.models import ApiKey
    from app.gateway.quota import check_key, session_factory, utcnow

    with session_factory()() as db:
        key = db.get(ApiKey, admission.key_id)
        check_key(key, utcnow())
        if not key.safety_enabled:
            return
    receipt = admission.call.metrics.pop("safety_receipt", None)
    if receipt:
        from app.gateway.schemas import Message
        from app.safety.policy import SAFE_INSTRUCTIONS

        mode = receipt["safe_mode"]
        if mode:
            messages = list(admission.call.request.messages)
            messages.insert(0, Message(role="system", content=SAFE_INSTRUCTIONS[mode]))
            object.__setattr__(
                admission.call,
                "request",
                admission.call.request.model_copy(update={"messages": messages}),
            )
        admission.call.metrics["safety"] = {
            **receipt,
            "action": "safe_complete" if mode else "allow",
        }
        return
    context = chat_context(admission.call.request)
    images = [i.image_url.url for m in admission.call.request.messages for i in m.images()]
    checks = []
    # Text Safety first; every image Safety precedes native vision/fallback.
    for image in [None, *images]:
        result = assess(context, image=image)
        checks.extend(result["internal_requests"])
        admission.call.metrics["safety"] = {
            "policy_version": "ais_safety_v1",
            "action": result["action"],
            "internal_requests": checks,
        }
        if result["action"] in {"refuse", "violation"}:
            raise ProviderError("content_filter", 400, usage_unknown=False)
