from alembic import op
import sqlalchemy as sa


revision = "18c65af3e290"
down_revision = "3e8a1c7d5b2f"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "wowi_company_code",
        sa.Column("tenant_charge_letter_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("invoice", sa.Column("completed_at", sa.DateTime(timezone=True)))
    op.add_column("invoice", sa.Column("completion_mode", sa.String(20)))


def downgrade():
    op.drop_column("invoice", "completion_mode")
    op.drop_column("invoice", "completed_at")
    op.drop_column("wowi_company_code", "tenant_charge_letter_enabled")
