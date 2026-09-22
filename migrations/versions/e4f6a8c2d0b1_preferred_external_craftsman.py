from alembic import op
import sqlalchemy as sa


revision = "e4f6a8c2d0b1"
down_revision = "d8e1f3a5b7c9"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table(
        "craft_activity"
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "preferred_external_craftsman_id",
                sa.Integer(),
                nullable=True,
            )
        )
        batch_op.create_index(
            "ix_craft_activity_preferred_external_craftsman_id",
            [
                "preferred_external_craftsman_id"
            ],
            unique=False,
        )
        batch_op.create_foreign_key(
            "fk_craft_activity_preferred_external_craftsman",
            "external_craftsman",
            [
                "preferred_external_craftsman_id"
            ],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade():
    with op.batch_alter_table(
        "craft_activity"
    ) as batch_op:
        batch_op.drop_constraint(
            "fk_craft_activity_preferred_external_craftsman",
            type_="foreignkey",
        )
        batch_op.drop_index(
            "ix_craft_activity_preferred_external_craftsman_id"
        )
        batch_op.drop_column(
            "preferred_external_craftsman_id"
        )