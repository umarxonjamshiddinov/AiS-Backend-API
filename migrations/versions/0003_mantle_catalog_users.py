"""Users, owned API keys, Mantle routing and the complete Cloud Bot v7 catalog.

The versioned JSON is an immutable bootstrap artifact, not a runtime registry.
Existing aliases, credentials, permissions and request history are preserved.
"""

import json
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

LEGACY_OWNER = "00000000-0000-4000-8000-000000000001"


def upgrade():
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("username", sa.String(100), nullable=False, unique=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.execute(
        sa.text("INSERT INTO users (id, username) VALUES (:id, 'legacy-owner')").bindparams(
            id=LEGACY_OWNER
        )
    )
    op.add_column("api_keys", sa.Column("user_id", sa.String(36), server_default=LEGACY_OWNER))
    op.execute(sa.text("UPDATE api_keys SET user_id=:id").bindparams(id=LEGACY_OWNER))
    op.alter_column("api_keys", "user_id", nullable=False)
    op.create_foreign_key("fk_api_keys_user_id", "api_keys", "users", ["user_id"], ["id"])
    op.create_index("ix_api_keys_user_id", "api_keys", ["user_id"])
    op.add_column("api_keys", sa.Column("last_used_at", sa.DateTime(timezone=True)))
    op.add_column(
        "api_keys",
        sa.Column("all_public_models", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    for column in (
        sa.Column("model_key", sa.String(100)),
        sa.Column("display_name", sa.String(200)),
        sa.Column("manufacturer", sa.String(100)),
        sa.Column("region", sa.String(40)),
        sa.Column("api_route", sa.String(40)),
        sa.Column("model_type", sa.String(40)),
        sa.Column("is_public", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("supports_vision", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("supports_reasoning", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "reasoning_efforts",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("vision_behavior", sa.String(30), nullable=False, server_default="none"),
        sa.Column(
            "catalog_metadata",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    ):
        op.add_column("model_routes", column)
    op.alter_column("model_routes", "max_output_tokens", existing_type=sa.Integer(), nullable=True)
    op.create_unique_constraint("uq_model_routes_model_key", "model_routes", ["model_key"])
    op.create_index(
        "uq_catalog_model_id",
        "model_routes",
        ["upstream_model"],
        unique=True,
        postgresql_where=sa.text("model_key IS NOT NULL"),
    )
    for column in (
        sa.Column("user_id", sa.String(36)),
        sa.Column("model_key", sa.String(100)),
        sa.Column("region", sa.String(40)),
        sa.Column("api_route", sa.String(40)),
        sa.Column("purpose", sa.String(30), nullable=False, server_default="chat"),
        sa.Column(
            "execution_metadata",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    ):
        op.add_column("usage", column)
    op.execute("UPDATE usage SET user_id=k.user_id FROM api_keys k WHERE usage.key_id=k.id")
    op.alter_column("usage", "key_id", existing_type=sa.String(36), nullable=True)
    op.create_foreign_key("fk_usage_user_id", "usage", "users", ["user_id"], ["id"])
    op.create_index("ix_usage_user_id", "usage", ["user_id"])
    op.create_foreign_key("fk_usage_model_alias", "usage", "model_routes", ["alias"], ["alias"])

    # Frozen mapping for this migration. Do not import application defaults.
    path = Path(__file__).resolve().parents[2] / "app/catalog/cloud_bot_v7.json"
    catalog = json.loads(path.read_text(encoding="utf-8"))["models"]
    if len(catalog) != 42 or sum(m["is_public"] for m in catalog) != 40:
        raise RuntimeError("Cloud Bot v7 bootstrap artifact is invalid")
    table = sa.Table("model_routes", sa.MetaData(), autoload_with=op.get_bind())
    for model in catalog:
        values = {
            "alias": model["model_id"],
            "upstream_model": model["model_id"],
            "provider": model["backend"],
            "provider_transport": "mantle_chat",
            "model_key": model["model_key"],
            "display_name": model["display_name"],
            "manufacturer": model["manufacturer"],
            "region": model["region"],
            "api_route": model["api_route"],
            "model_type": model["model_type"],
            "max_output_tokens": model["max_output"],
            "max_context_tokens": model["context_window"],
            "is_public": model["is_public"],
            "external_api_allowed": model["is_public"],
            "enabled": True,
            "supports_vision": model["supports_vision"],
            "supports_reasoning": model["supports_reasoning"],
            "reasoning_efforts": model["reasoning_efforts"],
            "vision_behavior": model["vision_behavior"],
            "catalog_metadata": model["metadata"],
            "input_price": 0,
            "output_price": 0,
            "capabilities": {
                "text_input": True,
                "text_output": True,
                "system_prompt": True,
                "temperature": False,
                "streaming": False,
                "tools": False,
                "vision": model["supports_vision"],
                "image_input": model["supports_vision"],
                "reasoning": False,
                "reasoning_efforts": [],
            },
        }
        previous = (
            op.get_bind()
            .execute(sa.select(table).where(table.c.alias == model["model_id"]))
            .mappings()
            .one_or_none()
        )
        if previous is None:
            op.get_bind().execute(table.insert().values(**values))
        else:
            # A downgrade keeps model rows. Re-upgrade restores the new metadata
            # without altering existing enable switches, prices or permissions.
            if (
                previous["provider"] != model["backend"]
                or previous["upstream_model"] != model["model_id"]
            ):
                raise RuntimeError("Catalog identity collides with an existing route")
            for field in ("enabled", "external_api_allowed", "input_price", "output_price"):
                values[field] = previous[field]
            op.get_bind().execute(
                table.update().where(table.c.alias == model["model_id"]).values(**values)
            )


def downgrade():
    connection = op.get_bind()
    if connection.scalar(
        sa.text("SELECT count(*) FROM users WHERE id <> :id").bindparams(id=LEGACY_OWNER)
    ):
        raise RuntimeError("User account data prevents a lossless downgrade")
    if connection.scalar(sa.text("SELECT count(*) FROM usage WHERE key_id IS NULL")):
        raise RuntimeError("Internal inference history prevents a lossless downgrade")
    # Keep all model/key/usage rows. Fill unknown maxima with the prior schema's
    # default only when explicitly downgrading to that older schema.
    op.execute("UPDATE model_routes SET max_output_tokens=4096 WHERE max_output_tokens IS NULL")
    op.alter_column("model_routes", "max_output_tokens", existing_type=sa.Integer(), nullable=False)
    op.alter_column("usage", "key_id", existing_type=sa.String(36), nullable=False)
    op.drop_index("ix_usage_user_id", table_name="usage")
    op.drop_constraint("fk_usage_model_alias", "usage", type_="foreignkey")
    op.drop_constraint("fk_usage_user_id", "usage", type_="foreignkey")
    for name in ("user_id", "model_key", "region", "api_route", "purpose", "execution_metadata"):
        op.drop_column("usage", name)
    op.drop_index("uq_catalog_model_id", table_name="model_routes")
    op.drop_constraint("uq_model_routes_model_key", "model_routes", type_="unique")
    for name in (
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
    ):
        op.drop_column("model_routes", name)
    op.drop_index("ix_api_keys_user_id", table_name="api_keys")
    op.drop_constraint("fk_api_keys_user_id", "api_keys", type_="foreignkey")
    for name in ("user_id", "last_used_at", "all_public_models"):
        op.drop_column("api_keys", name)
    op.drop_table("users")
