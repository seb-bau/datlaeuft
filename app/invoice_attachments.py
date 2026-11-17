from hashlib import sha256
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from flask import current_app
from pypdf import PdfReader, PdfWriter

from .models import InvoiceAttachment


MAX_INVOICE_ATTACHMENT_BYTES = 25 * 1024 * 1024


def attachment_root():
    return Path(current_app.config.get("INVOICE_ATTACHMENT_DIR") or Path(current_app.instance_path) / "invoice_attachments").resolve()


def invoice_attachment_path(attachment):
    root = attachment_root()
    path = (root / attachment.storage_name).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Der Dateipfad des Rechnungsanhangs ist ungültig.")
    return path


def validate_invoice_attachment(data):
    if not data or len(data) > MAX_INVOICE_ATTACHMENT_BYTES:
        raise ValueError("Jede PDF-Datei darf höchstens 25 MB groß sein.")
    try:
        reader = PdfReader(BytesIO(data), strict=False)
    except Exception as exc:
        raise ValueError("Die Datei konnte nicht als PDF gelesen werden.") from exc
    if reader.is_encrypted:
        raise ValueError("Passwortgeschützte PDF-Dateien können nicht angehängt werden.")
    try:
        count = len(reader.pages)
        for page in reader.pages:
            page.mediabox
    except Exception as exc:
        raise ValueError("Die Datei konnte nicht als PDF gelesen werden.") from exc
    if not count:
        raise ValueError("Die PDF-Datei enthält keine Seiten.")
    return count


def store_invoice_attachment(invoice, upload, user, position_index):
    name = str(upload.filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name.lower().endswith(".pdf") or len(name) > 255 or any(ord(character) < 32 or ord(character) == 127 for character in name):
        raise ValueError("Bitte eine PDF-Datei mit gültigem Dateinamen auswählen.")
    data = upload.stream.read(MAX_INVOICE_ATTACHMENT_BYTES + 1)
    pages = validate_invoice_attachment(data)
    attachment = InvoiceAttachment(
        invoice=invoice, uploaded_by_user_id=user.id, original_filename=name,
        storage_name=f"{invoice.public_id}/{uuid4().hex}.pdf", sha256=sha256(data).hexdigest(),
        size_bytes=len(data), page_count=pages, position_index=position_index,
    )
    path = invoice_attachment_path(attachment)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as output:
            output.write(data)
    except OSError:
        path.unlink(missing_ok=True)
        raise
    return attachment


def remove_invoice_attachment_files(paths):
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            current_app.logger.exception("invoice_attachment_cleanup_failed path=%s", path)


def append_invoice_attachments(invoice, source, lock=False):
    attachments = (InvoiceAttachment.query.filter_by(invoice_id=invoice.id).populate_existing()
                   .order_by(InvoiceAttachment.position_index, InvoiceAttachment.id).with_for_update().all()) if lock else list(invoice.pdf_attachments)
    if not attachments:
        return source
    try:
        writer = PdfWriter()
        writer.append(PdfReader(BytesIO(source)), import_outline=False)
        for attachment in attachments:
            data = invoice_attachment_path(attachment).read_bytes()
            if sha256(data).hexdigest() != attachment.sha256:
                raise ValueError("Ein Rechnungsanhang wurde verändert. Bitte den Anhang im Entwurf erneut hochladen.")
            reader = PdfReader(BytesIO(data))
            if reader.is_encrypted or len(reader.pages) != attachment.page_count:
                raise ValueError("Ein Rechnungsanhang ist nicht mehr lesbar.")
            for page in reader.pages:
                page.pop("/AA", None)
                annotations = page.get("/Annots")
                for reference in annotations.get_object() if annotations is not None else []:
                    annotation = reference.get_object()
                    annotation.pop("/A", None)
                    annotation.pop("/AA", None)
            writer.append(reader, import_outline=False)
        output = BytesIO()
        writer.write(output)
        return output.getvalue()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("Die PDF-Anhänge konnten nicht vollständig an die Rechnung angefügt werden.") from exc
