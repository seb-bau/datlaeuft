from alembic import op
import sqlalchemy as sa

revision = "a2c4e6f8b0d1"
down_revision = "a1b3c5d7e9f2"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("external_craftsman") as batch_op:
        batch_op.add_column(sa.Column(
            "assignment_enabled", sa.Boolean(), server_default=sa.false(), nullable=False,
        ))
        batch_op.create_index(
            "ix_external_craftsman_assignment_enabled", ["assignment_enabled"], unique=False,
        )

    external_craftsman = sa.table(
        "external_craftsman",
        sa.column("assignment_enabled", sa.Boolean()),
        sa.column("portal_enabled", sa.Boolean()),
    )
    op.execute(
        external_craftsman.update()
        .where(external_craftsman.c.portal_enabled.is_(True))
        .values(assignment_enabled=True)
    )


def downgrade():
    with op.batch_alter_table("external_craftsman") as batch_op:
        batch_op.drop_index("ix_external_craftsman_assignment_enabled")
        batch_op.drop_column("assignment_enabled")