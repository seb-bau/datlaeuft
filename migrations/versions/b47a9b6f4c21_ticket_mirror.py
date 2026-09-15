from alembic import op
import sqlalchemy as sa


revision = "b47a9b6f4c21"
down_revision = "6cd5e600e656"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ticket_mirror",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("erp_id", sa.Integer(), nullable=False),
        sa.Column("id_num", sa.String(length=120), nullable=True),
        sa.Column("subject", sa.String(length=500), nullable=True),
        sa.Column("content_excerpt", sa.Text(), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("status_erp_id", sa.Integer(), nullable=True),
        sa.Column("status_code", sa.String(length=120), nullable=True),
        sa.Column("priority_erp_id", sa.Integer(), nullable=True),
        sa.Column("priority_code", sa.String(length=120), nullable=True),
        sa.Column("source_erp_id", sa.Integer(), nullable=True),
        sa.Column("source_code", sa.String(length=120), nullable=True),
        sa.Column("department_erp_id", sa.Integer(), nullable=True),
        sa.Column(
            "department_name",
            sa.String(length=255),
            nullable=True,
        ),
        sa.Column("assigned_user_erp_id", sa.Integer(), nullable=True),
        sa.Column("assignments", sa.JSON(), nullable=True),
        sa.Column(
            "state",
            sa.String(length=30),
            server_default="new",
            nullable=False,
        ),
        sa.Column("commission_id", sa.Integer(), nullable=True),
        sa.Column(
            "source_signature",
            sa.String(length=64),
            nullable=True,
        ),
        sa.Column(
            "ai_state",
            sa.String(length=30),
            server_default="not_requested",
            nullable=False,
        ),
        sa.Column("ai_candidate", sa.Boolean(), nullable=True),
        sa.Column(
            "ai_classification",
            sa.String(length=30),
            nullable=True,
        ),
        sa.Column("ai_craft_activity_id", sa.Integer(), nullable=True),
        sa.Column("ai_title", sa.String(length=255), nullable=True),
        sa.Column("ai_commission_text", sa.Text(), nullable=True),
        sa.Column(
            "ai_damage_location",
            sa.String(length=255),
            nullable=True,
        ),
        sa.Column("ai_missing_information", sa.JSON(), nullable=True),
        sa.Column("ai_model", sa.String(length=120), nullable=True),
        sa.Column("ai_input_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "ai_generated_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("ai_error", sa.Text(), nullable=True),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
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
        sa.ForeignKeyConstraint(
            ["commission_id"],
            ["commission.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["ai_craft_activity_id"],
            ["craft_activity.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("erp_id"),
    )

    for column in (
        "erp_id",
        "id_num",
        "subject",
        "received_at",
        "status_erp_id",
        "status_code",
        "priority_erp_id",
        "source_erp_id",
        "department_erp_id",
        "assigned_user_erp_id",
        "state",
        "commission_id",
        "ai_state",
        "ai_craft_activity_id",
        "deleted_at",
    ):
        op.create_index(
            f"ix_ticket_mirror_{column}",
            "ticket_mirror",
            [column],
        )


def downgrade():
    op.drop_table("ticket_mirror")