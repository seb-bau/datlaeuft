from alembic import op
import sqlalchemy as sa


revision = "70b6a1c9d245"
down_revision = "2dd0c8028f8f"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("commission") as batch_op:
        batch_op.add_column(sa.Column(
            "is_high_priority", sa.Boolean(),
            server_default=sa.false(), nullable=False,
        ))
        batch_op.add_column(sa.Column(
            "priority_is_manual", sa.Boolean(),
            server_default=sa.false(), nullable=False,
        ))

    op.create_table(
        "commission_ticket_link",
        sa.Column("commission_id", sa.Integer(), nullable=False),
        sa.Column("ticket_erp_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["commission_id"], ["commission.id"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("commission_id", "ticket_erp_id"),
    )
    op.create_index(
        "ix_commission_ticket_link_ticket_erp_id",
        "commission_ticket_link", ["ticket_erp_id"],
    )


def downgrade():
    op.drop_table("commission_ticket_link")
    with op.batch_alter_table("commission") as batch_op:
        batch_op.drop_column("priority_is_manual")
        batch_op.drop_column("is_high_priority")