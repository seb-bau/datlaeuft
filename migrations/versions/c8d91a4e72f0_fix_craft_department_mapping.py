from datetime import datetime

from alembic import op
import sqlalchemy as sa


revision = "c8d91a4e72f0"
down_revision = "b7e8f9a0b1c2"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    table_names = set(
        sa.inspect(connection).get_table_names()
    )

    if "wowi_department" not in table_names:
        op.create_table(
            "wowi_department",
            sa.Column(
                "id",
                sa.Integer(),
                nullable=False,
            ),
            sa.Column(
                "erp_id",
                sa.Integer(),
                nullable=False,
            ),
            sa.Column(
                "name",
                sa.String(length=255),
                nullable=False,
            ),
            sa.Column(
                "active",
                sa.Boolean(),
                server_default=sa.true(),
                nullable=False,
            ),
            sa.Column(
                "synced_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            op.f("ix_wowi_department_erp_id"),
            "wowi_department",
            ["erp_id"],
            unique=True,
        )
        op.create_index(
            op.f("ix_wowi_department_name"),
            "wowi_department",
            ["name"],
            unique=False,
        )
    rows = connection.execute(
        sa.text(
            """
            SELECT
                department_erp_id,
                department_name,
                active,
                synced_at
            FROM wowi_economic_unit_jurisdiction
            WHERE department_erp_id IS NOT NULL
            ORDER BY id
            """
        )
    ).mappings()

    departments = {}

    for row in rows:
        erp_id = int(row["department_erp_id"])
        current = departments.get(erp_id)
        name = row["department_name"]
        synced_at = row["synced_at"]

        if isinstance(synced_at, str):
            try:
                synced_at = datetime.fromisoformat(
                    synced_at.replace(
                        "Z",
                        "+00:00",
                    )
                )
            except ValueError:
                synced_at = None

        if current is None:
            departments[erp_id] = {
                "erp_id": erp_id,
                "name": name or str(erp_id),
                "active": bool(row["active"]),
                "synced_at": synced_at,
            }
            continue

        if name:
            current["name"] = name

        current["active"] = (
                current["active"]
                or bool(row["active"])
        )

        if synced_at is not None:
            current["synced_at"] = synced_at

    department_table = sa.table(
        "wowi_department",
        sa.column("erp_id", sa.Integer()),
        sa.column(
            "name",
            sa.String(length=255),
        ),
        sa.column("active", sa.Boolean()),
        sa.column(
            "synced_at",
            sa.DateTime(timezone=True),
        ),
    )

    existing_department_ids = set(
        connection.execute(
            sa.text(
                "SELECT erp_id FROM wowi_department"
            )
        ).scalars()
    )

    new_departments = [
        item
        for erp_id, item in departments.items()
        if erp_id not in existing_department_ids
    ]

    if new_departments:
        op.bulk_insert(
            department_table,
            new_departments,
        )

    op.create_table(
        "craft_activity_department_mapping",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "craft_activity_id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "department_id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "updated_by_user_id",
            sa.Integer(),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["craft_activity_id"],
            ["craft_activity.id"],
            name=(
                "fk_craft_department_mapping_"
                "craft_activity"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["department_id"],
            ["wowi_department.id"],
            name=(
                "fk_craft_department_mapping_"
                "department"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["user_account.id"],
            name=(
                "fk_craft_department_mapping_"
                "updated_by"
            ),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("craft_activity_id"),
    )
    op.create_index(
        op.f(
            "ix_craft_activity_department_mapping_"
            "craft_activity_id"
        ),
        "craft_activity_department_mapping",
        ["craft_activity_id"],
        unique=True,
    )
    op.create_index(
        op.f(
            "ix_craft_activity_department_mapping_"
            "department_id"
        ),
        "craft_activity_department_mapping",
        ["department_id"],
        unique=False,
    )
    op.create_index(
        op.f(
            "ix_craft_activity_department_mapping_"
            "updated_by_user_id"
        ),
        "craft_activity_department_mapping",
        ["updated_by_user_id"],
        unique=False,
    )


def downgrade():
    op.drop_table(
        "craft_activity_department_mapping"
    )
    op.drop_table("wowi_department")