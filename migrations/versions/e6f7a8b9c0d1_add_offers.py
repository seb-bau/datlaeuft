from alembic import op
import sqlalchemy as sa


revision = "e6f7a8b9c0d1"
down_revision = "d5e6f7a8b9c0"
branch_labels = None
depends_on = None


def table_exists(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return inspector.has_table(table_name)


def column_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())

    if not inspector.has_table(table_name):
        return set()

    return {
        column["name"]
        for column in inspector.get_columns(
            table_name
        )
    }


def index_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())

    if not inspector.has_table(table_name):
        return set()

    return {
        index["name"]
        for index in inspector.get_indexes(
            table_name
        )
        if index.get("name")
    }


def create_index(
    table_name: str,
    index_name: str,
    columns: list[str],
) -> None:
    if index_name in index_names(table_name):
        return

    op.create_index(
        index_name,
        table_name,
        columns,
        unique=False,
    )


def upgrade() -> None:
    user_columns = column_names(
        "user_account"
    )

    if "can_manage_offers" not in user_columns:
        op.add_column(
            "user_account",
            sa.Column(
                "can_manage_offers",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            ),
        )

    company_code_columns = column_names(
        "wowi_company_code"
    )

    if (
        "offer_number_mask"
        not in company_code_columns
    ):
        op.add_column(
            "wowi_company_code",
            sa.Column(
                "offer_number_mask",
                sa.String(length=160),
                server_default=(
                    "<company_code>-ANG-"
                    "<year>-<counter>"
                ),
                nullable=False,
            ),
        )

    if "offer_counter" not in company_code_columns:
        op.add_column(
            "wowi_company_code",
            sa.Column(
                "offer_counter",
                sa.Integer(),
                server_default="0",
                nullable=False,
            ),
        )

    if (
        "offer_counter_year"
        not in company_code_columns
    ):
        op.add_column(
            "wowi_company_code",
            sa.Column(
                "offer_counter_year",
                sa.Integer(),
                nullable=True,
            ),
        )

    if (
        "offer_counter_digits"
        not in company_code_columns
    ):
        op.add_column(
            "wowi_company_code",
            sa.Column(
                "offer_counter_digits",
                sa.Integer(),
                server_default="5",
                nullable=False,
            ),
        )

    if not table_exists("offer"):
        op.create_table(
            "offer",
            sa.Column(
                "id",
                sa.Integer(),
                primary_key=True,
            ),
            sa.Column(
                "public_id",
                sa.String(length=36),
                nullable=False,
            ),
            sa.Column(
                "status",
                sa.String(length=40),
                server_default="draft",
                nullable=False,
            ),
            sa.Column(
                "source_offer_id",
                sa.Integer(),
                nullable=True,
            ),
            sa.Column(
                "company_code_id",
                sa.Integer(),
                nullable=False,
            ),
            sa.Column(
                "company_code_erp_id",
                sa.Integer(),
                nullable=False,
            ),
            sa.Column(
                "company_code_code",
                sa.String(length=40),
                nullable=False,
            ),
            sa.Column(
                "craft_activity_id",
                sa.Integer(),
                nullable=True,
            ),
            sa.Column(
                "offer_number",
                sa.String(length=160),
                nullable=True,
            ),
            sa.Column(
                "offer_date",
                sa.Date(),
                nullable=True,
            ),
            sa.Column(
                "valid_until",
                sa.Date(),
                nullable=True,
            ),
            sa.Column(
                "title",
                sa.String(length=255),
                nullable=True,
            ),
            sa.Column(
                "object_type",
                sa.String(length=40),
                nullable=True,
            ),
            sa.Column(
                "object_id",
                sa.Integer(),
                nullable=True,
            ),
            sa.Column(
                "economic_unit_erp_id",
                sa.Integer(),
                nullable=True,
            ),
            sa.Column(
                "building_erp_id",
                sa.Integer(),
                nullable=True,
            ),
            sa.Column(
                "use_unit_erp_id",
                sa.Integer(),
                nullable=True,
            ),
            sa.Column(
                "license_agreement_erp_id",
                sa.Integer(),
                nullable=True,
            ),
            sa.Column(
                "license_agreement_idnum",
                sa.String(length=100),
                nullable=True,
            ),
            sa.Column(
                "object_address",
                sa.String(length=500),
                nullable=True,
            ),
            sa.Column(
                "object_location",
                sa.String(length=255),
                nullable=True,
            ),
            sa.Column(
                "tenant_name",
                sa.String(length=255),
                nullable=True,
            ),
            sa.Column(
                "recipient_type",
                sa.String(length=40),
                server_default="private",
                nullable=False,
            ),
            sa.Column(
                "recipient_person_erp_id",
                sa.Integer(),
                nullable=True,
            ),
            sa.Column(
                "recipient_name",
                sa.String(length=255),
                nullable=True,
            ),
            sa.Column(
                "recipient_name_addition",
                sa.String(length=255),
                nullable=True,
            ),
            sa.Column(
                "recipient_street",
                sa.String(length=255),
                nullable=True,
            ),
            sa.Column(
                "recipient_postal_code",
                sa.String(length=20),
                nullable=True,
            ),
            sa.Column(
                "recipient_town",
                sa.String(length=255),
                nullable=True,
            ),
            sa.Column(
                "recipient_country_code",
                sa.String(length=2),
                server_default="DE",
                nullable=False,
            ),
            sa.Column(
                "recipient_email",
                sa.String(length=255),
                nullable=True,
            ),
            sa.Column(
                "recipient_vat_id",
                sa.String(length=80),
                nullable=True,
            ),
            sa.Column(
                "seller_name",
                sa.String(length=255),
                nullable=True,
            ),
            sa.Column(
                "seller_street",
                sa.String(length=255),
                nullable=True,
            ),
            sa.Column(
                "seller_postal_code",
                sa.String(length=20),
                nullable=True,
            ),
            sa.Column(
                "seller_town",
                sa.String(length=255),
                nullable=True,
            ),
            sa.Column(
                "seller_country_code",
                sa.String(length=2),
                server_default="DE",
                nullable=False,
            ),
            sa.Column(
                "seller_vat_id",
                sa.String(length=80),
                nullable=True,
            ),
            sa.Column(
                "seller_tax_number",
                sa.String(length=80),
                nullable=True,
            ),
            sa.Column(
                "seller_email",
                sa.String(length=255),
                nullable=True,
            ),
            sa.Column(
                "seller_phone",
                sa.String(length=80),
                nullable=True,
            ),
            sa.Column(
                "currency_code",
                sa.String(length=3),
                server_default="EUR",
                nullable=False,
            ),
            sa.Column(
                "introductory_text",
                sa.Text(),
                nullable=True,
            ),
            sa.Column(
                "concluding_text",
                sa.Text(),
                nullable=True,
            ),
            sa.Column(
                "total_net",
                sa.Numeric(14, 2),
                server_default="0",
                nullable=False,
            ),
            sa.Column(
                "total_vat",
                sa.Numeric(14, 2),
                server_default="0",
                nullable=False,
            ),
            sa.Column(
                "total_gross",
                sa.Numeric(14, 2),
                server_default="0",
                nullable=False,
            ),
            sa.Column(
                "document_filename",
                sa.String(length=255),
                nullable=True,
            ),
            sa.Column(
                "document_sha256",
                sa.String(length=64),
                nullable=True,
            ),
            sa.Column(
                "commission_id",
                sa.Integer(),
                nullable=True,
            ),
            sa.Column(
                "invoice_id",
                sa.Integer(),
                nullable=True,
            ),
            sa.Column(
                "status_note",
                sa.Text(),
                nullable=True,
            ),
            sa.Column(
                "dms_archive_status",
                sa.String(length=30),
                server_default="not_archived",
                nullable=False,
            ),
            sa.Column(
                "dms_archive_key",
                sa.String(length=180),
                nullable=True,
            ),
            sa.Column(
                "dms_document_id",
                sa.String(length=255),
                nullable=True,
            ),
            sa.Column(
                "dms_archive_pdf_sha256",
                sa.String(length=64),
                nullable=True,
            ),
            sa.Column(
                "dms_archive_attempted_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
            sa.Column(
                "dms_archived_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
            sa.Column(
                "dms_archived_by_user_id",
                sa.Integer(),
                nullable=True,
            ),
            sa.Column(
                "dms_archive_error",
                sa.Text(),
                nullable=True,
            ),
            sa.Column(
                "created_by_user_id",
                sa.Integer(),
                nullable=False,
            ),
            sa.Column(
                "released_by_user_id",
                sa.Integer(),
                nullable=True,
            ),
            sa.Column(
                "accepted_by_user_id",
                sa.Integer(),
                nullable=True,
            ),
            sa.Column(
                "rejected_by_user_id",
                sa.Integer(),
                nullable=True,
            ),
            sa.Column(
                "canceled_by_user_id",
                sa.Integer(),
                nullable=True,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
            ),
            sa.Column(
                "released_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
            sa.Column(
                "accepted_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
            sa.Column(
                "rejected_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
            sa.Column(
                "canceled_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
            sa.Column(
                "invoiced_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
            sa.ForeignKeyConstraint(
                ["source_offer_id"],
                ["offer.id"],
            ),
            sa.ForeignKeyConstraint(
                ["company_code_id"],
                ["wowi_company_code.id"],
            ),
            sa.ForeignKeyConstraint(
                ["craft_activity_id"],
                ["craft_activity.id"],
            ),
            sa.ForeignKeyConstraint(
                ["commission_id"],
                ["commission.id"],
            ),
            sa.ForeignKeyConstraint(
                ["invoice_id"],
                ["invoice.id"],
            ),
            sa.ForeignKeyConstraint(
                ["dms_archived_by_user_id"],
                ["user_account.id"],
            ),
            sa.ForeignKeyConstraint(
                ["created_by_user_id"],
                ["user_account.id"],
            ),
            sa.ForeignKeyConstraint(
                ["released_by_user_id"],
                ["user_account.id"],
            ),
            sa.ForeignKeyConstraint(
                ["accepted_by_user_id"],
                ["user_account.id"],
            ),
            sa.ForeignKeyConstraint(
                ["rejected_by_user_id"],
                ["user_account.id"],
            ),
            sa.ForeignKeyConstraint(
                ["canceled_by_user_id"],
                ["user_account.id"],
            ),
            sa.UniqueConstraint(
                "public_id",
                name="uq_offer_public_id",
            ),
            sa.UniqueConstraint(
                "offer_number",
                name="uq_offer_number",
            ),
            sa.UniqueConstraint(
                "commission_id",
                name="uq_offer_commission_id",
            ),
            sa.UniqueConstraint(
                "invoice_id",
                name="uq_offer_invoice_id",
            ),
            sa.UniqueConstraint(
                "dms_archive_key",
                name="uq_offer_dms_archive_key",
            ),
            sa.UniqueConstraint(
                "dms_document_id",
                name="uq_offer_dms_document_id",
            ),
        )

    if not table_exists("offer_position"):
        op.create_table(
            "offer_position",
            sa.Column(
                "id",
                sa.Integer(),
                primary_key=True,
            ),
            sa.Column(
                "offer_id",
                sa.Integer(),
                nullable=False,
            ),
            sa.Column(
                "position_index",
                sa.Integer(),
                nullable=False,
            ),
            sa.Column(
                "source_type",
                sa.String(length=40),
                server_default="free",
                nullable=False,
            ),
            sa.Column(
                "task_template_id",
                sa.Integer(),
                nullable=True,
            ),
            sa.Column(
                "description",
                sa.Text(),
                nullable=False,
            ),
            sa.Column(
                "details",
                sa.Text(),
                nullable=True,
            ),
            sa.Column(
                "quantity",
                sa.Numeric(12, 4),
                server_default="1",
                nullable=False,
            ),
            sa.Column(
                "unit",
                sa.String(length=40),
                server_default="Stk",
                nullable=False,
            ),
            sa.Column(
                "unit_price_net",
                sa.Numeric(14, 4),
                server_default="0",
                nullable=False,
            ),
            sa.Column(
                "vat_rate",
                sa.Numeric(5, 2),
                server_default="19",
                nullable=False,
            ),
            sa.Column(
                "net_amount",
                sa.Numeric(14, 2),
                server_default="0",
                nullable=False,
            ),
            sa.Column(
                "vat_amount",
                sa.Numeric(14, 2),
                server_default="0",
                nullable=False,
            ),
            sa.Column(
                "gross_amount",
                sa.Numeric(14, 2),
                server_default="0",
                nullable=False,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["offer_id"],
                ["offer.id"],
            ),
            sa.ForeignKeyConstraint(
                ["task_template_id"],
                ["time_task_template.id"],
            ),
            sa.UniqueConstraint(
                "offer_id",
                "position_index",
                name="uq_offer_position_index",
            ),
        )

    if not table_exists("offer_craft_fallback"):
        op.create_table(
            "offer_craft_fallback",
            sa.Column(
                "id",
                sa.Integer(),
                primary_key=True,
            ),
            sa.Column(
                "craft_activity_id",
                sa.Integer(),
                nullable=False,
            ),
            sa.Column(
                "task_template_id",
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
            ),
            sa.ForeignKeyConstraint(
                ["task_template_id"],
                ["time_task_template.id"],
            ),
            sa.ForeignKeyConstraint(
                ["updated_by_user_id"],
                ["user_account.id"],
            ),
            sa.UniqueConstraint(
                "craft_activity_id",
                name=(
                    "uq_offer_craft_fallback_"
                    "craft_activity"
                ),
            ),
            sa.UniqueConstraint(
                "task_template_id",
                name=(
                    "uq_offer_craft_fallback_"
                    "task_template"
                ),
            ),
        )

    offer_indexes = {
        "ix_offer_status": ["status"],
        "ix_offer_source_offer_id": [
            "source_offer_id"
        ],
        "ix_offer_company_code_id": [
            "company_code_id"
        ],
        "ix_offer_craft_activity_id": [
            "craft_activity_id"
        ],
        "ix_offer_valid_until": ["valid_until"],
        "ix_offer_object_reference": [
            "object_type",
            "object_id",
        ],
        "ix_offer_created_at": ["created_at"],
        "ix_offer_dms_archive_status": [
            "dms_archive_status"
        ],
    }

    for index_name, columns in (
        offer_indexes.items()
    ):
        create_index(
            "offer",
            index_name,
            columns,
        )

    create_index(
        "offer_position",
        "ix_offer_position_offer_id",
        ["offer_id"],
    )
    create_index(
        "offer_position",
        "ix_offer_position_task_template_id",
        ["task_template_id"],
    )
    create_index(
        "offer_position",
        "ix_offer_position_source_type",
        ["source_type"],
    )
    create_index(
        "offer_craft_fallback",
        (
            "ix_offer_craft_fallback_"
            "craft_activity_id"
        ),
        ["craft_activity_id"],
    )
    create_index(
        "offer_craft_fallback",
        (
            "ix_offer_craft_fallback_"
            "task_template_id"
        ),
        ["task_template_id"],
    )


def downgrade() -> None:
    if table_exists("offer_craft_fallback"):
        op.drop_table("offer_craft_fallback")

    if table_exists("offer_position"):
        op.drop_table("offer_position")

    if table_exists("offer"):
        op.drop_table("offer")

    company_code_columns = column_names(
        "wowi_company_code"
    )

    with op.batch_alter_table(
        "wowi_company_code",
        schema=None,
    ) as batch_op:
        for column_name in (
            "offer_counter_digits",
            "offer_counter_year",
            "offer_counter",
            "offer_number_mask",
        ):
            if column_name in company_code_columns:
                batch_op.drop_column(column_name)

    user_columns = column_names(
        "user_account"
    )

    if "can_manage_offers" in user_columns:
        with op.batch_alter_table(
            "user_account",
            schema=None,
        ) as batch_op:
            batch_op.drop_column(
                "can_manage_offers"
            )