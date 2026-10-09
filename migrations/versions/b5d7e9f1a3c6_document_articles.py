from alembic import op
import sqlalchemy as sa


revision = "b5d7e9f1a3c6"
down_revision = "a2c4e6f8b0d1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "local_catalog_item",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("item_number", sa.String(100), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "business_area",
            sa.String(100),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "product_group",
            sa.String(100),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "unit",
            sa.String(40),
            nullable=False,
            server_default="Stk",
        ),
        sa.Column(
            "price_net",
            sa.Numeric(14, 4),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "tax_label",
            sa.String(80),
            nullable=False,
            server_default="Normalsteuersatz",
        ),
        sa.Column(
            "vat_rate",
            sa.Numeric(5, 2),
            nullable=False,
            server_default="19",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
    )

    for column in ("item_number", "name", "active"):
        op.create_index(
            f"ix_local_catalog_item_{column}",
            "local_catalog_item",
            [column],
            unique=column == "item_number",
        )

    for table in ("offer_position", "invoice_position"):
        with op.batch_alter_table(table) as batch:
            batch.add_column(
                sa.Column(
                    "local_catalog_item_id",
                    sa.Integer(),
                    nullable=True,
                )
            )
            batch.create_foreign_key(
                f"fk_{table}_local_catalog_item",
                "local_catalog_item",
                ["local_catalog_item_id"],
                ["id"],
                ondelete="SET NULL",
            )
            batch.create_index(
                f"ix_{table}_local_catalog_item_id",
                ["local_catalog_item_id"],
            )

    with op.batch_alter_table("offer_position") as batch:
        batch.add_column(
            sa.Column(
                "inventory_item_id",
                sa.Integer(),
                nullable=True,
            )
        )
        batch.add_column(
            sa.Column("source_data", sa.JSON(), nullable=True)
        )
        batch.create_foreign_key(
            "fk_offer_position_inventory_item",
            "inventory_item",
            ["inventory_item_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index(
            "ix_offer_position_inventory_item_id",
            ["inventory_item_id"],
        )


def downgrade():
    with op.batch_alter_table("offer_position") as batch:
        batch.drop_index("ix_offer_position_inventory_item_id")
        batch.drop_constraint(
            "fk_offer_position_inventory_item",
            type_="foreignkey",
        )
        batch.drop_column("source_data")
        batch.drop_column("inventory_item_id")

    for table in ("invoice_position", "offer_position"):
        with op.batch_alter_table(table) as batch:
            batch.drop_index(f"ix_{table}_local_catalog_item_id")
            batch.drop_constraint(
                f"fk_{table}_local_catalog_item",
                type_="foreignkey",
            )
            batch.drop_column("local_catalog_item_id")

    op.drop_table("local_catalog_item")