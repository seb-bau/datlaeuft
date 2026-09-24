from alembic import op
import sqlalchemy as sa


revision = "a7c9e1f3b5d7"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table(
        "process_info"
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "sender_external_craftsman_id",
                sa.Integer(),
                nullable=True,
            )
        )
        batch_op.create_foreign_key(
            "fk_process_info_sender_external_craftsman",
            "external_craftsman",
            [
                "sender_external_craftsman_id"
            ],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_process_info_sender_external_craftsman_id",
            [
                "sender_external_craftsman_id"
            ],
            unique=False,
        )


def downgrade():
    with op.batch_alter_table(
        "process_info"
    ) as batch_op:
        batch_op.drop_index(
            "ix_process_info_sender_external_craftsman_id"
        )
        batch_op.drop_constraint(
            "fk_process_info_sender_external_craftsman",
            type_="foreignkey",
        )
        batch_op.drop_column(
            "sender_external_craftsman_id"
        )