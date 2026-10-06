from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "b2d4f6a8c0e3"
down_revision = "a9b1c3d5e7f0"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("commission") as batch_op:
        batch_op.add_column(sa.Column(
            "notify_tenants", sa.Boolean(), server_default=sa.false(), nullable=False,
        ))
    op.create_table(
        "commission_tenant_notification",
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
        sa.UniqueConstraint("commission_id", name="uq_commission_tenant_notification"),
    )
    op.create_index("ix_tenant_notification_due", "commission_tenant_notification", ["state", "available_at"])
    setting_table = sa.table("app_setting",
        sa.column("key", sa.String), sa.column("value_text", sa.Text),
        sa.column("value_type", sa.String), sa.column("label", sa.String),
        sa.column("group", sa.String), sa.column("is_secret", sa.Boolean),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    now = datetime.now(timezone.utc)
    op.bulk_insert(setting_table, [{
        "key": "commissions.tenant_notifications_start_at", "value_text": now.isoformat(),
        "value_type": "string", "label": "Beginn der Mieter-Mitteilungen", "group": "E-Mail",
        "is_secret": False, "updated_at": now,
    }])


def downgrade():
    op.execute(sa.text("DELETE FROM app_setting WHERE key = 'commissions.tenant_notifications_start_at'"))
    op.drop_index("ix_tenant_notification_due", table_name="commission_tenant_notification")
    op.drop_table("commission_tenant_notification")
    with op.batch_alter_table("commission") as batch_op:
        batch_op.drop_column("notify_tenants")