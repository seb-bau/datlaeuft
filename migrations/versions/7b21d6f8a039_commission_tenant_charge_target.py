from alembic import op
import sqlalchemy as sa


revision = "7b21d6f8a039"
down_revision = "5c3a5ee04f62"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("commission", sa.Column("tenant_charge_target", sa.JSON(none_as_null=True)))


def downgrade():
    op.drop_column("commission", "tenant_charge_target")
