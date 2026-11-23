from alembic import op
import sqlalchemy as sa


revision = "6b421e9c8d30"
down_revision = "23def4a6b8c0"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("safety_inspection_template") as batch:
        batch.add_column(sa.Column("time_allowance_minutes", sa.Integer()))
        batch.create_check_constraint("ck_safety_inspection_time_allowance", "time_allowance_minutes > 0")

    with op.batch_alter_table("time_entry") as batch:
        batch.add_column(sa.Column("safety_inspection_job_id", sa.Integer()))
        batch.add_column(sa.Column("safety_inspection_protocol_id", sa.Integer()))
        batch.create_unique_constraint("uq_time_entry_safety_inspection_job", ["safety_inspection_job_id"])
        batch.create_foreign_key(
            "fk_time_entry_safety_inspection_job", "safety_inspection_job", ["safety_inspection_job_id"], ["id"],
        )
        batch.create_foreign_key(
            "fk_time_entry_safety_inspection_protocol", "safety_inspection_protocol", ["safety_inspection_protocol_id"], ["id"],
        )
        batch.create_check_constraint(
            "ck_time_entry_safety_inspection_source",
            "(safety_inspection_job_id IS NULL AND safety_inspection_protocol_id IS NULL) OR "
            "(safety_inspection_job_id IS NOT NULL AND safety_inspection_protocol_id IS NOT NULL "
            "AND commission_id IS NULL)",
        )


def downgrade():
    with op.batch_alter_table("time_entry") as batch:
        batch.drop_constraint("ck_time_entry_safety_inspection_source", type_="check")
        batch.drop_constraint("fk_time_entry_safety_inspection_protocol", type_="foreignkey")
        batch.drop_constraint("fk_time_entry_safety_inspection_job", type_="foreignkey")
        batch.drop_constraint("uq_time_entry_safety_inspection_job", type_="unique")
        batch.drop_column("safety_inspection_protocol_id")
        batch.drop_column("safety_inspection_job_id")

    with op.batch_alter_table("safety_inspection_template") as batch:
        batch.drop_constraint("ck_safety_inspection_time_allowance", type_="check")
        batch.drop_column("time_allowance_minutes")
