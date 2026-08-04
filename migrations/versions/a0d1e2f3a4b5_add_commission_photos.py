from alembic import op
import sqlalchemy as sa


revision = "a0d1e2f3a4b5"
down_revision = "f9c1a2b3d4e5"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "commission_photo",
        sa.Column(
            "id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "commission_id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "uploaded_by_user_id",
            sa.Integer(),
            nullable=True,
        ),
        sa.Column(
            "source",
            sa.String(length=40),
            server_default="app",
            nullable=False,
        ),
        sa.Column(
            "external_id",
            sa.String(length=160),
            nullable=True,
        ),
        sa.Column(
            "original_filename",
            sa.String(length=255),
            nullable=False,
        ),
        sa.Column(
            "storage_name",
            sa.String(length=500),
            nullable=False,
        ),
        sa.Column(
            "thumbnail_storage_name",
            sa.String(length=500),
            nullable=False,
        ),
        sa.Column(
            "mime_type",
            sa.String(length=100),
            server_default="image/jpeg",
            nullable=False,
        ),
        sa.Column(
            "size_bytes",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "content_sha256",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "caption",
            sa.String(length=500),
            nullable=True,
        ),
        sa.Column(
            "active",
            sa.Boolean(),
            server_default="1",
            nullable=False,
        ),
        sa.Column(
            "external_updated_at",
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
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["commission_id"],
            ["commission.id"],
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by_user_id"],
            ["user_account.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "commission_id",
            "external_id",
            name="uq_commission_photo_external_id",
        ),
        sa.UniqueConstraint("storage_name"),
        sa.UniqueConstraint(
            "thumbnail_storage_name"
        ),
    )

    with op.batch_alter_table(
        "commission_photo",
        schema=None,
    ) as batch_op:
        batch_op.create_index(
            "ix_commission_photo_commission_id",
            ["commission_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_commission_photo_uploaded_by_user_id",
            ["uploaded_by_user_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_commission_photo_source",
            ["source"],
            unique=False,
        )
        batch_op.create_index(
            "ix_commission_photo_external_id",
            ["external_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_commission_photo_content_sha256",
            ["content_sha256"],
            unique=False,
        )
        batch_op.create_index(
            "ix_commission_photo_active",
            ["active"],
            unique=False,
        )
        batch_op.create_index(
            "ix_commission_photo_created_at",
            ["created_at"],
            unique=False,
        )


def downgrade():
    op.drop_table(
        "commission_photo"
    )