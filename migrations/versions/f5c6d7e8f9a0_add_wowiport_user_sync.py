from alembic import op
import sqlalchemy as sa


revision = "f5c6d7e8f9a0"
down_revision = "e4b5c6d7e8f9"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table(
        "user_account",
        schema=None,
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "person_erp_id",
                sa.Integer(),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "creditor_erp_id",
                sa.Integer(),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "creditor_number",
                sa.String(length=120),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "craftsman_portal_id",
                sa.String(length=120),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "wowiport_managed",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "exclude_from_wowiport_sync",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            )
        )
        batch_op.create_index(
            batch_op.f(
                "ix_user_account_person_erp_id"
            ),
            ["person_erp_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f(
                "ix_user_account_creditor_erp_id"
            ),
            ["creditor_erp_id"],
            unique=False,
        )


def downgrade():
    with op.batch_alter_table(
        "user_account",
        schema=None,
    ) as batch_op:
        batch_op.drop_index(
            batch_op.f(
                "ix_user_account_creditor_erp_id"
            )
        )
        batch_op.drop_index(
            batch_op.f(
                "ix_user_account_person_erp_id"
            )
        )
        batch_op.drop_column(
            "exclude_from_wowiport_sync"
        )
        batch_op.drop_column(
            "wowiport_managed"
        )
        batch_op.drop_column(
            "craftsman_portal_id"
        )
        batch_op.drop_column(
            "creditor_number"
        )
        batch_op.drop_column(
            "creditor_erp_id"
        )
        batch_op.drop_column(
            "person_erp_id"
        )