from alembic import op
import sqlalchemy as sa


revision = "f9c1a2b3d4e5"
down_revision = "e436ae9a75d6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "inventory_location",
        sa.Column(
            "id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "code",
            sa.String(length=80),
            nullable=False,
        ),
        sa.Column(
            "name",
            sa.String(length=255),
            nullable=False,
        ),
        sa.Column(
            "location_type",
            sa.String(length=40),
            server_default="warehouse",
            nullable=False,
        ),
        sa.Column(
            "is_default",
            sa.Boolean(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "active",
            sa.Boolean(),
            server_default="1",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    with op.batch_alter_table(
        "inventory_location",
        schema=None,
    ) as batch_op:
        batch_op.create_index(
            "ix_inventory_location_code",
            ["code"],
            unique=True,
        )
        batch_op.create_index(
            "ix_inventory_location_name",
            ["name"],
            unique=False,
        )
        batch_op.create_index(
            "ix_inventory_location_location_type",
            ["location_type"],
            unique=False,
        )
        batch_op.create_index(
            "ix_inventory_location_is_default",
            ["is_default"],
            unique=False,
        )
        batch_op.create_index(
            "ix_inventory_location_active",
            ["active"],
            unique=False,
        )

    op.create_table(
        "inventory_stock",
        sa.Column(
            "id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "item_id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "location_id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "quantity",
            sa.Float(),
            server_default="0",
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["inventory_item.id"],
        ),
        sa.ForeignKeyConstraint(
            ["location_id"],
            ["inventory_location.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "item_id",
            "location_id",
            name="uq_inventory_stock_item_location",
        ),
    )

    with op.batch_alter_table(
        "inventory_stock",
        schema=None,
    ) as batch_op:
        batch_op.create_index(
            "ix_inventory_stock_item_id",
            ["item_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_inventory_stock_location_id",
            ["location_id"],
            unique=False,
        )

    op.create_table(
        "inventory_item_barcode",
        sa.Column(
            "id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "item_id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "code",
            sa.String(length=160),
            nullable=False,
        ),
        sa.Column(
            "kind",
            sa.String(length=40),
            server_default="ean",
            nullable=False,
        ),
        sa.Column(
            "description",
            sa.String(length=255),
            nullable=True,
        ),
        sa.Column(
            "active",
            sa.Boolean(),
            server_default="1",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["inventory_item.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    with op.batch_alter_table(
        "inventory_item_barcode",
        schema=None,
    ) as batch_op:
        batch_op.create_index(
            "ix_inventory_item_barcode_item_id",
            ["item_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_inventory_item_barcode_code",
            ["code"],
            unique=True,
        )
        batch_op.create_index(
            "ix_inventory_item_barcode_kind",
            ["kind"],
            unique=False,
        )
        batch_op.create_index(
            "ix_inventory_item_barcode_active",
            ["active"],
            unique=False,
        )

    with op.batch_alter_table(
        "inventory_movement",
        schema=None,
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "source_location_id",
                sa.Integer(),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "target_location_id",
                sa.Integer(),
                nullable=True,
            )
        )
        batch_op.create_foreign_key(
            "fk_inventory_movement_source_location",
            "inventory_location",
            ["source_location_id"],
            ["id"],
        )
        batch_op.create_foreign_key(
            "fk_inventory_movement_target_location",
            "inventory_location",
            ["target_location_id"],
            ["id"],
        )
        batch_op.create_index(
            "ix_inventory_movement_source_location_id",
            ["source_location_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_inventory_movement_target_location_id",
            ["target_location_id"],
            unique=False,
        )

    connection = op.get_bind()

    connection.execute(
        sa.text(
            """
            INSERT INTO inventory_location (
                code,
                name,
                location_type,
                is_default,
                active,
                created_at
            )
            VALUES (
                'HAUPTLAGER',
                'Hauptlager',
                'warehouse',
                1,
                1,
                CURRENT_TIMESTAMP
            )
            """
        )
    )

    location_id = connection.execute(
        sa.text(
            """
            SELECT id
            FROM inventory_location
            WHERE code = 'HAUPTLAGER'
            """
        )
    ).scalar_one()

    connection.execute(
        sa.text(
            """
            INSERT INTO inventory_stock (
                item_id,
                location_id,
                quantity
            )
            SELECT
                id,
                :location_id,
                stock_quantity
            FROM inventory_item
            """
        ),
        {
            "location_id": location_id,
        },
    )

    items = connection.execute(
        sa.text(
            """
            SELECT
                id,
                ean,
                qr_code_payload
            FROM inventory_item
            ORDER BY id
            """
        )
    ).mappings().all()

    used_codes = set()

    for item in items:
        values = [
            (
                item["qr_code_payload"],
                "qr",
                "Artikeltyp",
            ),
            (
                item["ean"],
                "ean",
                "EAN",
            ),
        ]

        for code, kind, description in values:
            normalized_code = str(
                code or ""
            ).strip()

            if (
                not normalized_code
                or normalized_code in used_codes
            ):
                continue

            connection.execute(
                sa.text(
                    """
                    INSERT INTO inventory_item_barcode (
                        item_id,
                        code,
                        kind,
                        description,
                        active,
                        created_at
                    )
                    VALUES (
                        :item_id,
                        :code,
                        :kind,
                        :description,
                        1,
                        CURRENT_TIMESTAMP
                    )
                    """
                ),
                {
                    "item_id": item["id"],
                    "code": normalized_code,
                    "kind": kind,
                    "description": description,
                },
            )

            used_codes.add(normalized_code)


def downgrade():
    with op.batch_alter_table(
        "inventory_movement",
        schema=None,
    ) as batch_op:
        batch_op.drop_index(
            "ix_inventory_movement_target_location_id"
        )
        batch_op.drop_index(
            "ix_inventory_movement_source_location_id"
        )
        batch_op.drop_constraint(
            "fk_inventory_movement_target_location",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "fk_inventory_movement_source_location",
            type_="foreignkey",
        )
        batch_op.drop_column(
            "target_location_id"
        )
        batch_op.drop_column(
            "source_location_id"
        )

    op.drop_table(
        "inventory_item_barcode"
    )
    op.drop_table(
        "inventory_stock"
    )
    op.drop_table(
        "inventory_location"
    )