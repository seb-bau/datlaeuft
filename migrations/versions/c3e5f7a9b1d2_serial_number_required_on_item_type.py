from alembic import op
import sqlalchemy as sa


revision = "c3e5f7a9b1d2"
down_revision = "b8d0f2a4c6e8"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table(
        "inventory_item_type"
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "serial_number_required",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            )
        )

    op.execute(
        sa.text(
            """
            UPDATE inventory_item_type
            SET serial_number_required = COALESCE(
                (
                    SELECT inventory_item_group.serial_number_required
                    FROM inventory_item_group
                    WHERE inventory_item_group.id
                        = inventory_item_type.group_id
                ),
                0
            )
            """
        )
    )

    with op.batch_alter_table(
        "inventory_item_group"
    ) as batch_op:
        batch_op.drop_column(
            "serial_number_required"
        )


def downgrade():
    with op.batch_alter_table(
        "inventory_item_group"
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "serial_number_required",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            )
        )

    op.execute(
        sa.text(
            """
            UPDATE inventory_item_group
            SET serial_number_required = CASE
                WHEN EXISTS (
                    SELECT 1
                    FROM inventory_item_type
                    WHERE inventory_item_type.group_id
                        = inventory_item_group.id
                    AND inventory_item_type.serial_number_required = 1
                )
                THEN 1
                ELSE 0
            END
            """
        )
    )

    with op.batch_alter_table(
        "inventory_item_type"
    ) as batch_op:
        batch_op.drop_column(
            "serial_number_required"
        )