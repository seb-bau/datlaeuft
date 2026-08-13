from alembic import op
import sqlalchemy as sa


revision = "d5e6f7a8b9c0"
down_revision = "c4d5e6f7a8b9"
branch_labels = None
depends_on = None


def _column_names() -> set[str]:
    inspector = sa.inspect(
        op.get_bind()
    )

    return {
        column["name"]
        for column in inspector.get_columns(
            "invoice"
        )
    }


def _foreign_keys() -> list[dict]:
    inspector = sa.inspect(
        op.get_bind()
    )

    return inspector.get_foreign_keys(
        "invoice"
    )


def _unique_constraints() -> list[dict]:
    inspector = sa.inspect(
        op.get_bind()
    )

    return inspector.get_unique_constraints(
        "invoice"
    )


def _index_names() -> set[str]:
    inspector = sa.inspect(
        op.get_bind()
    )

    return {
        index["name"]
        for index in inspector.get_indexes(
            "invoice"
        )
        if index.get("name")
    }


def _has_foreign_key(
    column_name: str,
    referred_table: str,
) -> bool:
    return any(
        foreign_key.get(
            "constrained_columns"
        ) == [column_name]
        and foreign_key.get(
            "referred_table"
        ) == referred_table
        for foreign_key in _foreign_keys()
    )


def _has_unique_constraint(
    column_name: str,
) -> bool:
    return any(
        constraint.get(
            "column_names"
        ) == [column_name]
        for constraint in (
            _unique_constraints()
        )
    )


def upgrade():
    columns = _column_names()

    if "invoice_kind" not in columns:
        op.add_column(
            "invoice",
            sa.Column(
                "invoice_kind",
                sa.String(length=30),
                server_default="invoice",
                nullable=False,
            ),
        )

    if "original_invoice_id" not in columns:
        op.add_column(
            "invoice",
            sa.Column(
                "original_invoice_id",
                sa.Integer(),
                nullable=True,
            ),
        )

    if "cancellation_reason" not in columns:
        op.add_column(
            "invoice",
            sa.Column(
                "cancellation_reason",
                sa.Text(),
                nullable=True,
            ),
        )

    if "canceled_at" not in columns:
        op.add_column(
            "invoice",
            sa.Column(
                "canceled_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )

    if "canceled_by_user_id" not in columns:
        op.add_column(
            "invoice",
            sa.Column(
                "canceled_by_user_id",
                sa.Integer(),
                nullable=True,
            ),
        )

    create_original_foreign_key = (
        not _has_foreign_key(
            "original_invoice_id",
            "invoice",
        )
    )
    create_user_foreign_key = (
        not _has_foreign_key(
            "canceled_by_user_id",
            "user_account",
        )
    )
    create_original_unique = (
        not _has_unique_constraint(
            "original_invoice_id"
        )
    )

    if (
        create_original_foreign_key
        or create_user_foreign_key
        or create_original_unique
    ):
        with op.batch_alter_table(
            "invoice",
            schema=None,
        ) as batch_op:
            if create_original_foreign_key:
                batch_op.create_foreign_key(
                    "fk_invoice_original_invoice_id",
                    "invoice",
                    ["original_invoice_id"],
                    ["id"],
                )

            if create_user_foreign_key:
                batch_op.create_foreign_key(
                    "fk_invoice_canceled_by_user_id",
                    "user_account",
                    ["canceled_by_user_id"],
                    ["id"],
                )

            if create_original_unique:
                batch_op.create_unique_constraint(
                    "uq_invoice_original_invoice_id",
                    ["original_invoice_id"],
                )

    indexes = _index_names()

    if (
        "ix_invoice_invoice_kind"
        not in indexes
    ):
        op.create_index(
            "ix_invoice_invoice_kind",
            "invoice",
            ["invoice_kind"],
            unique=False,
        )

    if (
        "ix_invoice_original_invoice_id"
        not in indexes
    ):
        op.create_index(
            "ix_invoice_original_invoice_id",
            "invoice",
            ["original_invoice_id"],
            unique=False,
        )

    if (
        "ix_invoice_canceled_by_user_id"
        not in indexes
    ):
        op.create_index(
            "ix_invoice_canceled_by_user_id",
            "invoice",
            ["canceled_by_user_id"],
            unique=False,
        )


def downgrade():
    indexes = _index_names()

    if (
        "ix_invoice_canceled_by_user_id"
        in indexes
    ):
        op.drop_index(
            "ix_invoice_canceled_by_user_id",
            table_name="invoice",
        )

    if (
        "ix_invoice_original_invoice_id"
        in indexes
    ):
        op.drop_index(
            "ix_invoice_original_invoice_id",
            table_name="invoice",
        )

    if (
        "ix_invoice_invoice_kind"
        in indexes
    ):
        op.drop_index(
            "ix_invoice_invoice_kind",
            table_name="invoice",
        )

    drop_original_foreign_key = (
        _has_foreign_key(
            "original_invoice_id",
            "invoice",
        )
    )
    drop_original_unique = (
        _has_unique_constraint(
            "original_invoice_id"
        )
    )

    if (
        drop_original_foreign_key
        or drop_original_unique
    ):
        with op.batch_alter_table(
            "invoice",
            schema=None,
        ) as batch_op:
            if drop_original_unique:
                batch_op.drop_constraint(
                    "uq_invoice_original_invoice_id",
                    type_="unique",
                )

            if drop_original_foreign_key:
                batch_op.drop_constraint(
                    "fk_invoice_original_invoice_id",
                    type_="foreignkey",
                )

    columns = _column_names()

    if "original_invoice_id" in columns:
        op.drop_column(
            "invoice",
            "original_invoice_id",
        )

    if "invoice_kind" in columns:
        op.drop_column(
            "invoice",
            "invoice_kind",
        )