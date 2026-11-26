from alembic import op
import sqlalchemy as sa


revision = "5c3a5ee04f62"
down_revision = "18c65af3e290"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("dms_document", sa.Column("invoice_dispatch", sa.JSON(none_as_null=True)))
    with op.batch_alter_table("user_account") as batch:
        batch.add_column(sa.Column("paperwork_user_id", sa.String(40)))
        batch.create_unique_constraint("uq_user_account_paperwork_user_id", ["paperwork_user_id"])


def downgrade():
    with op.batch_alter_table("user_account") as batch:
        batch.drop_constraint("uq_user_account_paperwork_user_id", type_="unique")
        batch.drop_column("paperwork_user_id")
    op.drop_column("dms_document", "invoice_dispatch")
