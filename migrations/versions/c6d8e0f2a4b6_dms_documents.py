from alembic import op
import sqlalchemy as sa


revision = "c6d8e0f2a4b6"
down_revision = "c4e6a8b0d2f5"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "dms_profile",
        sa.Column("document_type", sa.String(80), primary_key=True),
        sa.Column("category_id", sa.String(80), nullable=False),
        sa.Column("key_property_id", sa.String(80), nullable=False),
        sa.Column("mappings", sa.JSON(), nullable=False),
        sa.Column("schema", sa.JSON()),
    )
    op.create_table(
        "dms_document",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("document_type", sa.String(80), nullable=False),
        sa.Column("entity_type", sa.String(40), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("storage_name", sa.String(255), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column("profile_snapshot", sa.JSON()),
        sa.Column("hostname", sa.String(255), nullable=False),
        sa.Column("repository_id", sa.String(80), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True)),
        sa.Column("lock_token", sa.String(36)),
        sa.Column("upload_started", sa.Boolean(), nullable=False),
        sa.Column("dms_document_id", sa.String(80)),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
    )
    for column in ("document_type", "entity_type", "entity_id", "status", "available_at"):
        op.create_index(f"ix_dms_document_{column}", "dms_document", [column])


def downgrade():
    op.drop_table("dms_document")
    op.drop_table("dms_profile")