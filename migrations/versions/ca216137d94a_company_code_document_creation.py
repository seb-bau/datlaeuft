from alembic import op
import sqlalchemy as sa


revision = "ca216137d94a"
down_revision = "f7c9e1a3b5d8"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "wowi_company_code",
        sa.Column("document_creation_disabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade():
    with op.batch_alter_table("wowi_company_code") as batch:
        batch.drop_column("document_creation_disabled")
