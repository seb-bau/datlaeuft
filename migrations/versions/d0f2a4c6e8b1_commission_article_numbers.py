from alembic import op
import sqlalchemy as sa


revision = "d0f2a4c6e8b1"
down_revision = "c7e9a1b3d5f8"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("commission_position") as batch:
        batch.add_column(sa.Column("article_number", sa.String(120), nullable=True))


def downgrade():
    with op.batch_alter_table("commission_position") as batch:
        batch.drop_column("article_number")
