from alembic import op
import sqlalchemy as sa


revision = "d8e1f3a5b7c9"
down_revision = "c1d4e7f9a2b5"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "external_craftsman",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("craftsman_erp_id", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("username", sa.String(length=120), nullable=True),
        sa.Column("password_hash", sa.String(length=255), nullable=True),
        sa.Column(
            "portal_enabled",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column(
            "wowi_active",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.Column(
            "failed_login_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "locked_until",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "invited_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "last_login_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "last_synced_at",
            sa.DateTime(timezone=True),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "craftsman_erp_id",
            name="uq_external_craftsman_erp_id",
        ),
        sa.UniqueConstraint(
            "username",
            name="uq_external_craftsman_username",
        ),
    )
    op.create_index(
        "ix_external_craftsman_portal_enabled",
        "external_craftsman",
        ["portal_enabled"],
        unique=False,
    )
    op.create_index(
        "ix_external_craftsman_wowi_active",
        "external_craftsman",
        ["wowi_active"],
        unique=False,
    )

    op.create_table(
        "external_craftsman_craft_activity",
        sa.Column(
            "external_craftsman_id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "craft_activity_id",
            sa.Integer(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["external_craftsman_id"],
            ["external_craftsman.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["craft_activity_id"],
            ["craft_activity.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "external_craftsman_id",
            "craft_activity_id",
        ),
    )

    op.create_table(
        "external_craftsman_access_token",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "external_craftsman_id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "created_by_user_id",
            sa.Integer(),
            nullable=True,
        ),
        sa.Column(
            "purpose",
            sa.String(length=40),
            nullable=False,
        ),
        sa.Column(
            "token_hash",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "used_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user_account.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["external_craftsman_id"],
            ["external_craftsman.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "token_hash",
            name="uq_external_access_token_hash",
        ),
    )
    op.create_index(
        "ix_external_access_token_craftsman",
        "external_craftsman_access_token",
        ["external_craftsman_id"],
        unique=False,
    )
    op.create_index(
        "ix_external_access_token_expires",
        "external_craftsman_access_token",
        ["expires_at"],
        unique=False,
    )

    with op.batch_alter_table("commission") as batch_op:
        batch_op.add_column(
            sa.Column(
                "completion_submitted_by_external_craftsman_id",
                sa.Integer(),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "external_assignment_version",
                sa.Integer(),
                server_default="0",
                nullable=False,
            )
        )
        batch_op.create_foreign_key(
            "fk_commission_completion_external",
            "external_craftsman",
            ["completion_submitted_by_external_craftsman_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_commission_completion_submitted_by_external_craftsman_id",
            ["completion_submitted_by_external_craftsman_id"],
            unique=False,
        )

    op.create_table(
        "external_commission_assignment_mail",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("commission_id", sa.Integer(), nullable=False),
        sa.Column(
            "external_craftsman_id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "assignment_version",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "state",
            sa.String(length=30),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "locked_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["commission_id"],
            ["commission.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["external_craftsman_id"],
            ["external_craftsman.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "commission_id",
            "assignment_version",
            name="uq_external_assignment_mail_version",
        ),
    )
    op.create_index(
        "ix_external_assignment_mail_state",
        "external_commission_assignment_mail",
        ["state", "available_at"],
        unique=False,
    )
    op.create_index(
        "ix_external_assignment_mail_craftsman",
        "external_commission_assignment_mail",
        ["external_craftsman_id"],
        unique=False,
    )

    with op.batch_alter_table("audit_log") as batch_op:
        batch_op.add_column(
            sa.Column(
                "actor_external_craftsman_id",
                sa.Integer(),
                nullable=True,
            )
        )
        batch_op.create_foreign_key(
            "fk_audit_log_external_actor",
            "external_craftsman",
            ["actor_external_craftsman_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_audit_log_actor_external_craftsman_id",
            ["actor_external_craftsman_id"],
            unique=False,
        )


def downgrade():
    with op.batch_alter_table("audit_log") as batch_op:
        batch_op.drop_index(
            "ix_audit_log_actor_external_craftsman_id"
        )
        batch_op.drop_constraint(
            "fk_audit_log_external_actor",
            type_="foreignkey",
        )
        batch_op.drop_column(
            "actor_external_craftsman_id"
        )

    op.drop_index(
        "ix_external_assignment_mail_craftsman",
        table_name="external_commission_assignment_mail",
    )
    op.drop_index(
        "ix_external_assignment_mail_state",
        table_name="external_commission_assignment_mail",
    )
    op.drop_table(
        "external_commission_assignment_mail"
    )

    with op.batch_alter_table("commission") as batch_op:
        batch_op.drop_index(
            "ix_commission_completion_submitted_by_external_craftsman_id"
        )
        batch_op.drop_constraint(
            "fk_commission_completion_external",
            type_="foreignkey",
        )
        batch_op.drop_column(
            "external_assignment_version"
        )
        batch_op.drop_column(
            "completion_submitted_by_external_craftsman_id"
        )

    op.drop_index(
        "ix_external_access_token_expires",
        table_name="external_craftsman_access_token",
    )
    op.drop_index(
        "ix_external_access_token_craftsman",
        table_name="external_craftsman_access_token",
    )
    op.drop_table(
        "external_craftsman_access_token"
    )
    op.drop_table(
        "external_craftsman_craft_activity"
    )
    op.drop_index(
        "ix_external_craftsman_wowi_active",
        table_name="external_craftsman",
    )
    op.drop_index(
        "ix_external_craftsman_portal_enabled",
        table_name="external_craftsman",
    )
    op.drop_table("external_craftsman")