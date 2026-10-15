from alembic import op
import sqlalchemy as sa


revision = "b3d5f7a9c1e4"
down_revision = "a1c3e5f7b9d2"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "safety_inspection_template_item",
        sa.Column("row_type", sa.String(16), nullable=False, server_default="check"),
    )
    op.add_column("safety_inspection_result", sa.Column("section_key", sa.String(36)))
    op.add_column("safety_inspection_result", sa.Column("section_title", sa.String(255)))


def downgrade():
    connection = op.get_bind()
    headings = connection.execute(sa.text(
        "SELECT id FROM safety_inspection_template_item WHERE row_type = 'heading' LIMIT 1"
    )).first()
    sections = connection.execute(sa.text(
        "SELECT id FROM safety_inspection_result WHERE section_key IS NOT NULL LIMIT 1"
    )).first()
    if headings or sections:
        raise RuntimeError("Ein Downgrade ist mit vorhandenen Überschriften oder gruppierten Prüfergebnissen nicht möglich.")
    with op.batch_alter_table("safety_inspection_result") as batch_op:
        batch_op.drop_column("section_title")
        batch_op.drop_column("section_key")
    with op.batch_alter_table("safety_inspection_template_item") as batch_op:
        batch_op.drop_column("row_type")