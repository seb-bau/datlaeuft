from alembic import op
import sqlalchemy as sa


revision = "e5f7a9b1c3d5"
down_revision = "d3f5a7c9e1b2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "commission_tenant_sms_notification",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("commission_id", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(30), server_default="pending", nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["commission_id"], ["commission.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("commission_id", name="uq_commission_tenant_sms_notification"),
    )
    op.create_index("ix_tenant_sms_notification_due", "commission_tenant_sms_notification", ["state", "available_at"])


def downgrade():
    op.drop_index("ix_tenant_sms_notification_due", table_name="commission_tenant_sms_notification")
    op.drop_table("commission_tenant_sms_notification")
