"""Shared account/key operations for the existing admin API and local CLI."""

import hashlib
import secrets

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.db.models import ApiKey, KeyModel, ModelRoute, User
from app.providers.base import ProviderError

LEGACY_OWNER = "00000000-0000-4000-8000-000000000001"


def default_owner(db):
    db.execute(
        insert(User)
        .values(id=LEGACY_OWNER, username="legacy-owner", active=True)
        .on_conflict_do_nothing()
    )
    return db.scalar(select(User).where(User.username == "legacy-owner"))


def get_user(db, identifier):
    user = db.get(User, identifier)
    if user is None:
        user = db.scalar(select(User).where(User.username == identifier))
    if user is None:
        raise ProviderError("user_not_found", 404, usage_unknown=False)
    return user


def serialize_user(user):
    return {field: getattr(user, field) for field in ("id", "username", "active", "created_at")}


def check_models(db, aliases):
    if len(set(aliases)) != len(aliases) or any(db.get(ModelRoute, a) is None for a in aliases):
        raise ProviderError("invalid_model_permissions", 400, usage_unknown=False)


def create_key(db, payload):
    user = get_user(db, payload.user_id) if payload.user_id else default_owner(db)
    if not user.active:
        raise ProviderError("inactive_user", 403, usage_unknown=False)
    check_models(db, payload.models)
    token = "sk-myais-" + secrets.token_urlsafe(32)
    values = payload.model_dump(exclude={"models", "user_id"})
    key = ApiKey(
        **values,
        user_id=user.id,
        prefix=token[:18],
        key_hash=hashlib.sha256(token.encode()).hexdigest(),
    )
    db.add(key)
    db.flush()
    for alias in payload.models:
        db.add(KeyModel(key_id=key.id, alias=alias))
    return {"id": key.id, "user_id": user.id, "key": token}
