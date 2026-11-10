from alembic import op
import sqlalchemy as sa


revision = "ba8c2d4e6f10"
down_revision = "e7b9c1d3f5a7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "mobile_sync_operation",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.String(36), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("response_payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "operation_id"),
    )
    op.create_table(
        "mobile_sync_delivery",
        sa.Column("id", sa.String(200), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_mobile_sync_delivery_user_id", "mobile_sync_delivery", ["user_id"])
    op.create_index("ix_mobile_sync_delivery_available_at", "mobile_sync_delivery", ["available_at"])


def downgrade():
    op.drop_index("ix_mobile_sync_delivery_available_at", table_name="mobile_sync_delivery")
    op.drop_index("ix_mobile_sync_delivery_user_id", table_name="mobile_sync_delivery")
    op.drop_table("mobile_sync_delivery")
    op.drop_table("mobile_sync_operation")
