from alembic import op
import sqlalchemy as sa

revision = "b4d6f8a0c2e4"
down_revision = "a3c5e7f9b1d2"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_context().as_sql or "appointment_calendar_email_enabled" not in {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("user_account")
    }:
        with op.batch_alter_table("user_account") as batch:
            batch.add_column(sa.Column("appointment_calendar_email_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade():
    with op.batch_alter_table("user_account") as batch:
        batch.drop_column("appointment_calendar_email_enabled")
