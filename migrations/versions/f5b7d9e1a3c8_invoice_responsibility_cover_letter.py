from alembic import op
import sqlalchemy as sa


revision = "f5b7d9e1a3c8"
down_revision = "f4a6c8e0b2d7"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("user_account") as batch:
        batch.add_column(sa.Column("phone", sa.String(80)))
    with op.batch_alter_table("wowi_company_code") as batch:
        batch.add_column(sa.Column("cover_letter_stationery_filename", sa.String(255)))
    with op.batch_alter_table("invoice") as batch:
        batch.add_column(sa.Column("responsible_user_id", sa.Integer()))
        batch.add_column(sa.Column("responsibility_revision", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("cover_letter_title", sa.String(200)))
        batch.add_column(sa.Column("cover_letter_text", sa.Text()))
        batch.add_column(sa.Column("cover_letter_date", sa.Date()))
    op.execute(sa.text("UPDATE invoice SET responsible_user_id = created_by_user_id"))
    with op.batch_alter_table("invoice") as batch:
        batch.alter_column("responsible_user_id", existing_type=sa.Integer(), nullable=False)
        batch.create_foreign_key("fk_invoice_responsible_user_id", "user_account", ["responsible_user_id"], ["id"])
        batch.create_index("ix_invoice_responsible_user_id", ["responsible_user_id"])


def downgrade():
    with op.batch_alter_table("invoice") as batch:
        batch.drop_constraint("fk_invoice_responsible_user_id", type_="foreignkey")
        batch.drop_index("ix_invoice_responsible_user_id")
        for name in ("cover_letter_date", "cover_letter_text", "cover_letter_title", "responsibility_revision", "responsible_user_id"):
            batch.drop_column(name)
    with op.batch_alter_table("wowi_company_code") as batch:
        batch.drop_column("cover_letter_stationery_filename")
    with op.batch_alter_table("user_account") as batch:
        batch.drop_column("phone")
