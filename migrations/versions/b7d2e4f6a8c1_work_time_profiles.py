from alembic import op
import sqlalchemy as sa


revision = "b7d2e4f6a8c1"
down_revision = "a4c7e9f1b2d3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "work_time_profile",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "name",
            sa.String(length=255),
            nullable=False,
        ),
        sa.Column(
            "monday_minutes",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "tuesday_minutes",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "wednesday_minutes",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "thursday_minutes",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "friday_minutes",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "saturday_minutes",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "sunday_minutes",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "active",
            sa.Boolean(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "monday_minutes BETWEEN 0 AND 1440 "
            "AND tuesday_minutes BETWEEN 0 AND 1440 "
            "AND wednesday_minutes BETWEEN 0 AND 1440 "
            "AND thursday_minutes BETWEEN 0 AND 1440 "
            "AND friday_minutes BETWEEN 0 AND 1440 "
            "AND saturday_minutes BETWEEN 0 AND 1440 "
            "AND sunday_minutes BETWEEN 0 AND 1440",
            name="ck_work_time_profile_daily_minutes",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "name",
            name="uq_work_time_profile_name",
        ),
    )

    op.create_table(
        "user_work_time_profile_assignment",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "user_id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "profile_id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "effective_from",
            sa.Date(),
            nullable=False,
        ),
        sa.Column(
            "created_by_user_id",
            sa.Integer(),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user_account.id"],
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["work_time_profile.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user_account.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "effective_from",
            name=(
                "uq_user_work_time_profile_"
                "assignment_date"
            ),
        ),
    )

    op.create_index(
        "ix_user_work_time_profile_assignment_user_id",
        "user_work_time_profile_assignment",
        ["user_id"],
    )
    op.create_index(
        "ix_user_work_time_profile_assignment_profile_id",
        "user_work_time_profile_assignment",
        ["profile_id"],
    )
    op.create_index(
        (
            "ix_user_work_time_profile_"
            "assignment_effective_from"
        ),
        "user_work_time_profile_assignment",
        ["effective_from"],
    )

    op.create_table(
        "work_time_target_override",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "user_id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "work_date",
            sa.Date(),
            nullable=False,
        ),
        sa.Column(
            "target_minutes",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "reason",
            sa.String(length=255),
            nullable=False,
        ),
        sa.Column(
            "created_by_user_id",
            sa.Integer(),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "target_minutes BETWEEN 0 AND 1440",
            name="ck_work_time_target_override_minutes",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user_account.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user_account.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "work_date",
            name="uq_work_time_target_override_user_date",
        ),
    )

    op.create_index(
        "ix_work_time_target_override_user_id",
        "work_time_target_override",
        ["user_id"],
    )
    op.create_index(
        "ix_work_time_target_override_work_date",
        "work_time_target_override",
        ["work_date"],
    )

    with op.batch_alter_table(
        "time_task_template"
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "free_time_enabled",
                sa.Boolean(),
                server_default=sa.text("0"),
                nullable=False,
            )
        )

    with op.batch_alter_table(
        "time_entry"
    ) as batch_op:
        batch_op.alter_column(
            "commission_id",
            existing_type=sa.Integer(),
            nullable=True,
        )


def downgrade():
    op.execute(
        "DELETE FROM time_entry "
        "WHERE commission_id IS NULL"
    )

    with op.batch_alter_table(
        "time_entry"
    ) as batch_op:
        batch_op.alter_column(
            "commission_id",
            existing_type=sa.Integer(),
            nullable=False,
        )

    with op.batch_alter_table(
        "time_task_template"
    ) as batch_op:
        batch_op.drop_column(
            "free_time_enabled"
        )

    op.drop_index(
        "ix_work_time_target_override_work_date",
        table_name="work_time_target_override",
    )
    op.drop_index(
        "ix_work_time_target_override_user_id",
        table_name="work_time_target_override",
    )
    op.drop_table(
        "work_time_target_override"
    )

    op.drop_index(
        (
            "ix_user_work_time_profile_"
            "assignment_effective_from"
        ),
        table_name=(
            "user_work_time_profile_assignment"
        ),
    )
    op.drop_index(
        "ix_user_work_time_profile_assignment_profile_id",
        table_name=(
            "user_work_time_profile_assignment"
        ),
    )
    op.drop_index(
        "ix_user_work_time_profile_assignment_user_id",
        table_name=(
            "user_work_time_profile_assignment"
        ),
    )
    op.drop_table(
        "user_work_time_profile_assignment"
    )
    op.drop_table(
        "work_time_profile"
    )