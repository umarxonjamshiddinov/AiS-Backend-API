"""Local management CLI; requires server/DB access, not an HTTP admin token."""

import argparse
import json
import sys
from datetime import UTC, datetime

from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import PROJECT_ROOT
from app.db.models import ApiKey, ModelRoute, User
from app.db.session import session_factory
from app.gateway.schemas import KeyInput, UserInput
from app.providers.base import ProviderError
from app.services.accounts import create_key, get_user, serialize_user


def optional_rate_limit(value):
    if value.lower() == "unlimited":
        return None
    try:
        result = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Use a positive integer or unlimited") from None
    if result < 1:
        raise argparse.ArgumentTypeError("Use a positive integer or unlimited")
    return result


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db", help="Apply migrations and insert missing catalog rows")
    sub.add_parser("sync-models", help="Insert missing catalog rows without overwriting DB edits")
    sub.add_parser("models", help="Show catalog counts and model IDs")
    user = sub.add_parser("create-user")
    user.add_argument("--name", required=True, help="Unique username; existing users are reused")
    status = sub.add_parser("set-user-active")
    status.add_argument("--user", required=True)
    status.add_argument("--active", choices=["true", "false"], required=True)
    key = sub.add_parser("create-key")
    key.add_argument("--user", required=True, help="User UUID or username")
    key.add_argument("--name", required=True)
    key.add_argument("--model", action="append", help="Optional allowlist; default: public catalog")
    key.add_argument(
        "--rpm",
        type=optional_rate_limit,
        default=30,
        help="Requests per minute, or unlimited (default: 30)",
    )
    key.add_argument(
        "--tpm",
        type=optional_rate_limit,
        default=2000000,
        help="Tokens per minute, or unlimited (default: 2000000)",
    )
    keys = sub.add_parser("list-keys")
    keys.add_argument("--user", required=True)
    revoke = sub.add_parser("revoke-key")
    revoke.add_argument("--id", required=True)
    return root


def execute(options):
    if options.command in ("init-db", "sync-models"):
        from scripts.seed_models import seed_models

        if options.command == "init-db":
            config = Config(str(PROJECT_ROOT / "alembic.ini"))
            config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
            command.upgrade(config, "head")
        return seed_models()
    with session_factory().begin() as db:
        if options.command == "create-user":
            payload = UserInput(username=options.name)
            user = db.scalar(select(User).where(User.username == payload.username))
            if user is None:
                user = User(username=payload.username, active=True)
                db.add(user)
                db.flush()
            return serialize_user(user)
        if options.command == "set-user-active":
            user = get_user(db, options.user)
            user.active = options.active == "true"
            return serialize_user(user)
        if options.command == "create-key":
            user = get_user(db, options.user)
            return create_key(
                db,
                KeyInput(
                    name=options.name,
                    user_id=user.id,
                    models=options.model or [],
                    all_public_models=not options.model,
                    rpm=options.rpm,
                    tpm=options.tpm,
                ),
            )
        if options.command == "list-keys":
            from app.admin.routes import serialize_key

            user = get_user(db, options.user)
            return {
                "data": [
                    serialize_key(db, k)
                    for k in db.scalars(
                        select(ApiKey)
                        .where(ApiKey.user_id == user.id)
                        .order_by(ApiKey.created_at.desc())
                    )
                ]
            }
        if options.command == "revoke-key":
            key = db.get(ApiKey, options.id)
            if key is None:
                raise ProviderError("key_not_found", 404)
            key.active = False
            key.revoked_at = key.revoked_at or datetime.now(UTC)
            return {"id": key.id, "status": "revoked"}
        if options.command == "models":
            rows = db.scalars(select(ModelRoute).order_by(ModelRoute.alias)).all()
            catalog = [r for r in rows if r.model_key is not None]
            return {
                "total": len(catalog),
                "public": sum(r.is_public for r in catalog),
                "internal": sum(not r.is_public for r in catalog),
                "legacy_routes": len(rows) - len(catalog),
                "data": [
                    {
                        "id": r.alias,
                        "model_key": r.model_key,
                        "public": r.is_public,
                        "enabled": r.enabled,
                        "region": r.region,
                        "route": r.api_route,
                    }
                    for r in catalog
                ],
            }


def main(argv=None):
    options = parser().parse_args(argv)
    try:
        print(json.dumps(execute(options), default=str, ensure_ascii=False))
        return 0
    except ProviderError as exc:
        print(json.dumps(exc.as_dict()), file=sys.stderr)
    except SQLAlchemyError:
        print("Database operation failed; check DATABASE_URL and migrations.", file=sys.stderr)
    except Exception:
        print("Management operation failed; check arguments and configuration.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
