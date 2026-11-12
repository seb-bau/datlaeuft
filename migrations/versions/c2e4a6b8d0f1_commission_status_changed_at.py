from alembic import op
import sqlalchemy as sa


revision = "c2e4a6b8d0f1"
down_revision = "ba8c2d4e6f10"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "commission",
        sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True),
    )
    commission = sa.table(
        "commission",
        sa.column("id", sa.Integer()),
        sa.column("local_status", sa.String()),
        sa.column("source", sa.String()),
        sa.column("recording_date", sa.DateTime(timezone=True)),
        sa.column("completion_submitted_at", sa.DateTime(timezone=True)),
        sa.column("completed_at", sa.DateTime(timezone=True)),
        sa.column("deleted_at", sa.DateTime(timezone=True)),
        sa.column("status_changed_at", sa.DateTime(timezone=True)),
    )
    audit_log = sa.table(
        "audit_log",
        sa.column("entity_type", sa.String()),
        sa.column("entity_id", sa.String()),
        sa.column("action", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    status_actions = {
        "in_progress": ("commission.start",),
        "completion_review": ("commission.reject", "commission.complete"),
        "tenant_charge_review": ("commission.complete",),
        "done": ("commission.complete",),
    }
    for status, actions in status_actions.items():
        last_change = sa.select(sa.func.max(audit_log.c.created_at)).where(
            audit_log.c.entity_type == "commission",
            audit_log.c.entity_id == sa.cast(commission.c.id, sa.String()),
            audit_log.c.action.in_(actions),
        ).correlate(commission).scalar_subquery()
        op.execute(commission.update().where(
            commission.c.local_status == status,
        ).values(status_changed_at=last_change))

    for status, status_date in (
        ("deleted", commission.c.deleted_at),
        ("completion_review", commission.c.completion_submitted_at),
        ("tenant_charge_review", sa.func.coalesce(
            commission.c.completion_submitted_at, commission.c.completed_at,
        )),
        ("done", sa.func.coalesce(
            commission.c.completion_submitted_at, commission.c.completed_at,
        )),
    ):
        op.execute(commission.update().where(
            commission.c.local_status == status,
        ).values(status_changed_at=sa.func.coalesce(
            commission.c.status_changed_at, status_date,
        )))

    op.execute(commission.update().where(
        commission.c.local_status == "recorded",
        commission.c.source.in_(("web", "ios")),
    ).values(status_changed_at=commission.c.recording_date))


def downgrade():
    op.drop_column("commission", "status_changed_at")
