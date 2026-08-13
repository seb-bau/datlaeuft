from alembic import op
import sqlalchemy as sa


revision = "c4d5e6f7a8b9"
down_revision = "b3c4d5e6f7a8"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table(
        "invoice",
        schema=None,
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "dms_archive_status",
                sa.String(length=30),
                server_default="not_archived",
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "dms_archive_key",
                sa.String(length=180),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "dms_document_id",
                sa.String(length=255),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "dms_archive_pdf_sha256",
                sa.String(length=64),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "dms_archive_attempted_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "dms_archived_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "dms_archived_by_user_id",
                sa.Integer(),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "dms_archive_error",
                sa.Text(),
                nullable=True,
            )
        )
        batch_op.create_foreign_key(
            "fk_invoice_dms_archived_by_user_id",
            "user_account",
            ["dms_archived_by_user_id"],
            ["id"],
        )
        batch_op.create_unique_constraint(
            "uq_invoice_dms_archive_key",
            ["dms_archive_key"],
        )
        batch_op.create_unique_constraint(
            "uq_invoice_dms_document_id",
            ["dms_document_id"],
        )
        batch_op.create_index(
            "ix_invoice_dms_archive_status",
            ["dms_archive_status"],
            unique=False,
        )
        batch_op.create_index(
            "ix_invoice_dms_archive_key",
            ["dms_archive_key"],
            unique=False,
        )
        batch_op.create_index(
            "ix_invoice_dms_document_id",
            ["dms_document_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_invoice_dms_archived_by_user_id",
            ["dms_archived_by_user_id"],
            unique=False,
        )


def downgrade():
    with op.batch_alter_table(
        "invoice",
        schema=None,
    ) as batch_op:
        batch_op.drop_index(
            "ix_invoice_dms_archived_by_user_id"
        )
        batch_op.drop_index(
            "ix_invoice_dms_document_id"
        )
        batch_op.drop_index(
            "ix_invoice_dms_archive_key"
        )
        batch_op.drop_index(
            "ix_invoice_dms_archive_status"
        )
        batch_op.drop_constraint(
            "uq_invoice_dms_document_id",
            type_="unique",
        )
        batch_op.drop_constraint(
            "uq_invoice_dms_archive_key",
            type_="unique",
        )
        batch_op.drop_constraint(
            "fk_invoice_dms_archived_by_user_id",
            type_="foreignkey",
        )
        batch_op.drop_column(
            "dms_archive_error"
        )
        batch_op.drop_column(
            "dms_archived_by_user_id"
        )
        batch_op.drop_column(
            "dms_archived_at"
        )
        batch_op.drop_column(
            "dms_archive_attempted_at"
        )
        batch_op.drop_column(
            "dms_archive_pdf_sha256"
        )
        batch_op.drop_column(
            "dms_document_id"
        )
        batch_op.drop_column(
            "dms_archive_key"
        )
        batch_op.drop_column(
            "dms_archive_status"
        )