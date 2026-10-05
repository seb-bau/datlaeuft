from alembic import op
import sqlalchemy as sa


revision = "e2f4a6b8c0d2"
down_revision = "d4e6f8a0b2c4"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("commission") as batch:
        batch.add_column(
            sa.Column("local_official_user_id", sa.Integer(), nullable=True)
        )
        batch.create_foreign_key(
            "fk_commission_local_official",
            "user_account",
            ["local_official_user_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_index(
            "ix_commission_local_official_user_id",
            ["local_official_user_id"],
        )

    with op.batch_alter_table("process_info_read") as batch:
        batch.add_column(
            sa.Column(
                "read_verified", sa.Boolean(),
                server_default=sa.false(), nullable=False,
            )
        )

    op.create_table(
        "process_info_external_read",
        sa.Column(
            "process_info_id", sa.String(36),
            sa.ForeignKey("process_info.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "external_craftsman_id", sa.Integer(),
            sa.ForeignKey("external_craftsman.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("read_version", sa.Integer(), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "browser_push_subscription",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(),
            sa.ForeignKey("user_account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("endpoint_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("subscription", sa.JSON(), nullable=False),
        sa.Column(
            "active", sa.Boolean(), server_default=sa.true(), nullable=False,
        ),
    )
    op.create_index(
        "ix_browser_push_subscription_user_id",
        "browser_push_subscription", ["user_id"],
    )

    op.create_table(
        "process_info_delivery",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "process_info_id", sa.String(36),
            sa.ForeignKey("process_info.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "commission_id", sa.Integer(),
            sa.ForeignKey("commission.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id", sa.Integer(),
            sa.ForeignKey("user_account.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("recipient_key", sa.String(80), nullable=False),
        sa.Column("recipient_email", sa.String(255), nullable=False),
        sa.Column("recipient_name", sa.String(255), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("destination_key", sa.String(80), nullable=False),
        sa.Column(
            "subscription_id", sa.Integer(),
            sa.ForeignKey("browser_push_subscription.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "state", sa.String(20), server_default="pending", nullable=False,
        ),
        sa.Column(
            "attempt_count", sa.Integer(), server_default="0", nullable=False,
        ),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "process_info_id", "version", "channel", "destination_key",
            name="uq_process_info_delivery_destination",
        ),
    )
    op.create_index(
        "ix_process_info_delivery_due",
        "process_info_delivery", ["state", "available_at"],
    )
    op.create_index(
        "ix_process_info_delivery_process_info_id",
        "process_info_delivery", ["process_info_id"],
    )
    op.create_index(
        "ix_process_info_delivery_user_id",
        "process_info_delivery", ["user_id"],
    )


def downgrade():
    op.drop_table("process_info_delivery")
    op.drop_table("browser_push_subscription")
    op.drop_table("process_info_external_read")

    with op.batch_alter_table("process_info_read") as batch:
        batch.drop_column("read_verified")

    with op.batch_alter_table("commission") as batch:
        batch.drop_index("ix_commission_local_official_user_id")
        batch.drop_constraint(
            "fk_commission_local_official", type_="foreignkey",
        )
        batch.drop_column("local_official_user_id")