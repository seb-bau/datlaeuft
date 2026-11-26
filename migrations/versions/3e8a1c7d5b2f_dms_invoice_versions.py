from alembic import op
import sqlalchemy as sa


revision = "3e8a1c7d5b2f"
down_revision = "6b421e9c8d30"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("dms_document", sa.Column("invoice_version", sa.Integer()))
    documents = sa.table(
        "dms_document",
        sa.column("id", sa.String()), sa.column("entity_type", sa.String()),
        sa.column("entity_id", sa.Integer()), sa.column("document_type", sa.String()),
        sa.column("created_at", sa.DateTime()), sa.column("invoice_version", sa.Integer()),
        sa.column("status", sa.String()), sa.column("error", sa.Text()),
    )
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(documents.c.id, documents.c.entity_id, documents.c.status)
        .where(
            documents.c.entity_type == "invoice",
            documents.c.document_type.in_(("invoice", "cancellation_invoice")),
        )
        .order_by(documents.c.entity_id, documents.c.created_at.desc(), documents.c.id.desc())
    ).all()
    remaining = {}
    for _, invoice_id, _ in rows:
        remaining[invoice_id] = remaining.get(invoice_id, 0) + 1
    already_archived = set()
    for document_id, invoice_id, status in rows:
        values = {"invoice_version": remaining[invoice_id]}
        remaining[invoice_id] -= 1
        if status == "archived":
            already_archived.add(invoice_id)
        elif invoice_id in already_archived and status != "cancelled":
            values.update(status="cancelled", error="Eine neuere Fassung der Rechnung wurde bereits archiviert.")
        connection.execute(
            documents.update().where(documents.c.id == document_id).values(**values)
        )
    op.create_index(
        "ix_dms_document_invoice_version", "dms_document",
        ["entity_type", "entity_id", "invoice_version"], unique=True,
    )


def downgrade():
    op.drop_index("ix_dms_document_invoice_version", table_name="dms_document")
    op.drop_column("dms_document", "invoice_version")
