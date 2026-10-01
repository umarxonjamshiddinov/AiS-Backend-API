"""Bounded one-use receipts: no arbitrary bypass, no durable request content."""

import hashlib
import json
import secrets
import time
from collections import OrderedDict
from threading import Lock

from app.providers.base import ProviderError

_receipts = OrderedDict()
_lock = Lock()


def image_digest(value):
    return hashlib.sha256(value.encode()).hexdigest() if value else None


def issue(key_id, selected, context, image, result):
    if result["action"] not in {"allow", "safe_complete"}:
        return None
    token = secrets.token_urlsafe(32)
    with _lock:
        while len(_receipts) >= 128:
            _receipts.popitem(last=False)
        _receipts[token] = (
            time.monotonic() + 120,
            key_id,
            selected,
            image_digest(context.user_request),
            bool(context.untrusted_material),
            image_digest(image),
            result["safe_mode"],
            result["policy_version"],
            image_digest(
                json.dumps([t.model_dump() for t in context.recent_conversation], sort_keys=True)
            ),
        )
    return token


def consume(token, key_id, selected, request):
    with _lock:
        entry = _receipts.pop(token, None)
    if entry is None or entry[0] < time.monotonic() or entry[1:3] != (key_id, selected):
        raise ProviderError("invalid_safety_receipt", 400, usage_unknown=False)
    from app.gateway.schemas import TextPart

    user = next((m for m in reversed(request.messages) if m.role == "user"), None)
    if user is None:
        raise ProviderError("invalid_safety_receipt", 400, usage_unknown=False)
    text = (
        user.content
        if isinstance(user.content, str)
        else "\n".join(p.text for p in (user.content or []) if isinstance(p, TextPart))
    )
    images = [i.image_url.url for m in request.messages for i in m.images()]
    # Only the exact assessed request/material can authorize its completion;
    # additional web/catalog context remains subject to product system instructions.
    if (
        image_digest(text) != entry[3]
        or bool(entry[4])
        or len(images) != int(entry[5] is not None)
        or (images and image_digest(images[0]) != entry[5])
    ):
        return None  # Changed/transformed input must be checked again server-side.
    turns = [
        {"role": m.role, "content": m.content}
        for m in request.messages
        if m is not user and m.role in {"user", "assistant"} and isinstance(m.content, str)
    ][-8:]
    if image_digest(json.dumps(turns, sort_keys=True)) != entry[8]:
        return None
    return {"safe_mode": entry[6], "policy_version": entry[7]}
