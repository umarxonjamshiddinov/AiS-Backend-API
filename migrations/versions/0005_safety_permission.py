"""Opt-in server-to-server Safety permission. Existing keys remain unchanged."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "api_keys",
        sa.Column("safety_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade():
    op.drop_column("api_keys", "safety_enabled")
