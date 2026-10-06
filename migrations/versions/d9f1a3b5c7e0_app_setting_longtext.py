from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "d9f1a3b5c7e0"
down_revision = "c6d8e0f2a4b6"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    if connection.dialect.name not in {"mysql", "mariadb"}:
        return

    op.alter_column(
        "app_setting",
        "value_text",
        existing_type=sa.Text(),
        type_=mysql.LONGTEXT(),
        existing_nullable=True,
    )


def downgrade():
    connection = op.get_bind()
    if connection.dialect.name not in {"mysql", "mariadb"}:
        return

    oversized_value = connection.execute(
        sa.text(
            "SELECT 1 FROM app_setting "
            "WHERE OCTET_LENGTH(value_text) > 65535 LIMIT 1"
        )
    ).scalar()
    if oversized_value is not None:
        raise RuntimeError(
            "Die Spalte kann nicht auf TEXT verkleinert werden, "
            "weil gespeicherte Einstellungen größer als 65.535 Byte sind."
        )

    op.alter_column(
        "app_setting",
        "value_text",
        existing_type=mysql.LONGTEXT(),
        type_=sa.Text(),
        existing_nullable=True,
    )