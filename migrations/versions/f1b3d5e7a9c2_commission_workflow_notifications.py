from alembic import op
import sqlalchemy as sa

revision = "f1b3d5e7a9c2"
down_revision = "e5f7a9b1c3d5"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("commission", sa.Column("is_retrospective", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("commission", sa.Column("overdue_notification_deadline", sa.DateTime(timezone=True)))
    op.create_table(
        "workflow_notification",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("notification_key", sa.String(180), nullable=False, unique=True),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("commission_id", sa.Integer(), sa.ForeignKey("commission.id", ondelete="CASCADE")),
        sa.Column("ticket_id", sa.Integer(), sa.ForeignKey("ticket_mirror.id", ondelete="CASCADE")),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(30), server_default="pending", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
    )
    op.create_index("ix_workflow_notification_due", "workflow_notification", ["state", "available_at"])
    op.create_index("ix_workflow_notification_commission_id", "workflow_notification", ["commission_id"])
    op.create_index("ix_workflow_notification_ticket_id", "workflow_notification", ["ticket_id"])


def downgrade():
    op.drop_table("workflow_notification")
    op.drop_column("commission", "is_retrospective")
    op.drop_column("commission", "overdue_notification_deadline")
