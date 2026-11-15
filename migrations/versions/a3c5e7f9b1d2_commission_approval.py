from alembic import op
import sqlalchemy as sa

revision = "a3c5e7f9b1d2"
down_revision = "f1b3d5e7a9c2"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("user_account") as batch:
        for assignment in ("internal", "external"):
            column = f"{assignment}_commission_approver_user_id"
            batch.add_column(sa.Column(column, sa.Integer()))
            batch.create_foreign_key(f"fk_user_{assignment}_commission_approver", "user_account", [column], ["id"], ondelete="RESTRICT")
            batch.create_index(f"ix_user_account_{column}", [column])
    with op.batch_alter_table("commission") as batch:
        batch.add_column(sa.Column("approval_state", sa.String(20), server_default="not_required", nullable=False))
        batch.add_column(sa.Column("approval_approver_user_id", sa.Integer()))
        batch.add_column(sa.Column("approval_requested_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("approved_by_user_id", sa.Integer()))
        batch.add_column(sa.Column("approved_at", sa.DateTime(timezone=True)))
        batch.create_foreign_key("fk_commission_approval_approver", "user_account", ["approval_approver_user_id"], ["id"], ondelete="RESTRICT")
        batch.create_foreign_key("fk_commission_approved_by", "user_account", ["approved_by_user_id"], ["id"], ondelete="RESTRICT")
        batch.create_index("ix_commission_approval_approver_user_id", ["approval_approver_user_id"])
        batch.create_index("ix_commission_approval_state", ["approval_state"])


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
