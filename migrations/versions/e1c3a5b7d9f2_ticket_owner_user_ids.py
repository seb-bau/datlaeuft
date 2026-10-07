from alembic import op
import sqlalchemy as sa


revision = "e1c3a5b7d9f2"
down_revision = "d9f1a3b5c7e0"
branch_labels = None
depends_on = None


columns = (
    ("wowi_economic_unit_jurisdiction", "responsible_user_erp_id", sa.Integer(), True),
    ("wowi_economic_unit", "universal_responsible_user_erp_id", sa.Integer(), True),
    ("wowi_economic_unit", "universal_responsible_name", sa.String(255), False),
)


def upgrade():
    for table_name, column_name, column_type, indexed in columns:
        op.add_column(table_name, sa.Column(column_name, column_type, nullable=True))
        if indexed:
            op.create_index(
                f"ix_{table_name}_{column_name}", table_name, [column_name], unique=False
            )


def downgrade():
    for table_name, column_name, column_type, indexed in reversed(columns):
        if indexed:
            op.drop_index(f"ix_{table_name}_{column_name}", table_name=table_name)
        op.drop_column(table_name, column_name)