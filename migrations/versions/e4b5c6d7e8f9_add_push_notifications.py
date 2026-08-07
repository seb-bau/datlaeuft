from alembic import op
import sqlalchemy as sa


revision = "e4b5c6d7e8f9"
down_revision = "d3a4b5c6d7e8"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table(
        "user_account",
        schema=None,
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "push_new_commission_enabled",
                sa.Boolean(),
                server_default=sa.true(),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "push_changed_commission_enabled",
                sa.Boolean(),
                server_default=sa.true(),
                nullable=False,
            )
        )

    op.create_table(
        "push_device",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("installation_id", sa.String(length=36), nullable=False),
        sa.Column("device_token", sa.String(length=512), nullable=False),
        sa.Column(
            "platform",
            sa.String(length=30),
            server_default="ios",
            nullable=False,
        ),
        sa.Column("app_version", sa.String(length=80), nullable=True),
        sa.Column("device_name", sa.String(length=255), nullable=True),
        sa.Column(
            "active",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "last_registered_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user_account.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "installation_id",
            name="uq_push_device_installation_id",
        ),
    )
    op.create_index(
        op.f("ix_push_device_active"),
        "push_device",
        ["active"],
        unique=False,
    )
    op.create_index(
        op.f("ix_push_device_device_token"),
        "push_device",
        ["device_token"],
        unique=False,
    )
    op.create_index(
        op.f("ix_push_device_installation_id"),
        "push_device",
        ["installation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_push_device_user_id"),
        "push_device",
        ["user_id"],
        unique=False,
    )

    op.create_table(
        "push_delivery",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("device_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("commission_id", sa.Integer(), nullable=True),
        sa.Column("device_token", sa.String(length=512), nullable=False),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("title", sa.String(length=180), nullable=False),
        sa.Column("body", sa.String(length=500), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("collapse_id", sa.String(length=64), nullable=True),
        sa.Column(
            "state",
            sa.String(length=30),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "locked_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("provider_status", sa.Integer(), nullable=True),
        sa.Column("provider_reason", sa.String(length=120), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["commission_id"],
            ["commission.id"],
        ),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["push_device.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user_account.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "batch_id",
            "device_id",
            name="uq_push_delivery_batch_device",
        ),
    )
    op.create_index(
        op.f("ix_push_delivery_available_at"),
        "push_delivery",
        ["available_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_push_delivery_batch_id"),
        "push_delivery",
        ["batch_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_push_delivery_commission_id"),
        "push_delivery",
        ["commission_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_push_delivery_device_id"),
        "push_delivery",
        ["device_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_push_delivery_event_type"),
        "push_delivery",
        ["event_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_push_delivery_locked_at"),
        "push_delivery",
        ["locked_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_push_delivery_state"),
        "push_delivery",
        ["state"],
        unique=False,
    )
    op.create_index(
        op.f("ix_push_delivery_user_id"),
        "push_delivery",
        ["user_id"],
        unique=False,
    )


def downgrade():
    op.drop_index(
        op.f("ix_push_delivery_user_id"),
        table_name="push_delivery",
    )
    op.drop_index(
        op.f("ix_push_delivery_state"),
        table_name="push_delivery",
    )
    op.drop_index(
        op.f("ix_push_delivery_locked_at"),
        table_name="push_delivery",
    )
    op.drop_index(
        op.f("ix_push_delivery_event_type"),
        table_name="push_delivery",
    )
    op.drop_index(
        op.f("ix_push_delivery_device_id"),
        table_name="push_delivery",
    )
    op.drop_index(
        op.f("ix_push_delivery_commission_id"),
        table_name="push_delivery",
    )
    op.drop_index(
        op.f("ix_push_delivery_batch_id"),
        table_name="push_delivery",
    )
    op.drop_index(
        op.f("ix_push_delivery_available_at"),
        table_name="push_delivery",
    )
    op.drop_table("push_delivery")

    op.drop_index(
        op.f("ix_push_device_user_id"),
        table_name="push_device",
    )
    op.drop_index(
        op.f("ix_push_device_installation_id"),
        table_name="push_device",
    )
    op.drop_index(
        op.f("ix_push_device_device_token"),
        table_name="push_device",
    )
    op.drop_index(
        op.f("ix_push_device_active"),
        table_name="push_device",
    )
    op.drop_table("push_device")

    with op.batch_alter_table(
        "user_account",
        schema=None,
    ) as batch_op:
        batch_op.drop_column(
            "push_changed_commission_enabled"
        )
        batch_op.drop_column(
            "push_new_commission_enabled"
        )