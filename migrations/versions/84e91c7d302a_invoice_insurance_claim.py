from alembic import op
import sqlalchemy as sa


revision = "84e91c7d302a"
down_revision = "7bc34cfa91d5"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("invoice") as batch:
        batch.add_column(sa.Column("insurance_claim_number", sa.String(120), nullable=True))


def downgrade():
    with op.batch_alter_table("invoice") as batch:
        batch.drop_column("insurance_claim_number")
