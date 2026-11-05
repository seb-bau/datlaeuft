from alembic import op
import sqlalchemy as sa


revision = "e7b9c1d3f5a7"
down_revision = "d2f4a6b8c0e2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ticket_ai_job",
        sa.Column("ticket_id", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("source_signature", sa.String(64)),
        sa.Column("automatic", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("actor_user_id", sa.Integer()),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True)),
        sa.Column("lock_token", sa.String(32)),
        sa.ForeignKeyConstraint(["ticket_id"], ["ticket_mirror.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["user_account.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("ticket_id"),
    )
    op.create_index("ix_ticket_ai_job_available_at", "ticket_ai_job", ["available_at"])


def downgrade():
    op.drop_table("ticket_ai_job")