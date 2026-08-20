"""add jurisdiction person mappings

Revision ID: b14e8d2f3a5c
Revises: a03f7c1d2e4b
Create Date: 2026-08-20
"""

from alembic import op
import sqlalchemy as sa


revision = "b14e8d2f3a5c"
down_revision = "a03f7c1d2e4b"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_jurisdiction_person_mapping",
        sa.Column(
            "id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "jurisdiction_person_erp_id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "person_name_snapshot",
            sa.String(length=255),
            nullable=True,
        ),
        sa.Column(
            "active",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text(
                "CURRENT_TIMESTAMP"
            ),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text(
                "CURRENT_TIMESTAMP"
            ),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user_account.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "jurisdiction_person_erp_id",
            name=(
                "uq_user_jurisdiction_person_"
                "mapping_person"
            ),
        ),
    )

    op.create_index(
        (
            "ix_user_jurisdiction_person_"
            "mapping_user_id"
        ),
        "user_jurisdiction_person_mapping",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        (
            "ix_user_jurisdiction_person_"
            "mapping_active"
        ),
        "user_jurisdiction_person_mapping",
        ["active"],
        unique=False,
    )


def downgrade():
    op.drop_index(
        (
            "ix_user_jurisdiction_person_"
            "mapping_active"
        ),
        table_name=(
            "user_jurisdiction_person_mapping"
        ),
    )
    op.drop_index(
        (
            "ix_user_jurisdiction_person_"
            "mapping_user_id"
        ),
        table_name=(
            "user_jurisdiction_person_mapping"
        ),
    )
    op.drop_table(
        "user_jurisdiction_person_mapping"
    )