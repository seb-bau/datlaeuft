from alembic import op
import sqlalchemy as sa


revision = "c4e6a8b0d2f5"
down_revision = "b2d4f6a8c0e3"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("commission") as batch_op:
        batch_op.add_column(sa.Column(
            "notify_on_completion", sa.Boolean(), server_default=sa.false(), nullable=False,
        ))
    op.create_table(
        "commission_completion_notification",
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
        sa.UniqueConstraint("commission_id", name="uq_commission_completion_notification"),
    )
    op.create_index("ix_completion_notification_due", "commission_completion_notification", ["state", "available_at"])


def downgrade():
    op.drop_index("ix_completion_notification_due", table_name="commission_completion_notification")
    op.drop_table("commission_completion_notification")
    with op.batch_alter_table("commission") as batch_op:
        batch_op.drop_column("notify_on_completion")