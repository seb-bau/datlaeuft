from alembic import op
import sqlalchemy as sa


revision = "d7e8f9a0b1c2"
down_revision = "c92d4e7a1b30"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table(
        "time_task_template",
        schema=None,
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "review_when_shorter",
                sa.Boolean(),
                server_default=sa.text("0"),
                nullable=False,
            )
        )

    op.create_table(
        "commission_performance_review",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("commission_id", sa.Integer(), nullable=False),
        sa.Column("scope_key", sa.String(length=80), nullable=False),
        sa.Column("task_template_id", sa.Integer(), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=40),
            server_default="needs_review",
            nullable=False,
        ),
        sa.Column("reviewed_by_user_id", sa.Integer(), nullable=True),
        sa.Column(
            "reviewed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["commission_id"],
            ["commission.id"],
        ),
        sa.ForeignKeyConstraint(
            ["task_template_id"],
            ["time_task_template.id"],
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_user_id"],
            ["user_account.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "commission_id",
            "scope_key",
            name="uq_commission_performance_review_scope",
        ),
    )

    op.create_index(
        "ix_commission_performance_review_commission_id",
        "commission_performance_review",
        ["commission_id"],
    )
    op.create_index(
        "ix_commission_performance_review_status",
        "commission_performance_review",
        ["status"],
    )


def downgrade():
    op.drop_index(
        "ix_commission_performance_review_status",
        table_name="commission_performance_review",
    )
    op.drop_index(
        "ix_commission_performance_review_commission_id",
        table_name="commission_performance_review",
    )
    op.drop_table("commission_performance_review")

    with op.batch_alter_table(
        "time_task_template",
        schema=None,
    ) as batch_op:
        batch_op.drop_column(
            "review_when_shorter"
        )