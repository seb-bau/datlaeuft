from alembic import op
import sqlalchemy as sa


revision = "f2c4e6a8b0d3"
down_revision = "e1c3a5b7d9f2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "wowi_responsible_official",
        sa.Column("erp_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("user_erp_id", sa.Integer(), nullable=True),
        sa.Column("person_erp_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(500), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("erp_id"),
    )
    for column_name in ("user_erp_id", "person_erp_id"):
        op.create_index(
            f"ix_wowi_responsible_official_{column_name}",
            "wowi_responsible_official",
            [column_name],
            unique=False,
        )


def downgrade():
    op.drop_table("wowi_responsible_official")