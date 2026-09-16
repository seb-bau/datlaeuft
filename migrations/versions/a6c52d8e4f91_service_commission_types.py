from alembic import op
import sqlalchemy as sa


revision = "a6c52d8e4f91"
down_revision = "f3a18c7d9e42"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table(
        "time_task_template"
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "commission_type_erp_ids",
                sa.JSON(),
                nullable=True,
            )
        )


def downgrade():
    with op.batch_alter_table(
        "time_task_template"
    ) as batch_op:
        batch_op.drop_column(
            "commission_type_erp_ids"
        )