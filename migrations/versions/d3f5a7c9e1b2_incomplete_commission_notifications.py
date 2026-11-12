from alembic import op
import sqlalchemy as sa


revision = "d3f5a7c9e1b2"
down_revision = "c2e4a6b8d0f1"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("commission_completion_notification") as batch_op:
        batch_op.add_column(sa.Column(
            "notification_key", sa.String(100), server_default="completion", nullable=False,
        ))
        batch_op.add_column(sa.Column("report_submitted_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_unique_constraint(
            "uq_commission_completion_notification_event", ["commission_id", "notification_key"],
        )
        batch_op.drop_constraint("uq_commission_completion_notification", type_="unique")


def downgrade():
    op.execute(sa.text(
        "DELETE FROM commission_completion_notification WHERE notification_key <> 'completion'"
    ))
    with op.batch_alter_table("commission_completion_notification") as batch_op:
        batch_op.create_unique_constraint("uq_commission_completion_notification", ["commission_id"])
        batch_op.drop_constraint("uq_commission_completion_notification_event", type_="unique")
        batch_op.drop_column("report_submitted_at")
        batch_op.drop_column("notification_key")
