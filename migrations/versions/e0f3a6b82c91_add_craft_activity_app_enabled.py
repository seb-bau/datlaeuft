from alembic import op
import sqlalchemy as sa


revision = "e0f3a6b82c91"
down_revision = "d9e2c5f41a70"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table(
        "craft_activity",
        schema=None,
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "app_enabled",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            )
        )

    op.execute(
        sa.text(
            "UPDATE craft_activity "
            "SET app_enabled = 1"
        )
    )


def downgrade():
    with op.batch_alter_table(
        "craft_activity",
        schema=None,
    ) as batch_op:
        batch_op.drop_column(
            "app_enabled"
        )