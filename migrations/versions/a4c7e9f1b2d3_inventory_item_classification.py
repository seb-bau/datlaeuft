from alembic import op
import sqlalchemy as sa


revision = "a4c7e9f1b2d3"
down_revision = (
    "f9a2b3c4d5e6",
    "f3a18c7d9e42",
)
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "inventory_item_group",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "code",
            sa.String(length=100),
            nullable=False,
        ),
        sa.Column(
            "name",
            sa.String(length=255),
            nullable=False,
        ),
        sa.Column(
            "serial_number_required",
            sa.Boolean(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "active",
            sa.Boolean(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "code",
            name="uq_inventory_item_group_code",
        ),
        sa.UniqueConstraint(
            "name",
            name="uq_inventory_item_group_name",
        ),
    )

    op.create_table(
        "inventory_item_type",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "group_id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "code",
            sa.String(length=100),
            nullable=False,
        ),
        sa.Column(
            "name",
            sa.String(length=255),
            nullable=False,
        ),
        sa.Column(
            "unit",
            sa.String(length=40),
            server_default="Stk",
            nullable=False,
        ),
        sa.Column(
            "active",
            sa.Boolean(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["inventory_item_group.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "code",
            name="uq_inventory_item_type_code",
        ),
        sa.UniqueConstraint(
            "group_id",
            "name",
            name="uq_inventory_item_type_group_name",
        ),
    )

    op.create_index(
        "ix_inventory_item_type_group_id",
        "inventory_item_type",
        ["group_id"],
    )

    with op.batch_alter_table(
        "inventory_item"
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "item_type_id",
                sa.Integer(),
                nullable=True,
            )
        )
        batch_op.create_index(
            "ix_inventory_item_item_type_id",
            ["item_type_id"],
            unique=False,
        )
        batch_op.create_foreign_key(
            "fk_inventory_item_item_type",
            "inventory_item_type",
            ["item_type_id"],
            ["id"],
            ondelete="SET NULL",
        )

    with op.batch_alter_table(
        "inventory_movement"
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "item_type_id_snapshot",
                sa.Integer(),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "item_group_id_snapshot",
                sa.Integer(),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "serial_number",
                sa.String(length=255),
                nullable=True,
            )
        )
        batch_op.create_index(
            "ix_inventory_movement_item_type_id_snapshot",
            ["item_type_id_snapshot"],
            unique=False,
        )
        batch_op.create_index(
            "ix_inventory_movement_item_group_id_snapshot",
            ["item_group_id_snapshot"],
            unique=False,
        )
        batch_op.create_index(
            "ix_inventory_movement_serial_number",
            ["serial_number"],
            unique=False,
        )
        batch_op.create_foreign_key(
            "fk_inventory_movement_item_type_snapshot",
            "inventory_item_type",
            ["item_type_id_snapshot"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            "fk_inventory_movement_item_group_snapshot",
            "inventory_item_group",
            ["item_group_id_snapshot"],
            ["id"],
            ondelete="SET NULL",
        )

    with op.batch_alter_table(
        "time_task_expected_material"
    ) as batch_op:
        batch_op.alter_column(
            "item_id",
            existing_type=sa.Integer(),
            nullable=True,
        )
        batch_op.add_column(
            sa.Column(
                "item_type_id",
                sa.Integer(),
                nullable=True,
            )
        )
        batch_op.create_index(
            "ix_time_task_expected_material_item_type_id",
            ["item_type_id"],
            unique=False,
        )
        batch_op.create_foreign_key(
            "fk_time_task_expected_material_item_type",
            "inventory_item_type",
            ["item_type_id"],
            ["id"],
        )
        batch_op.create_unique_constraint(
            "uq_time_task_expected_material_task_type",
            [
                "task_template_id",
                "item_type_id",
            ],
        )
        batch_op.create_check_constraint(
            "ck_time_task_expected_material_target",
            (
                "(item_id IS NOT NULL "
                "AND item_type_id IS NULL) "
                "OR "
                "(item_id IS NULL "
                "AND item_type_id IS NOT NULL)"
            ),
        )

    op.execute(
        sa.text(
            """
            UPDATE inventory_movement
            SET item_type_id_snapshot = (
                SELECT inventory_item.item_type_id
                FROM inventory_item
                WHERE inventory_item.id =
                    inventory_movement.item_id
            )
            WHERE item_type_id_snapshot IS NULL
            """
        )
    )


def downgrade():
    op.execute(
        """
        DELETE FROM time_task_expected_material
        WHERE item_id IS NULL
        """
    )

    with op.batch_alter_table(
        "time_task_expected_material"
    ) as batch_op:
        batch_op.drop_constraint(
            "ck_time_task_expected_material_target",
            type_="check",
        )
        batch_op.drop_constraint(
            "uq_time_task_expected_material_task_type",
            type_="unique",
        )
        batch_op.drop_constraint(
            "fk_time_task_expected_material_item_type",
            type_="foreignkey",
        )
        batch_op.drop_index(
            "ix_time_task_expected_material_item_type_id"
        )
        batch_op.drop_column(
            "item_type_id"
        )
        batch_op.alter_column(
            "item_id",
            existing_type=sa.Integer(),
            nullable=False,
        )

    with op.batch_alter_table(
        "inventory_movement"
    ) as batch_op:
        batch_op.drop_constraint(
            "fk_inventory_movement_item_group_snapshot",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "fk_inventory_movement_item_type_snapshot",
            type_="foreignkey",
        )
        batch_op.drop_index(
            "ix_inventory_movement_serial_number"
        )
        batch_op.drop_index(
            "ix_inventory_movement_item_group_id_snapshot"
        )
        batch_op.drop_index(
            "ix_inventory_movement_item_type_id_snapshot"
        )
        batch_op.drop_column(
            "serial_number"
        )
        batch_op.drop_column(
            "item_group_id_snapshot"
        )
        batch_op.drop_column(
            "item_type_id_snapshot"
        )

    with op.batch_alter_table(
        "inventory_item"
    ) as batch_op:
        batch_op.drop_constraint(
            "fk_inventory_item_item_type",
            type_="foreignkey",
        )
        batch_op.drop_index(
            "ix_inventory_item_item_type_id"
        )
        batch_op.drop_column(
            "item_type_id"
        )

    op.drop_index(
        "ix_inventory_item_type_group_id",
        table_name="inventory_item_type",
    )
    op.drop_table(
        "inventory_item_type"
    )
    op.drop_table(
        "inventory_item_group"
    )