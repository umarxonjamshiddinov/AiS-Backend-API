"""Model capabilities, technical quotas and expiring request reservations.

Existing models, keys, permissions and usage are preserved. New calendar and
concurrency limits are nullable for existing keys; admins may set them explicitly.
"""

import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "model_routes",
        sa.Column("external_api_allowed", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "model_routes",
        sa.Column("provider_transport", sa.String(40), nullable=False, server_default="converse"),
    )
    op.add_column(
        "model_routes",
        sa.Column(
            "capabilities",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column("model_routes", sa.Column("max_context_tokens", sa.Integer()))
    # Frozen legacy capabilities: do not import evolving application defaults here.
    for provider, modes, transport in (
        ("bedrock", ["auto", "none"], "converse"),
        ("openai", ["auto", "none", "required", "function"], "openai_chat"),
    ):
        capabilities = {
            "text_input": True,
            "text_output": True,
            "streaming": True,
            "system_prompt": True,
            "tools": True,
            "tool_choice": True,
            "tool_choice_modes": modes,
            "temperature": True,
        }
        op.execute(
            sa.text(
                "UPDATE model_routes SET capabilities=CAST(:caps AS jsonb), "
                "provider_transport=:transport WHERE provider=:provider"
            ).bindparams(caps=json.dumps(capabilities), transport=transport, provider=provider)
        )
    op.alter_column("model_routes", "provider_transport", server_default=None)
    op.alter_column("api_keys", "budget_usd", existing_type=sa.Numeric(18, 8), nullable=True)
    for name in ("requests_per_day", "requests_per_month", "tokens_per_day", "tokens_per_month"):
        op.add_column("api_keys", sa.Column(name, sa.BigInteger()))
    op.add_column("api_keys", sa.Column("max_concurrent_requests", sa.Integer()))
    op.add_column("api_keys", sa.Column("revoked_at", sa.DateTime(timezone=True)))
    op.execute("UPDATE api_keys SET revoked_at=CURRENT_TIMESTAMP WHERE NOT active")
    for column in (
        sa.Column("provider", sa.String(30), nullable=False, server_default="unknown"),
        sa.Column("upstream_model", sa.String(200), nullable=False, server_default=""),
        sa.Column("accounted_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("usage_estimated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error_code", sa.String(100)),
        sa.Column("dispatched_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
    ):
        op.add_column("usage", column)
    op.execute(
        "UPDATE usage SET provider=m.provider, upstream_model=m.upstream_model "
        "FROM model_routes m WHERE usage.alias=m.alias"
    )
    op.execute(
        "UPDATE usage SET accounted_tokens=input_tokens+output_tokens, "
        "finished_at=created_at WHERE status='completed'"
    )
    op.execute(
        "UPDATE usage SET accounted_tokens=reserved_tokens, usage_estimated=true, "
        "dispatched_at=created_at WHERE status IN ('reserved','uncertain')"
    )
    # A rolling deployment gets a lease grace period. Old uncertain reservations are
    # reaped automatically and transferred to estimated accounting by the new code.
    op.execute(
        "UPDATE usage SET lease_expires_at=CURRENT_TIMESTAMP + INTERVAL '10 minutes' "
        "WHERE status='reserved'"
    )
    op.execute(
        "UPDATE usage SET status='reserved', lease_expires_at=CURRENT_TIMESTAMP "
        "WHERE status='uncertain'"
    )
    op.create_index("ix_usage_key_created", "usage", ["key_id", "created_at"])
    op.create_index(
        "ix_usage_active_lease",
        "usage",
        ["lease_expires_at"],
        postgresql_where=sa.text("status = 'reserved'"),
    )


def downgrade():
    # A downgrade cannot faithfully restore unlimited budgets to a NOT NULL schema.
    # Refuse instead of inventing limits or discarding data.
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT count(*) FROM api_keys WHERE budget_usd IS NULL")):
        raise RuntimeError("Set explicit budgets before downgrading to revision 0001")
    op.drop_index("ix_usage_active_lease", table_name="usage")
    op.drop_index("ix_usage_key_created", table_name="usage")
    for name in (
        "provider",
        "upstream_model",
        "accounted_tokens",
        "usage_estimated",
        "error_code",
        "dispatched_at",
        "finished_at",
        "lease_expires_at",
    ):
        op.drop_column("usage", name)
    for name in (
        "requests_per_day",
        "requests_per_month",
        "tokens_per_day",
        "tokens_per_month",
        "max_concurrent_requests",
        "revoked_at",
    ):
        op.drop_column("api_keys", name)
    op.alter_column("api_keys", "budget_usd", existing_type=sa.Numeric(18, 8), nullable=False)
    for name in (
        "external_api_allowed",
        "provider_transport",
        "capabilities",
        "max_context_tokens",
    ):
        op.drop_column("model_routes", name)
