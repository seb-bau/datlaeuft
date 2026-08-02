"""Add resilient commission synchronization

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-08-01 22:00:00.000000

"""
from uuid import uuid4

from alembic import op
import sqlalchemy as sa


revision = "d2e3f4a5b6c7"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("commission", schema=None) as batch_op:
        batch_op.alter_column("erp_id", existing_type=sa.Integer(), nullable=True)
        batch_op.add_column(sa.Column("public_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("source", sa.String(length=30), server_default="wowiport", nullable=False))
        batch_op.add_column(sa.Column("erp_sync_state", sa.String(length=30), server_default="synced", nullable=False))
        batch_op.add_column(sa.Column("erp_sync_message", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("erp_external_identification_number", sa.String(length=80), nullable=True))
        batch_op.add_column(sa.Column("commission_type_erp_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("crafts_process_erp_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("creditor_erp_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("department_erp_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("responsible_official_repair_erp_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("created_by_user_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("completion_successful", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column("tenant_charge_requested", sa.Boolean(), server_default="0", nullable=False))
        batch_op.add_column(sa.Column("completion_reason", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("completion_submitted_by_user_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("completion_submitted_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index(batch_op.f("ix_commission_public_id"), ["public_id"], unique=True)
        batch_op.create_index(batch_op.f("ix_commission_source"), ["source"], unique=False)
        batch_op.create_index(batch_op.f("ix_commission_erp_sync_state"), ["erp_sync_state"], unique=False)
        batch_op.create_index(batch_op.f("ix_commission_commission_type_erp_id"), ["commission_type_erp_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_commission_crafts_process_erp_id"), ["crafts_process_erp_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_commission_responsible_official_repair_erp_id"), ["responsible_official_repair_erp_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_commission_created_by_user_id"), ["created_by_user_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_commission_completion_submitted_by_user_id"), ["completion_submitted_by_user_id"], unique=False)
        batch_op.create_foreign_key(
            "fk_commission_created_by_user_id_user_account",
            "user_account",
            ["created_by_user_id"],
            ["id"],
        )
        batch_op.create_foreign_key(
            "fk_commission_completion_submitted_by_user_id_user_account",
            "user_account",
            ["completion_submitted_by_user_id"],
            ["id"],
        )

    connection = op.get_bind()
    commission_ids = connection.execute(sa.text("SELECT id FROM commission")).scalars().all()

    for commission_id in commission_ids:
        public_id = str(uuid4())
        connection.execute(
            sa.text(
                "UPDATE commission "
                "SET public_id = :public_id, "
                "erp_external_identification_number = :public_id "
                "WHERE id = :commission_id"
            ),
            {
                "public_id": public_id,
                "commission_id": commission_id,
            },
        )

    with op.batch_alter_table("commission", schema=None) as batch_op:
        batch_op.alter_column(
            "public_id",
            existing_type=sa.String(length=36),
            nullable=False,
        )

    op.create_table(
        "wowi_commission_type",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("erp_id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=160), nullable=True),
        sa.Column("active", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    with op.batch_alter_table("wowi_commission_type", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_wowi_commission_type_erp_id"), ["erp_id"], unique=True)
        batch_op.create_index(batch_op.f("ix_wowi_commission_type_code"), ["code"], unique=False)
        batch_op.create_index(batch_op.f("ix_wowi_commission_type_active"), ["active"], unique=False)

    op.create_table(
        "commission_position",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("commission_id", sa.Integer(), nullable=False),
        sa.Column("erp_id", sa.Integer(), nullable=True),
        sa.Column("position_index", sa.Integer(), nullable=False),
        sa.Column("position_number", sa.Float(), nullable=True),
        sa.Column("code", sa.String(length=120), nullable=True),
        sa.Column("commission_text", sa.Text(), nullable=True),
        sa.Column("internal_description", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("units", sa.Float(), server_default="1", nullable=False),
        sa.Column("unit_price", sa.Float(), nullable=True),
        sa.Column("net_amount", sa.Float(), nullable=True),
        sa.Column("material_costs", sa.Float(), nullable=True),
        sa.Column("labour_costs", sa.Float(), nullable=True),
        sa.Column("service_catalog_erp_id", sa.Integer(), nullable=True),
        sa.Column("craft_activity_erp_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["commission_id"], ["commission.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "commission_id",
            "position_index",
            name="uq_commission_position_index",
        ),
    )

    with op.batch_alter_table("commission_position", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_commission_position_commission_id"), ["commission_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_commission_position_erp_id"), ["erp_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_commission_position_service_catalog_erp_id"), ["service_catalog_erp_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_commission_position_craft_activity_erp_id"), ["craft_activity_erp_id"], unique=False)

    op.create_table(
        "erp_outbox_operation",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("commission_id", sa.Integer(), nullable=False),
        sa.Column("operation_type", sa.String(length=50), nullable=False),
        sa.Column("idempotency_key", sa.String(length=180), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(length=30), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["commission_id"], ["commission.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_erp_outbox_operation_idempotency_key",
        ),
    )

    with op.batch_alter_table("erp_outbox_operation", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_erp_outbox_operation_commission_id"), ["commission_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_erp_outbox_operation_operation_type"), ["operation_type"], unique=False)
        batch_op.create_index(batch_op.f("ix_erp_outbox_operation_state"), ["state"], unique=False)
        batch_op.create_index(batch_op.f("ix_erp_outbox_operation_available_at"), ["available_at"], unique=False)

    with op.batch_alter_table("webhook_event", schema=None) as batch_op:
        batch_op.add_column(sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False))
        batch_op.add_column(sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("processing_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index(batch_op.f("ix_webhook_event_next_attempt_at"), ["next_attempt_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_webhook_event_processing_at"), ["processing_at"], unique=False)


def downgrade():
    with op.batch_alter_table("webhook_event", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_webhook_event_processing_at"))
        batch_op.drop_index(batch_op.f("ix_webhook_event_next_attempt_at"))
        batch_op.drop_column("processed_at")
        batch_op.drop_column("processing_at")
        batch_op.drop_column("next_attempt_at")
        batch_op.drop_column("attempt_count")

    op.drop_table("erp_outbox_operation")
    op.drop_table("commission_position")
    op.drop_table("wowi_commission_type")

    with op.batch_alter_table("commission", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_commission_completion_submitted_by_user_id_user_account",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "fk_commission_created_by_user_id_user_account",
            type_="foreignkey",
        )
        batch_op.drop_column("completion_submitted_at")
        batch_op.drop_column("completion_submitted_by_user_id")
        batch_op.drop_column("completion_reason")
        batch_op.drop_column("tenant_charge_requested")
        batch_op.drop_column("completion_successful")
        batch_op.drop_column("created_by_user_id")
        batch_op.drop_column("responsible_official_repair_erp_id")
        batch_op.drop_column("department_erp_id")
        batch_op.drop_column("creditor_erp_id")
        batch_op.drop_column("crafts_process_erp_id")
        batch_op.drop_column("commission_type_erp_id")
        batch_op.drop_column("erp_external_identification_number")
        batch_op.drop_column("erp_sync_message")
        batch_op.drop_column("erp_sync_state")
        batch_op.drop_column("source")
        batch_op.drop_column("public_id")
        batch_op.alter_column("erp_id", existing_type=sa.Integer(), nullable=False)