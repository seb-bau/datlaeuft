from alembic import op
import sqlalchemy as sa


revision = "c92d4e7a1b30"
down_revision = "a6c52d8e4f91"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ticket_ai_assignment_rule",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "name",
            sa.String(length=160),
            nullable=False,
        ),
        sa.Column(
            "condition_text",
            sa.Text(),
            nullable=False,
        ),
        sa.Column(
            "target_user_id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "enforcement_mode",
            sa.String(length=20),
            server_default="suggest",
            nullable=False,
        ),
        sa.Column(
            "priority",
            sa.Integer(),
            server_default="100",
            nullable=False,
        ),
        sa.Column(
            "active",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.Column(
            "created_by_user_id",
            sa.Integer(),
            nullable=True,
        ),
        sa.Column(
            "updated_by_user_id",
            sa.Integer(),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["target_user_id"],
            ["user_account.id"],
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user_account.id"],
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["user_account.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    for column in (
        "target_user_id",
        "enforcement_mode",
        "priority",
        "active",
        "created_by_user_id",
        "updated_by_user_id",
    ):
        op.create_index(
            f"ix_ticket_ai_assignment_rule_{column}",
            "ticket_ai_assignment_rule",
            [column],
        )

    with op.batch_alter_table("ticket_mirror") as batch_op:
        batch_op.add_column(
            sa.Column(
                "ai_assignment_rule_id",
                sa.Integer(),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "ai_assigned_user_id",
                sa.Integer(),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "ai_assignment_mode",
                sa.String(length=20),
                nullable=True,
            )
        )
        batch_op.create_index(
            "ix_ticket_mirror_ai_assignment_rule_id",
            ["ai_assignment_rule_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_ticket_mirror_ai_assigned_user_id",
            ["ai_assigned_user_id"],
            unique=False,
        )
        batch_op.create_foreign_key(
            "fk_ticket_mirror_ai_assignment_rule",
            "ticket_ai_assignment_rule",
            ["ai_assignment_rule_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            "fk_ticket_mirror_ai_assigned_user",
            "user_account",
            ["ai_assigned_user_id"],
            ["id"],
            ondelete="SET NULL",
        )

    op.execute(
        "UPDATE ticket_mirror "
        "SET ai_state = 'stale' "
        "WHERE ai_state = 'ready'"
    )


def downgrade():
    with op.batch_alter_table("ticket_mirror") as batch_op:
        batch_op.drop_constraint(
            "fk_ticket_mirror_ai_assigned_user",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "fk_ticket_mirror_ai_assignment_rule",
            type_="foreignkey",
        )
        batch_op.drop_index(
            "ix_ticket_mirror_ai_assigned_user_id"
        )
        batch_op.drop_index(
            "ix_ticket_mirror_ai_assignment_rule_id"
        )
        batch_op.drop_column("ai_assignment_mode")
        batch_op.drop_column("ai_assigned_user_id")
        batch_op.drop_column("ai_assignment_rule_id")

    for column in (
        "updated_by_user_id",
        "created_by_user_id",
        "active",
        "priority",
        "enforcement_mode",
        "target_user_id",
    ):
        op.drop_index(
            f"ix_ticket_ai_assignment_rule_{column}",
            table_name="ticket_ai_assignment_rule",
        )

    op.drop_table("ticket_ai_assignment_rule")