from alembic import op
import sqlalchemy as sa


revision = "c7e9a1b3d5f8"
down_revision = "b4d6f8a0c2e4"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("local_catalog_item") as batch:
        batch.drop_index("ix_local_catalog_item_item_number")
        batch.create_index("ix_local_catalog_item_item_number", ["item_number"], unique=False)
        batch.create_unique_constraint(
            "uq_local_catalog_item_variant",
            ["item_number", "name", "business_area", "tax_label", "vat_rate"],
        )


def downgrade():
    if not op.get_context().as_sql:
        duplicate = op.get_bind().execute(sa.text(
            "SELECT item_number FROM local_catalog_item GROUP BY item_number HAVING COUNT(*) > 1"
        )).first()
        if duplicate is not None:
            raise RuntimeError("Vor dem Zurücksetzen müssen Artikelvarianten mit gleicher Artikelnummer bereinigt werden.")
    with op.batch_alter_table("local_catalog_item") as batch:
        batch.drop_constraint("uq_local_catalog_item_variant", type_="unique")
        batch.drop_index("ix_local_catalog_item_item_number")
        batch.create_index("ix_local_catalog_item_item_number", ["item_number"], unique=True)
