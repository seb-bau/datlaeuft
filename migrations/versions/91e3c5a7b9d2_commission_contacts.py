from alembic import op
import sqlalchemy as sa


revision = "91e3c5a7b9d2"
down_revision = "84e91c7d302a"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("commission", sa.Column("reporter", sa.Text(), nullable=True))
    op.add_column("commission", sa.Column("other_affected", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("commission", "other_affected")
    op.drop_column("commission", "reporter")
