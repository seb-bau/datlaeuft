"""repair missing ERP synchronization tables

Revision ID: ae712522b0d1
Revises: d2e3f4a5b6c7
Create Date: 2026-08-01 22:33:09.225791

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'ae712522b0d1'
down_revision = 'd2e3f4a5b6c7'
branch_labels = None
depends_on = None


def create_missing_indexes(table_name, index_definitions):
    inspector = sa.inspect(op.get_bind())
    existing_indexes = {
        index["name"]
        for index in inspector.get_indexes(table_name)
    }

    for index_name, columns, unique in index_definitions:
        if index_name not in existing_indexes:
            op.create_index(
                index_name,
                table_name,
                columns,
                unique=unique,
            )


def upgrade():
    inspector = sa.inspect(op.get_bind())
    existing_tables = set(inspector.get_table_names())

    if "wowi_commission_type" not in existing_tables:
        op.create_table(
            "wowi_commission_type",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("erp_id", sa.Integer(), nullable=False),
            sa.Column("code", sa.String(length=160), nullable=True),
            sa.Column(
                "active",
                sa.Boolean(),
                server_default=sa.text("1"),
                nullable=False,
            ),
            sa.Column(
                "synced_at",
                sa.DateTime(timezone=True),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("id"),
        )

    create_missing_indexes(
        "wowi_commission_type",
        [
            ("ix_wowi_commission_type_active", ["active"], False),
            ("ix_wowi_commission_type_code", ["code"], False),
            ("ix_wowi_commission_type_erp_id", ["erp_id"], True),
        ],
    )

    if "commission_position" not in existing_tables:
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
            sa.Column(
                "units",
                sa.Float(),
                server_default=sa.text("1"),
                nullable=False,
            ),
            sa.Column("unit_price", sa.Float(), nullable=True),
            sa.Column("net_amount", sa.Float(), nullable=True),
            sa.Column("material_costs", sa.Float(), nullable=True),
            sa.Column("labour_costs", sa.Float(), nullable=True),
            sa.Column("service_catalog_erp_id", sa.Integer(), nullable=True),
            sa.Column("craft_activity_erp_id", sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(
                ["commission_id"],
                ["commission.id"],
            ),
            sa.PrimaryKeyConstraint("id"),
        )

    create_missing_indexes(
        "commission_position",
        [
            (
                "ix_commission_position_commission_id",
                ["commission_id"],
                False,
            ),
            (
                "ix_commission_position_craft_activity_erp_id",
                ["craft_activity_erp_id"],
                False,
            ),
            (
                "ix_commission_position_erp_id",
                ["erp_id"],
                False,
            ),
            (
                "ix_commission_position_service_catalog_erp_id",
                ["service_catalog_erp_id"],
                False,
            ),
        ],
    )

    if "erp_outbox_operation" not in existing_tables:
        op.create_table(
            "erp_outbox_operation",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("commission_id", sa.Integer(), nullable=False),
            sa.Column(
                "operation_type",
                sa.String(length=50),
                nullable=False,
            ),
            sa.Column(
                "idempotency_key",
                sa.String(length=180),
                nullable=False,
            ),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column(
                "state",
                sa.String(length=30),
                nullable=False,
            ),
            sa.Column(
                "attempt_count",
                sa.Integer(),
                server_default=sa.text("0"),
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
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
            ),
            sa.Column(
                "completed_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
            sa.ForeignKeyConstraint(
                ["commission_id"],
                ["commission.id"],
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "idempotency_key",
                name="uq_erp_outbox_operation_idempotency_key",
            ),
        )

    create_missing_indexes(
        "erp_outbox_operation",
        [
            (
                "ix_erp_outbox_operation_available_at",
                ["available_at"],
                False,
            ),
            (
                "ix_erp_outbox_operation_commission_id",
                ["commission_id"],
                False,
            ),
            (
                "ix_erp_outbox_operation_operation_type",
                ["operation_type"],
                False,
            ),
            (
                "ix_erp_outbox_operation_state",
                ["state"],
                False,
            ),
        ],
    )


def downgrade():
    pass