from alembic import op
import sqlalchemy as sa


revision = "e8f1a2b3c4d5"
down_revision = "d7e8f9a0b1c2"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table(
        "ticket_ai_assignment_rule"
    ) as batch_op:
        batch_op.alter_column(
            "target_user_id",
            existing_type=sa.Integer(),
            nullable=True,
        )
        batch_op.add_column(
            sa.Column(
                "target_craft_activity_id",
                sa.Integer(),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "target_task_template_id",
                sa.Integer(),
                nullable=True,
            )
        )
        batch_op.create_index(
            "ix_ticket_ai_assignment_rule_target_craft_activity_id",
            ["target_craft_activity_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_ticket_ai_assignment_rule_target_task_template_id",
            ["target_task_template_id"],
            unique=False,
        )
        batch_op.create_foreign_key(
            "fk_ticket_ai_assignment_rule_target_craft",
            "craft_activity",
            ["target_craft_activity_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            "fk_ticket_ai_assignment_rule_target_task",
            "time_task_template",
            ["target_task_template_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade():
    with op.batch_alter_table(
        "ticket_ai_assignment_rule"
    ) as batch_op:
        batch_op.drop_constraint(
            "fk_ticket_ai_assignment_rule_target_task",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "fk_ticket_ai_assignment_rule_target_craft",
            type_="foreignkey",
        )
        batch_op.drop_index(
            "ix_ticket_ai_assignment_rule_target_task_template_id"
        )
        batch_op.drop_index(
            "ix_ticket_ai_assignment_rule_target_craft_activity_id"
        )
        batch_op.drop_column(
            "target_task_template_id"
        )
        batch_op.drop_column(
            "target_craft_activity_id"
        )
        batch_op.alter_column(
            "target_user_id",
            existing_type=sa.Integer(),
            nullable=False,
        )