from alembic import op
import sqlalchemy as sa


revision = "b7e8f9a0b1c2"
down_revision = "a6d7e8f9a0b1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "wowi_department_type",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("erp_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_wowi_department_type_erp_id"),
        "wowi_department_type",
        ["erp_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_wowi_department_type_name"),
        "wowi_department_type",
        ["name"],
        unique=False,
    )

    op.create_table(
        "craft_activity_department_type_mapping",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "craft_activity_id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "department_type_id",
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
            name="fk_craft_department_type_mapping_craft_activity",
        ),
        sa.ForeignKeyConstraint(
            ["department_type_id"],
            ["wowi_department_type.id"],
            name="fk_craft_department_type_mapping_department_type",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["user_account.id"],
            name="fk_craft_department_type_mapping_updated_by",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("craft_activity_id"),
    )
    op.create_index(
        op.f(
            "ix_craft_activity_department_type_mapping_"
            "craft_activity_id"
        ),
        "craft_activity_department_type_mapping",
        ["craft_activity_id"],
        unique=True,
    )
    op.create_index(
        op.f(
            "ix_craft_activity_department_type_mapping_"
            "department_type_id"
        ),
        "craft_activity_department_type_mapping",
        ["department_type_id"],
        unique=False,
    )
    op.create_index(
        op.f(
            "ix_craft_activity_department_type_mapping_"
            "updated_by_user_id"
        ),
        "craft_activity_department_type_mapping",
        ["updated_by_user_id"],
        unique=False,
    )

    op.create_table(
        "wowi_economic_unit",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("erp_id", sa.Integer(), nullable=False),
        sa.Column(
            "id_num",
            sa.String(length=100),
            nullable=True,
        ),
        sa.Column(
            "name",
            sa.String(length=255),
            nullable=True,
        ),
        sa.Column(
            "location",
            sa.String(length=255),
            nullable=True,
        ),
        sa.Column(
            "company_code_erp_id",
            sa.Integer(),
            nullable=True,
        ),
        sa.Column(
            "company_code_code",
            sa.String(length=40),
            nullable=True,
        ),
        sa.Column(
            "universal_responsibility",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column(
            "universal_responsible_official_erp_id",
            sa.Integer(),
            nullable=True,
        ),
        sa.Column(
            "universal_responsible_person_erp_id",
            sa.Integer(),
            nullable=True,
        ),
        sa.Column(
            "active",
            sa.Boolean(),
            nullable=False,
        ),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    for column in (
        "erp_id",
        "id_num",
        "name",
        "company_code_erp_id",
        "company_code_code",
        "universal_responsible_official_erp_id",
        "universal_responsible_person_erp_id",
    ):
        op.create_index(
            op.f(f"ix_wowi_economic_unit_{column}"),
            "wowi_economic_unit",
            [column],
            unique=column == "erp_id",
        )

    with op.batch_alter_table(
        "commission",
        schema=None,
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "economic_unit_erp_id",
                sa.Integer(),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "economic_unit_idnum",
                sa.String(length=100),
                nullable=True,
            )
        )
        batch_op.create_index(
            batch_op.f(
                "ix_commission_economic_unit_erp_id"
            ),
            ["economic_unit_erp_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f(
                "ix_commission_economic_unit_idnum"
            ),
            ["economic_unit_idnum"],
            unique=False,
        )

    with op.batch_alter_table(
        "wowi_building",
        schema=None,
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "economic_unit_erp_id",
                sa.Integer(),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "economic_unit_id",
                sa.Integer(),
                nullable=True,
            )
        )
        batch_op.create_foreign_key(
            "fk_wowi_building_economic_unit",
            "wowi_economic_unit",
            ["economic_unit_id"],
            ["id"],
        )
        batch_op.create_index(
            batch_op.f(
                "ix_wowi_building_economic_unit_erp_id"
            ),
            ["economic_unit_erp_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f(
                "ix_wowi_building_economic_unit_id"
            ),
            ["economic_unit_id"],
            unique=False,
        )

    with op.batch_alter_table(
        "wowi_use_unit",
        schema=None,
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "economic_unit_erp_id",
                sa.Integer(),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "economic_unit_id",
                sa.Integer(),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "active",
                sa.Boolean(),
                server_default=sa.true(),
                nullable=False,
            )
        )
        batch_op.create_foreign_key(
            "fk_wowi_use_unit_economic_unit",
            "wowi_economic_unit",
            ["economic_unit_id"],
            ["id"],
        )
        batch_op.create_index(
            batch_op.f(
                "ix_wowi_use_unit_economic_unit_erp_id"
            ),
            ["economic_unit_erp_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f(
                "ix_wowi_use_unit_economic_unit_id"
            ),
            ["economic_unit_id"],
            unique=False,
        )

    op.create_table(
        "wowi_economic_unit_jurisdiction",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("erp_id", sa.Integer(), nullable=False),
        sa.Column(
            "economic_unit_id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "department_erp_id",
            sa.Integer(),
            nullable=True,
        ),
        sa.Column(
            "department_name",
            sa.String(length=255),
            nullable=True,
        ),
        sa.Column(
            "department_type_erp_id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "department_type_name",
            sa.String(length=255),
            nullable=True,
        ),
        sa.Column(
            "responsible_official_erp_id",
            sa.Integer(),
            nullable=True,
        ),
        sa.Column(
            "responsible_person_erp_id",
            sa.Integer(),
            nullable=True,
        ),
        sa.Column(
            "responsible_name",
            sa.String(length=255),
            nullable=True,
        ),
        sa.Column(
            "main_jurisdiction",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column(
            "active",
            sa.Boolean(),
            nullable=False,
        ),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["economic_unit_id"],
            ["wowi_economic_unit.id"],
            name="fk_wowi_jurisdiction_economic_unit",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    for column in (
        "erp_id",
        "economic_unit_id",
        "department_erp_id",
        "department_type_erp_id",
        "responsible_official_erp_id",
        "responsible_person_erp_id",
    ):
        op.create_index(
            op.f(
                "ix_wowi_economic_unit_jurisdiction_"
                f"{column}"
            ),
            "wowi_economic_unit_jurisdiction",
            [column],
            unique=column == "erp_id",
        )

    op.create_table(
        "wowi_license_agreement",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("erp_id", sa.Integer(), nullable=False),
        sa.Column(
            "id_num",
            sa.String(length=100),
            nullable=True,
        ),
        sa.Column(
            "use_unit_erp_id",
            sa.Integer(),
            nullable=True,
        ),
        sa.Column(
            "use_unit_id",
            sa.Integer(),
            nullable=True,
        ),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column(
            "is_vacancy",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column(
            "active",
            sa.Boolean(),
            nullable=False,
        ),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["use_unit_id"],
            ["wowi_use_unit.id"],
            name="fk_wowi_license_agreement_use_unit",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    for column in (
        "erp_id",
        "id_num",
        "use_unit_erp_id",
        "use_unit_id",
        "is_vacancy",
    ):
        op.create_index(
            op.f(
                f"ix_wowi_license_agreement_{column}"
            ),
            "wowi_license_agreement",
            [column],
            unique=column == "erp_id",
        )

    op.create_table(
        "wowi_contractor",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("erp_id", sa.Integer(), nullable=False),
        sa.Column(
            "license_agreement_erp_id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "license_agreement_id",
            sa.Integer(),
            nullable=True,
        ),
        sa.Column(
            "person_erp_id",
            sa.Integer(),
            nullable=True,
        ),
        sa.Column(
            "display_name",
            sa.String(length=255),
            nullable=False,
        ),
        sa.Column(
            "contractual_use_from",
            sa.Date(),
            nullable=True,
        ),
        sa.Column(
            "contractual_use_to",
            sa.Date(),
            nullable=True,
        ),
        sa.Column(
            "active",
            sa.Boolean(),
            nullable=False,
        ),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["license_agreement_id"],
            ["wowi_license_agreement.id"],
            name="fk_wowi_contractor_license_agreement",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    for column in (
        "erp_id",
        "license_agreement_erp_id",
        "license_agreement_id",
        "person_erp_id",
        "display_name",
    ):
        op.create_index(
            op.f(f"ix_wowi_contractor_{column}"),
            "wowi_contractor",
            [column],
            unique=column == "erp_id",
        )


def downgrade():
    op.drop_table("wowi_contractor")
    op.drop_table("wowi_license_agreement")
    op.drop_table(
        "wowi_economic_unit_jurisdiction"
    )

    with op.batch_alter_table(
        "wowi_use_unit",
        schema=None,
    ) as batch_op:
        batch_op.drop_index(
            batch_op.f(
                "ix_wowi_use_unit_economic_unit_id"
            )
        )
        batch_op.drop_index(
            batch_op.f(
                "ix_wowi_use_unit_economic_unit_erp_id"
            )
        )
        batch_op.drop_constraint(
            "fk_wowi_use_unit_economic_unit",
            type_="foreignkey",
        )
        batch_op.drop_column("active")
        batch_op.drop_column("economic_unit_id")
        batch_op.drop_column(
            "economic_unit_erp_id"
        )

    with op.batch_alter_table(
        "wowi_building",
        schema=None,
    ) as batch_op:
        batch_op.drop_index(
            batch_op.f(
                "ix_wowi_building_economic_unit_id"
            )
        )
        batch_op.drop_index(
            batch_op.f(
                "ix_wowi_building_economic_unit_erp_id"
            )
        )
        batch_op.drop_constraint(
            "fk_wowi_building_economic_unit",
            type_="foreignkey",
        )
        batch_op.drop_column("economic_unit_id")
        batch_op.drop_column(
            "economic_unit_erp_id"
        )

    with op.batch_alter_table(
        "commission",
        schema=None,
    ) as batch_op:
        batch_op.drop_index(
            batch_op.f(
                "ix_commission_economic_unit_idnum"
            )
        )
        batch_op.drop_index(
            batch_op.f(
                "ix_commission_economic_unit_erp_id"
            )
        )
        batch_op.drop_column(
            "economic_unit_idnum"
        )
        batch_op.drop_column(
            "economic_unit_erp_id"
        )

    op.drop_table("wowi_economic_unit")
    op.drop_table(
        "craft_activity_department_type_mapping"
    )
    op.drop_table("wowi_department_type")