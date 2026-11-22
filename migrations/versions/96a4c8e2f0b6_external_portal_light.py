from alembic import op
import sqlalchemy as sa


revision = "96a4c8e2f0b6"
down_revision = "94f2a6c8d0e3"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("external_craftsman") as batch:
        batch.add_column(sa.Column("portal_light_enabled", sa.Boolean()))
        batch.add_column(sa.Column("reminder_first_days", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("reminder_second_days", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("cancel_unanswered_days", sa.Integer(), nullable=False, server_default="0"))
    with op.batch_alter_table("external_commission_assignment_mail") as batch:
        batch.add_column(sa.Column("response_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("response_decision", sa.String(20)))
        batch.add_column(sa.Column("response_text", sa.Text()))
        batch.add_column(sa.Column("auto_canceled_at", sa.DateTime(timezone=True)))


def downgrade():
    with op.batch_alter_table("external_commission_assignment_mail") as batch:
        for name in ("auto_canceled_at", "response_text", "response_decision", "response_at"):
            batch.drop_column(name)
    with op.batch_alter_table("external_craftsman") as batch:
        for name in ("cancel_unanswered_days", "reminder_second_days", "reminder_first_days", "portal_light_enabled"):
            batch.drop_column(name)
