from alembic import op


revision = "d9e2c5f41a70"
down_revision = "c8d91a4e72f0"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_index(
        "ix_wowi_contractor_erp_id",
        table_name="wowi_contractor",
    )
    op.create_index(
        "ix_wowi_contractor_erp_id",
        "wowi_contractor",
        ["erp_id"],
        unique=False,
    )
    op.create_index(
        (
            "ux_wowi_contractor_"
            "erp_license_agreement"
        ),
        "wowi_contractor",
        [
            "erp_id",
            "license_agreement_erp_id",
        ],
        unique=True,
    )


def downgrade():
    op.drop_index(
        (
            "ux_wowi_contractor_"
            "erp_license_agreement"
        ),
        table_name="wowi_contractor",
    )
    op.drop_index(
        "ix_wowi_contractor_erp_id",
        table_name="wowi_contractor",
    )
    op.create_index(
        "ix_wowi_contractor_erp_id",
        "wowi_contractor",
        ["erp_id"],
        unique=True,
    )