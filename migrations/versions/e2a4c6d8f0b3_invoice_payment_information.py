from alembic import op
import sqlalchemy as sa


revision = "e2a4c6d8f0b3"
down_revision = "d0f2a4c6e8b1"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("invoice") as batch:
        batch.add_column(sa.Column("payment_information", sa.JSON(), nullable=True))
    with op.batch_alter_table("wowi_company_code") as batch:
        batch.add_column(sa.Column("seller_creditor_id", sa.String(80), nullable=True))


def downgrade():
    with op.batch_alter_table("wowi_company_code") as batch:
        batch.drop_column("seller_creditor_id")
    with op.batch_alter_table("invoice") as batch:
        batch.drop_column("payment_information")
