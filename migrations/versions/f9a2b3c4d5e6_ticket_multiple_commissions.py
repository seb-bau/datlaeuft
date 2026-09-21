from alembic import op
import sqlalchemy as sa


revision = "f9a2b3c4d5e6"
down_revision = "e8f1a2b3c4d5"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        INSERT INTO commission_ticket_link (
            commission_id,
            ticket_erp_id
        )
        SELECT
            ticket_mirror.commission_id,
            ticket_mirror.erp_id
        FROM ticket_mirror
        WHERE ticket_mirror.commission_id IS NOT NULL
        AND NOT EXISTS (
            SELECT 1
            FROM commission_ticket_link
            WHERE commission_ticket_link.commission_id
                = ticket_mirror.commission_id
            AND commission_ticket_link.ticket_erp_id
                = ticket_mirror.erp_id
        )
        """
    )

    with op.batch_alter_table(
        "commission_ticket_link"
    ) as batch_op:
        batch_op.create_foreign_key(
            "fk_commission_ticket_link_ticket",
            "ticket_mirror",
            ["ticket_erp_id"],
            ["erp_id"],
            ondelete="CASCADE",
        )


def downgrade():
    with op.batch_alter_table(
        "commission_ticket_link"
    ) as batch_op:
        batch_op.drop_constraint(
            "fk_commission_ticket_link_ticket",
            type_="foreignkey",
        )