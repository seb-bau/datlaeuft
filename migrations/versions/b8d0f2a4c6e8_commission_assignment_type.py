from alembic import op
import sqlalchemy as sa


revision = "b8d0f2a4c6e8"
down_revision = "a7c9e1f3b5d7"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table(
        "commission"
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "assignment_type",
                sa.String(length=20),
                server_default="internal",
                nullable=False,
            )
        )
        batch_op.create_index(
            "ix_commission_assignment_type",
            ["assignment_type"],
            unique=False,
        )

    commission = sa.table(
        "commission",
        sa.column("id", sa.Integer()),
        sa.column("user_id", sa.Integer()),
        sa.column("craftsman_erp_id", sa.Integer()),
        sa.column(
            "external_assignment_version",
            sa.Integer(),
        ),
        sa.column(
            "assignment_type",
            sa.String(length=20),
        ),
    )
    external_craftsman = sa.table(
        "external_craftsman",
        sa.column("id", sa.Integer()),
        sa.column("craftsman_erp_id", sa.Integer()),
    )
    user_account = sa.table(
        "user_account",
        sa.column("craftsman_erp_id", sa.Integer()),
    )
    assignment_mail = sa.table(
        "external_commission_assignment_mail",
        sa.column("commission_id", sa.Integer()),
        sa.column(
            "external_craftsman_id",
            sa.Integer(),
        ),
        sa.column(
            "assignment_version",
            sa.Integer(),
        ),
    )

    current_external_assignment = sa.exists(
        sa.select(sa.literal(1))
        .select_from(
            assignment_mail.join(
                external_craftsman,
                assignment_mail
                .c.external_craftsman_id
                == external_craftsman.c.id,
            )
        )
        .where(
            assignment_mail.c.commission_id
            == commission.c.id,
            assignment_mail.c.assignment_version
            == commission
            .c.external_assignment_version,
            external_craftsman.c.craftsman_erp_id
            == commission.c.craftsman_erp_id,
        )
        .correlate(commission)
    )
    external_craftsman_erp_ids = sa.select(
        external_craftsman.c.craftsman_erp_id
    )
    internal_craftsman_erp_ids = (
        sa.select(
            user_account.c.craftsman_erp_id
        )
        .where(
            user_account.c.craftsman_erp_id
            .is_not(None)
        )
    )

    connection = op.get_bind()
    connection.execute(
        commission.update()
        .where(
            sa.and_(
                commission.c.user_id.is_(None),
                commission.c.craftsman_erp_id
                .is_not(None),
                sa.or_(
                    current_external_assignment,
                    sa.and_(
                        commission.c.craftsman_erp_id
                        .in_(
                            external_craftsman_erp_ids
                        ),
                        commission.c.craftsman_erp_id
                        .not_in(
                            internal_craftsman_erp_ids
                        ),
                    ),
                ),
            )
        )
        .values(
            assignment_type="external"
        )
    )


def downgrade():
    with op.batch_alter_table(
        "commission"
    ) as batch_op:
        batch_op.drop_index(
            "ix_commission_assignment_type"
        )
        batch_op.drop_column(
            "assignment_type"
        )