"""Allow real unlimited RPM/TPM using NULL; preserve every existing numeric limit."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    for name in ("rpm", "tpm"):
        op.alter_column("api_keys", name, existing_type=sa.Integer(), nullable=True)


def downgrade():
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM api_keys WHERE rpm IS NULL OR tpm IS NULL)")
    ):
        raise RuntimeError("Set explicit RPM and TPM limits before downgrading to revision 0003")
    for name in ("rpm", "tpm"):
        op.alter_column("api_keys", name, existing_type=sa.Integer(), nullable=False)
