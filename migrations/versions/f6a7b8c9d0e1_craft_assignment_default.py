from alembic import op
import sqlalchemy as sa


revision = "f6a7b8c9d0e1"
down_revision = "e4f6a8c2d0b1"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table(
        "craft_activity"
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "default_assignment_type",
                sa.String(length=20),
                server_default=sa.text("'internal'"),
                nullable=False,
            )
        )


def downgrade():
    with op.batch_alter_table(
        "craft_activity"
    ) as batch_op:
        batch_op.drop_column(
            "default_assignment_type"
        )