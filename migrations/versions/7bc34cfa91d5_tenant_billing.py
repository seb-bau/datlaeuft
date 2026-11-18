from alembic import op
import sqlalchemy as sa


revision = "7bc34cfa91d5"
down_revision = "ca216137d94a"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("wowi_company_code") as batch:
        batch.add_column(sa.Column("tenant_billing_issuer_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("tenant_billing_recipient_email", sa.String(255), nullable=True))
        batch.create_foreign_key(
            "fk_company_code_tenant_billing_issuer", "wowi_company_code",
            ["tenant_billing_issuer_id"], ["id"],
        )

    with op.batch_alter_table("invoice") as batch:
        batch.add_column(sa.Column("tenant_billing_reference", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("tenant_charge_letter", sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table("invoice") as batch:
        batch.drop_column("tenant_charge_letter")
        batch.drop_column("tenant_billing_reference")

    with op.batch_alter_table("wowi_company_code") as batch:
        batch.drop_constraint("fk_company_code_tenant_billing_issuer", type_="foreignkey")
        batch.drop_column("tenant_billing_recipient_email")
        batch.drop_column("tenant_billing_issuer_id")
