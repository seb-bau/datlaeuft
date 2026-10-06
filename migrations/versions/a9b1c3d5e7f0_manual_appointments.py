from alembic import op
import sqlalchemy as sa


revision = "a9b1c3d5e7f0"
down_revision = "e2f4a6b8c0d2"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("commission") as batch_op:
        batch_op.add_column(sa.Column(
            "accepted_appointment_has_time",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ))
        batch_op.add_column(sa.Column(
            "accepted_appointment_is_manual",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ))


def downgrade():
    with op.batch_alter_table("commission") as batch_op:
        batch_op.drop_column("accepted_appointment_is_manual")
        batch_op.drop_column("accepted_appointment_has_time")