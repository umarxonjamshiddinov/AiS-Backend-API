"""Initial PostgreSQL gateway schema."""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("model_routes",
        sa.Column("alias", sa.String(100), primary_key=True),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("upstream_model", sa.String(200), nullable=False),
        sa.Column("input_price", sa.Numeric(18, 8), nullable=False),
        sa.Column("output_price", sa.Numeric(18, 8), nullable=False),
        sa.Column("max_output_tokens", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False))
    op.create_table("api_keys",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("prefix", sa.String(24), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("budget_usd", sa.Numeric(18, 8), nullable=False),
        sa.Column("spent_usd", sa.Numeric(18, 8), nullable=False),
        sa.Column("reserved_usd", sa.Numeric(18, 8), nullable=False),
        sa.Column("rpm", sa.Integer(), nullable=False),
        sa.Column("tpm", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)))
    op.create_table("key_models",
        sa.Column("key_id", sa.String(36), sa.ForeignKey("api_keys.id"), primary_key=True),
        sa.Column("alias", sa.String(100), sa.ForeignKey("model_routes.alias"), primary_key=True))
    op.create_table("usage",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("key_id", sa.String(36), sa.ForeignKey("api_keys.id"), nullable=False),
        sa.Column("alias", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("reserved_tokens", sa.Integer(), nullable=False),
        sa.Column("reserved_cost", sa.Numeric(18, 8), nullable=False),
        sa.Column("input_price", sa.Numeric(18, 8), nullable=False),
        sa.Column("output_price", sa.Numeric(18, 8), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(18, 8), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False))
    op.create_index("ix_usage_key_id", "usage", ["key_id"])
    op.create_index("ix_usage_created_at", "usage", ["created_at"])


def downgrade():
    op.drop_table("usage")
    op.drop_table("key_models")
    op.drop_table("api_keys")
    op.drop_table("model_routes")
