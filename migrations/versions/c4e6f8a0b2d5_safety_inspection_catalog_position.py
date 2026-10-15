from alembic import op
import sqlalchemy as sa


revision = "c4e6f8a0b2d5"
down_revision = "b3d5f7a9c1e4"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "safety_inspection_template_item",
        sa.Column("catalog_position", sa.String(16), nullable=True),
    )
    op.add_column(
        "safety_inspection_result",
        sa.Column("item_catalog_position", sa.String(16), nullable=True),
    )


def downgrade():
    connection = op.get_bind()
    for table_name, column_name in (
        ("safety_inspection_template_item", "catalog_position"),
        ("safety_inspection_result", "item_catalog_position"),
    ):
        existing = connection.execute(sa.text(
            f"SELECT id FROM {table_name} "
            f"WHERE {column_name} IS NOT NULL AND {column_name} <> '' LIMIT 1"
        )).first()
        if existing:
            raise RuntimeError("Ein Downgrade ist mit vorhandenen Katalogpositionen nicht möglich.")
    with op.batch_alter_table("safety_inspection_result") as batch_op:
        batch_op.drop_column("item_catalog_position")
    with op.batch_alter_table("safety_inspection_template_item") as batch_op:
        batch_op.drop_column("catalog_position")