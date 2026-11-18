from alembic import op
import sqlalchemy as sa


revision = "f7c9e1a3b5d8"
down_revision = "f5b7d9e1a3c8"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("commission") as batch:
        batch.add_column(sa.Column("rejected_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("review_resolved_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("review_resolution", sa.String(30)))
        batch.add_column(sa.Column("review_resolved_by_user_id", sa.Integer()))
        batch.add_column(sa.Column("reissued_from_commission_id", sa.Integer()))
        batch.create_foreign_key("fk_commission_review_resolved_by", "user_account", ["review_resolved_by_user_id"], ["id"])
        batch.create_foreign_key("fk_commission_reissued_from", "commission", ["reissued_from_commission_id"], ["id"])
        batch.create_unique_constraint("uq_commission_reissued_from", ["reissued_from_commission_id"])
        for name in ("rejected_at", "review_resolved_at", "review_resolved_by_user_id"):
            batch.create_index("ix_commission_" + name, [name])
    connection = op.get_bind()
    tables = sa.inspect(connection).get_table_names()
    commission = sa.table("commission", sa.column("id", sa.Integer), sa.column("rejected_at"), sa.column("local_status"), sa.column("deleted_at"), sa.column("completion_submitted_at"), sa.column("status_changed_at"))
    evidence = []
    if "erp_outbox_operation" in tables:
        operations = sa.table("erp_outbox_operation", sa.column("commission_id"), sa.column("operation_type"))
        evidence.append(sa.exists(sa.select(1).where(operations.c.commission_id == commission.c.id, operations.c.operation_type == "refuse_commission")))
    if "audit_log" in tables:
        logs = sa.table("audit_log", sa.column("entity_type"), sa.column("entity_id"), sa.column("action"))
        evidence.append(sa.exists(sa.select(1).where(logs.c.entity_type == "commission", logs.c.entity_id == sa.cast(commission.c.id, sa.String(80)), logs.c.action == "commission.reject")))
    if evidence:
        connection.execute(sa.update(commission).where(sa.or_(*evidence)).values(
            rejected_at=sa.func.coalesce(commission.c.completion_submitted_at, commission.c.status_changed_at, sa.func.current_timestamp()),
            local_status="completion_review", deleted_at=None,
        ))


def downgrade():
    with op.batch_alter_table("commission") as batch:
        batch.drop_constraint("fk_commission_reissued_from", type_="foreignkey")
        batch.drop_constraint("fk_commission_review_resolved_by", type_="foreignkey")
        batch.drop_constraint("uq_commission_reissued_from", type_="unique")
        for name in ("rejected_at", "review_resolved_at", "review_resolved_by_user_id"):
            batch.drop_index("ix_commission_" + name)
        for name in ("reissued_from_commission_id", "review_resolved_by_user_id", "review_resolution", "review_resolved_at", "rejected_at"):
            batch.drop_column(name)
