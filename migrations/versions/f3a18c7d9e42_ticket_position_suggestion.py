from alembic import op
import sqlalchemy as sa


revision = "f3a18c7d9e42"
down_revision = "b47a9b6f4c21"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ticket_craft_fallback",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "craft_activity_id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "task_template_id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "updated_by_user_id",
            sa.Integer(),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["craft_activity_id"],
            ["craft_activity.id"],
        ),
        sa.ForeignKeyConstraint(
            ["task_template_id"],
            ["time_task_template.id"],
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["user_account.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "craft_activity_id",
            name="uq_ticket_craft_fallback_craft",
        ),
        sa.UniqueConstraint(
            "task_template_id",
            name="uq_ticket_craft_fallback_task",
        ),
    )

    op.create_index(
        "ix_ticket_craft_fallback_craft_activity_id",
        "ticket_craft_fallback",
        ["craft_activity_id"],
    )
    op.create_index(
        "ix_ticket_craft_fallback_task_template_id",
        "ticket_craft_fallback",
        ["task_template_id"],
    )
    op.create_index(
        "ix_ticket_craft_fallback_updated_by_user_id",
        "ticket_craft_fallback",
        ["updated_by_user_id"],
    )

    with op.batch_alter_table(
        "ticket_mirror"
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "ai_task_template_id",
                sa.Integer(),
                nullable=True,
            )
        )
        batch_op.create_index(
            "ix_ticket_mirror_ai_task_template_id",
            ["ai_task_template_id"],
            unique=False,
        )
        batch_op.create_foreign_key(
            "fk_ticket_mirror_ai_task_template",
            "time_task_template",
            ["ai_task_template_id"],
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
        "ticket_mirror"
    ) as batch_op:
        batch_op.drop_constraint(
            "fk_ticket_mirror_ai_task_template",
            type_="foreignkey",
        )
        batch_op.drop_index(
            "ix_ticket_mirror_ai_task_template_id"
        )
        batch_op.drop_column(
            "ai_task_template_id"
        )

    op.drop_index(
        "ix_ticket_craft_fallback_updated_by_user_id",
        table_name="ticket_craft_fallback",
    )
    op.drop_index(
        "ix_ticket_craft_fallback_task_template_id",
        table_name="ticket_craft_fallback",
    )
    op.drop_index(
        "ix_ticket_craft_fallback_craft_activity_id",
        table_name="ticket_craft_fallback",
    )
    op.drop_table("ticket_craft_fallback")