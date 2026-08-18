from alembic import op
import sqlalchemy as sa


revision = "f7a8b9c0d1e2"
down_revision = "e6f7a8b9c0d1"
branch_labels = None
depends_on = None


def column_names(
    table_name: str,
) -> dict[str, dict]:
    inspector = sa.inspect(op.get_bind())

    if not inspector.has_table(table_name):
        return {}

    return {
        column["name"]: column
        for column in inspector.get_columns(
            table_name
        )
    }


def index_names(
    table_name: str,
) -> set[str]:
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


def upgrade() -> None:
    columns = column_names("time_entry")

    if "service_date" not in columns:
        op.add_column(
            "time_entry",
            sa.Column(
                "service_date",
                sa.Date(),
                nullable=True,
            ),
        )

    op.get_bind().execute(
        sa.text(
            "UPDATE time_entry "
            "SET service_date = DATE(created_at) "
            "WHERE service_date IS NULL"
        )
    )

    columns = column_names("time_entry")
    service_date_column = columns.get(
        "service_date"
    )

    if (
        service_date_column is not None
        and service_date_column.get(
            "nullable",
            True,
        )
    ):
        with op.batch_alter_table(
            "time_entry"
        ) as batch_op:
            batch_op.alter_column(
                "service_date",
                existing_type=sa.Date(),
                nullable=False,
            )

    if (
        "ix_time_entry_service_date"
        not in index_names("time_entry")
    ):
        op.create_index(
            "ix_time_entry_service_date",
            "time_entry",
            ["service_date"],
            unique=False,
        )


def downgrade() -> None:
    if (
        "ix_time_entry_service_date"
        in index_names("time_entry")
    ):
        op.drop_index(
            "ix_time_entry_service_date",
            table_name="time_entry",
        )

    if (
        "service_date"
        in column_names("time_entry")
    ):
        with op.batch_alter_table(
            "time_entry"
        ) as batch_op:
            batch_op.drop_column(
                "service_date"
            )