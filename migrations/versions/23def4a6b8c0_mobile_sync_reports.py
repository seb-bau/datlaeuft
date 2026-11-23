from alembic import op
import sqlalchemy as sa


revision = "23def4a6b8c0"
down_revision = "96a4c8e2f0b6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "mobile_sync_report",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user_account.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("device_id", sa.String(36), primary_key=True),
        sa.Column("app_version", sa.String(40), nullable=False),
        sa.Column("app_build", sa.String(40), nullable=False),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("total_entries", sa.Integer(), nullable=False),
        sa.Column("entries", sa.JSON(), nullable=False),
    )


def downgrade():
    op.drop_table("mobile_sync_report")
