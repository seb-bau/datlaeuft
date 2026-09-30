from alembic import op
import sqlalchemy as sa


revision = "d4e6f8a0b2c4"
down_revision = "c3e5f7a9b1d2"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table(
        "ticket_ai_assignment_rule"
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "target_assignment_type",
                sa.String(length=20),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "target_external_craftsman_id",
                sa.Integer(),
                nullable=True,
            )
        )
        batch_op.create_index(
            "ix_ticket_ai_assignment_rule_target_external_craftsman_id",
            ["target_external_craftsman_id"],
            unique=False,
        )
        batch_op.create_foreign_key(
            "fk_ticket_ai_assignment_rule_target_external",
            "external_craftsman",
            ["target_external_craftsman_id"],
            ["id"],
            ondelete="SET NULL",
        )

    op.execute(
        "UPDATE ticket_mirror "
        "SET ai_state = 'stale' "
        "WHERE ai_state = 'ready'"
    )


def downgrade():
    with op.batch_alter_table(
        "ticket_ai_assignment_rule"
    ) as batch_op:
        batch_op.drop_constraint(
            "fk_ticket_ai_assignment_rule_target_external",
            type_="foreignkey",
        )
        batch_op.drop_index(
            "ix_ticket_ai_assignment_rule_target_external_craftsman_id"
        )
        batch_op.drop_column("target_external_craftsman_id")
        batch_op.drop_column("target_assignment_type")