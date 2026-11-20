from alembic import op
import sqlalchemy as sa


revision = "94f2a6c8d0e3"
down_revision = "91e3c5a7b9d2"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("user_account", sa.Column("first_name", sa.String(255)))
    op.add_column("user_account", sa.Column("last_name", sa.String(255)))


def downgrade():
    op.drop_column("user_account", "last_name")
    op.drop_column("user_account", "first_name")
