from alembic import op
import sqlalchemy as sa


revision = "c7d23a4b9e10"
down_revision = "f2c4e6a8b0d3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "health_heartbeat",
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("name"),
    )
    op.create_index(
        "ix_webhook_event_processed_received_at",
        "webhook_event",
        ["processed", "received_at"],
    )


def downgrade():
    op.drop_index(
        "ix_webhook_event_processed_received_at",
        table_name="webhook_event",
    )
    op.drop_table("health_heartbeat")