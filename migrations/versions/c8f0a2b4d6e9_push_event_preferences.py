from alembic import op
import sqlalchemy as sa


revision = "c8f0a2b4d6e9"
down_revision = "b5d7e9f1a3c6"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "user_account",
        sa.Column("push_event_preferences", sa.JSON(), nullable=True),
    )


def downgrade():
    op.drop_column("user_account", "push_event_preferences")