from alembic import op
import sqlalchemy as sa


revision = "a6d7e8f9a0b1"
down_revision = "f5c6d7e8f9a0"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table(
        "commission_photo",
        schema=None,
    ) as batch_op:
        batch_op.alter_column(
            "commission_id",
            existing_type=sa.Integer(),
            nullable=True,
        )
        batch_op.add_column(
            sa.Column(
                "external_file_guid",
                sa.String(length=64),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "metadata_signature",
                sa.String(length=64),
                nullable=True,
            )
        )
        batch_op.create_index(
            batch_op.f(
                "ix_commission_photo_external_file_guid"
            ),
            ["external_file_guid"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f(
                "ix_commission_photo_metadata_signature"
            ),
            ["metadata_signature"],
            unique=False,
        )


def downgrade():
    op.execute(
        "DELETE FROM commission_photo "
        "WHERE commission_id IS NULL"
    )

    with op.batch_alter_table(
        "commission_photo",
        schema=None,
    ) as batch_op:
        batch_op.drop_index(
            batch_op.f(
                "ix_commission_photo_metadata_signature"
            )
        )
        batch_op.drop_index(
            batch_op.f(
                "ix_commission_photo_external_file_guid"
            )
        )
        batch_op.drop_column(
            "metadata_signature"
        )
        batch_op.drop_column(
            "external_file_guid"
        )
        batch_op.alter_column(
            "commission_id",
            existing_type=sa.Integer(),
            nullable=False,
        )