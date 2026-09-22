from alembic import op
import sqlalchemy as sa


revision = "c1d4e7f9a2b5"
down_revision = "b7d2e4f6a8c1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_commission_visibility",
        sa.Column(
            "viewer_user_id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "visible_user_id",
            sa.Integer(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "viewer_user_id <> visible_user_id",
            name=(
                "ck_user_commission_visibility_"
                "distinct_users"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["viewer_user_id"],
            ["user_account.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["visible_user_id"],
            ["user_account.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "viewer_user_id",
            "visible_user_id",
        ),
    )
    op.create_index(
        (
            "ix_user_commission_visibility_"
            "visible_user_id"
        ),
        "user_commission_visibility",
        ["visible_user_id"],
        unique=False,
    )

    connection = op.get_bind()
    setting_table = sa.table(
        "app_setting",
        sa.column("key", sa.String()),
        sa.column("value_text", sa.Text()),
    )
    global_value = connection.execute(
        sa.select(setting_table.c.value_text).where(
            setting_table.c.key
            == "mobile.allow_colleague_commissions"
        )
    ).scalar_one_or_none()
    global_access_enabled = (
        str(global_value or "").strip().lower()
        in {"1", "true", "yes", "on", "ja"}
    )

    user_table = sa.table(
        "user_account",
        sa.column("id", sa.Integer()),
        sa.column("craftsman_erp_id", sa.Integer()),
        sa.column("is_admin", sa.Boolean()),
        sa.column("is_foreman", sa.Boolean()),
        sa.column("is_active_flag", sa.Boolean()),
    )
    visibility_table = sa.table(
        "user_commission_visibility",
        sa.column("viewer_user_id", sa.Integer()),
        sa.column("visible_user_id", sa.Integer()),
    )
    viewer = user_table.alias("viewer")
    visible_user = user_table.alias("visible_user")
    conditions = [
        viewer.c.is_active_flag.is_(True),
        visible_user.c.is_active_flag.is_(True),
        visible_user.c.craftsman_erp_id.is_not(None),
        viewer.c.id != visible_user.c.id,
    ]

    if not global_access_enabled:
        conditions.append(
            sa.or_(
                viewer.c.is_admin.is_(True),
                viewer.c.is_foreman.is_(True),
            )
        )

    connection.execute(
        visibility_table.insert().from_select(
            [
                "viewer_user_id",
                "visible_user_id",
            ],
            sa.select(
                viewer.c.id,
                visible_user.c.id,
            )
            .select_from(
                viewer.join(
                    visible_user,
                    viewer.c.id != visible_user.c.id,
                )
            )
            .where(*conditions),
        )
    )


def downgrade():
    op.drop_index(
        (
            "ix_user_commission_visibility_"
            "visible_user_id"
        ),
        table_name="user_commission_visibility",
    )
    op.drop_table("user_commission_visibility")