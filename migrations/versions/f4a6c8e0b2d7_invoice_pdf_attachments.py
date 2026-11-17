from alembic import op
import sqlalchemy as sa


revision = "f4a6c8e0b2d7"
down_revision = "e2a4c6d8f0b3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "invoice_attachment",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("invoice_id", sa.Integer(), sa.ForeignKey("invoice.id", ondelete="CASCADE"), nullable=False),
        sa.Column("uploaded_by_user_id", sa.Integer(), sa.ForeignKey("user_account.id", ondelete="SET NULL")),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("storage_name", sa.String(500), nullable=False, unique=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("position_index", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_invoice_attachment_invoice_id", "invoice_attachment", ["invoice_id"])


def downgrade():
    op.drop_index("ix_invoice_attachment_invoice_id", table_name="invoice_attachment")
    op.drop_table("invoice_attachment")
