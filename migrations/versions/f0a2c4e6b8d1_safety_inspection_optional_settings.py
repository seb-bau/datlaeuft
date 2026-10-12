from alembic import op
import sqlalchemy as sa


revision = "f0a2c4e6b8d1"
down_revision = "c8f0a2b4d6e9"
branch_labels = None
depends_on = None


GROUP_COLUMNS = (
    "department_id",
    "corrective_commission_type_erp_id",
    "corrective_craft_activity_id",
    "corrective_task_template_id",
)


def upgrade():
    with op.batch_alter_table("safety_inspection_group") as batch_op:
        for column in GROUP_COLUMNS:
            batch_op.alter_column(
                column,
                existing_type=sa.Integer(),
                nullable=True,
            )
    with op.batch_alter_table("safety_inspection_job") as batch_op:
        batch_op.alter_column(
            "department_id",
            existing_type=sa.Integer(),
            nullable=True,
        )


def downgrade():
    connection = op.get_bind()
    group_exists = connection.execute(sa.text(
        "SELECT id FROM safety_inspection_group WHERE "
        + " OR ".join(column + " IS NULL" for column in GROUP_COLUMNS)
        + " LIMIT 1"
    )).first()
    job_exists = connection.execute(sa.text(
        "SELECT id FROM safety_inspection_job "
        "WHERE department_id IS NULL LIMIT 1"
    )).first()
    if group_exists or job_exists:
        raise RuntimeError(
            "Vor dem Downgrade müssen Abteilungen und Mängelvorgaben ergänzt werden."
        )
    with op.batch_alter_table("safety_inspection_job") as batch_op:
        batch_op.alter_column(
            "department_id",
            existing_type=sa.Integer(),
            nullable=False,
        )
    with op.batch_alter_table("safety_inspection_group") as batch_op:
        for column in GROUP_COLUMNS:
            batch_op.alter_column(
                column,
                existing_type=sa.Integer(),
                nullable=False,
            )