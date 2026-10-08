from alembic import op
import sqlalchemy as sa

revision = "a1b3c5d7e9f2"
down_revision = "c7d23a4b9e10"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "blacklist_entry",
        sa.Column("target_type", sa.String(20), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("name_snapshot", sa.String(500), nullable=False),
        sa.Column("reason", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("target_type", "target_id"),
        sa.CheckConstraint(
            "target_type IN ('user', 'official', 'external')",
            name="ck_blacklist_target_type",
        ),
        sa.CheckConstraint("target_id > 0", name="ck_blacklist_target_id"),
    )


def downgrade():
    op.drop_table("blacklist_entry")