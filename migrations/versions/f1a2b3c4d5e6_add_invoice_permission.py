from alembic import op
import sqlalchemy as sa


revision = "f1a2b3c4d5e6"
down_revision = "e0f3a6b82c91"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table(
        "user_account",
        schema=None,
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "can_manage_invoices",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            )
        )


def downgrade():
    with op.batch_alter_table(
        "user_account",
        schema=None,
    ) as batch_op:
        batch_op.drop_column(
            "can_manage_invoices"
        )