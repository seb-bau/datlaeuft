from alembic import op
import sqlalchemy as sa

revision = "d2f4a6b8c0e2"
down_revision = ("0d3187067c49", "f9a2b3c4d5e6")
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("safety_inspection_job") as batch:
        batch.add_column(
            sa.Column("visit_data", sa.JSON(), nullable=True)
        )


def downgrade():
    with op.batch_alter_table("safety_inspection_job") as batch:
        batch.drop_column("visit_data")