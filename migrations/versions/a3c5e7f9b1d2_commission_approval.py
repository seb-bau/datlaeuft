from alembic import op
import sqlalchemy as sa

revision = "a3c5e7f9b1d2"
down_revision = "f1b3d5e7a9c2"
branch_labels = None
depends_on = None


def table_schema(table_name):
    if op.get_context().as_sql:
        return set(), {}, {}
    inspector = sa.inspect(op.get_bind())
    return (
        {column["name"] for column in inspector.get_columns(table_name)},
        {index["name"]: index for index in inspector.get_indexes(table_name)},
        {foreign_key["name"]: foreign_key for foreign_key in inspector.get_foreign_keys(table_name)},
    )


def ensure_columns(table_name, columns):
    existing, _, _ = table_schema(table_name)
    missing = [column for column in columns if column.name not in existing]
    if missing:
        with op.batch_alter_table(table_name) as batch:
            for column in missing:
                batch.add_column(column)


def ensure_indexes(table_name, indexes):
    _, existing, _ = table_schema(table_name)
    for name, columns in indexes:
        if name in existing:
            if existing[name]["column_names"] != columns or existing[name].get("unique"):
                raise RuntimeError(f"Der vorhandene Index {name} entspricht nicht der erwarteten Struktur.")
        else:
            op.create_index(name, table_name, columns)


def ensure_foreign_keys(table_name, foreign_keys):
    _, _, existing = table_schema(table_name)
    missing = []
    for name, columns in foreign_keys:
        if name in existing:
            foreign_key = existing[name]
            if (foreign_key["constrained_columns"] != columns or foreign_key["referred_table"] != "user_account"
                    or foreign_key["referred_columns"] != ["id"]
                    or str((foreign_key.get("options") or {}).get("ondelete") or "RESTRICT").upper() not in {"RESTRICT", "NO ACTION"}):
                raise RuntimeError(f"Der vorhandene Fremdschlüssel {name} entspricht nicht der erwarteten Struktur.")
        else:
            missing.append((name, columns))
    if missing:
        with op.batch_alter_table(table_name) as batch:
            for name, columns in missing:
                batch.create_foreign_key(name, "user_account", columns, ["id"], ondelete="RESTRICT")


def upgrade():
    user_columns = [f"{assignment}_commission_approver_user_id" for assignment in ("internal", "external")]
    ensure_columns("user_account", [sa.Column(column, sa.Integer()) for column in user_columns])
    ensure_indexes("user_account", [(f"ix_user_account_{column}", [column]) for column in user_columns])
    ensure_foreign_keys("user_account", [
        (f"fk_user_{assignment}_commission_approver", [column])
        for assignment, column in zip(("internal", "external"), user_columns)
    ])
    ensure_columns("commission", [
        sa.Column("approval_state", sa.String(20), server_default="not_required", nullable=False),
        sa.Column("approval_approver_user_id", sa.Integer()),
        sa.Column("approval_requested_at", sa.DateTime(timezone=True)),
        sa.Column("approved_by_user_id", sa.Integer()),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
    ])
    ensure_indexes("commission", [
        ("ix_commission_approval_approver_user_id", ["approval_approver_user_id"]),
        ("ix_commission_approval_state", ["approval_state"]),
    ])
    ensure_foreign_keys("commission", [
        ("fk_commission_approval_approver", ["approval_approver_user_id"]),
        ("fk_commission_approved_by", ["approved_by_user_id"]),
    ])


def downgrade():
    with op.batch_alter_table("commission") as batch:
        batch.drop_constraint("fk_commission_approval_approver", type_="foreignkey")
        batch.drop_constraint("fk_commission_approved_by", type_="foreignkey")
        batch.drop_index("ix_commission_approval_approver_user_id")
        batch.drop_index("ix_commission_approval_state")
        for column in ("approval_state", "approval_approver_user_id", "approval_requested_at", "approved_by_user_id", "approved_at"):
            batch.drop_column(column)
    with op.batch_alter_table("user_account") as batch:
        for assignment in ("internal", "external"):
            column = f"{assignment}_commission_approver_user_id"
            batch.drop_constraint(f"fk_user_{assignment}_commission_approver", type_="foreignkey")
            batch.drop_index(f"ix_user_account_{column}")
            batch.drop_column(column)
