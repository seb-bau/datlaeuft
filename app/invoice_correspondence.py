from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
from zoneinfo import ZoneInfo

from flask import current_app, render_template
from pypdf import PdfReader, PdfWriter
from weasyprint import HTML

from .document_previews import address_lines, invoice_preview_context, render_document_preview
from .invoice_document import InvoiceDocumentError, _apply_stationery, _stationery_path, resolve_invoice_document_path
from .models import utc_now
from .settings import SettingKeys, settings, validate_cover_letter_text


def letter_date():
    return utc_now().astimezone(ZoneInfo("Europe/Berlin")).date()


def cover_letter_values(invoice):
    return {
        "title": invoice.cover_letter_title if invoice.cover_letter_title is not None else settings.get(SettingKeys.INVOICE_COVER_LETTER_TITLE),
        "text": invoice.cover_letter_text if invoice.cover_letter_text is not None else settings.get(SettingKeys.INVOICE_COVER_LETTER_TEXT),
        "saved": invoice.cover_letter_title is not None,
    }


def cover_letter_context(invoice, title, text):
    title = validate_cover_letter_text(title, title=True)
    text = validate_cover_letter_text(text)
    values = invoice_preview_context(invoice)
    creator = invoice.created_by
    contact = {
        "name": creator.display_name if creator else "",
        "email": creator.email if creator else invoice.seller_email or invoice.company_code.seller_email or "",
        "phone": (creator.phone if creator else None) or invoice.seller_phone or invoice.company_code.seller_phone or "",
    }
    fields = {
        "invoice_number": invoice.invoice_number or f"Entwurf {invoice.id}",
        "invoice_date": values["invoice_date_formatted"],
        "contract_number": invoice.recipient_license_agreement_idnum or "",
        "recipient_name": invoice.recipient_name or "",
        "amount": values["total_gross_formatted"], "currency": invoice.currency_code or "EUR",
        "due_date": values["due_date_formatted"], "payment_terms": values["payment_terms_text"],
        "seller_name": invoice.seller_name or invoice.company_code.seller_name or invoice.company_code.name or "",
        "caseworker_name": contact["name"], "caseworker_email": contact["email"], "caseworker_phone": contact["phone"],
    }
    return {
        "title": title.format_map(fields), "text": text.format_map(fields),
        "contact": contact, "invoice_reference": fields["invoice_number"],
        "contract_number": fields["contract_number"], "recipient_address": address_lines(invoice),
        "sender_line": " · ".join(address_lines(invoice, "seller") or address_lines(invoice.company_code, "seller")),
        "letter_date": (invoice.cover_letter_date or letter_date()).strftime("%d.%m.%Y"),
        "preview": invoice.status == "draft",
    }


def cover_letter_stationery(invoice):
    filename = invoice.company_code.cover_letter_stationery_filename
    if not filename:
        if invoice.company_code.stationery_filename:
            return _stationery_path(invoice)
        return None
    root = (Path(current_app.instance_path) / "invoice_stationery").resolve()
    path = (root / filename).resolve()
    if path.parent != root or not path.is_file():
        raise ValueError("Der hinterlegte Briefbogen für das Anschreiben ist nicht verfügbar.")
    return path


def render_cover_letter(invoice, title, text):
    content = HTML(
        string=render_template("admin/invoice_cover_letter_pdf.html", **cover_letter_context(invoice, title, text)),
        base_url=current_app.root_path,
    ).write_pdf()
    background = cover_letter_stationery(invoice)
    if background is not None:
        with TemporaryDirectory() as directory:
            source, target = Path(directory) / "letter.pdf", Path(directory) / "result.pdf"
            source.write_bytes(content)
            _apply_stationery(source, target, background)
            content = target.read_bytes()
    return content


def prepend_cover_letter(invoice, data, *, title=None, text=None):
    if title is None and invoice.cover_letter_title is None:
        return data
    values = cover_letter_values(invoice)
    letter = render_cover_letter(invoice, values["title"] if title is None else title, values["text"] if text is None else text)
    writer = PdfWriter(clone_from=PdfReader(BytesIO(data)))
    for index, page in enumerate(PdfReader(BytesIO(letter)).pages):
        writer.insert_page(page, index)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def invoice_correspondence_pdf(invoice, *, title=None, text=None):
    if invoice.status == "draft":
        data = render_document_preview("invoice", invoice)
    else:
        from .invoice_delivery import final_invoice_pdf
        data = final_invoice_pdf(invoice)
    return prepend_cover_letter(invoice, data, title=title, text=text)


def store_delivery_correspondence(invoice):
    if invoice.cover_letter_title is None:
        return {}
    content = invoice_correspondence_pdf(invoice)
    path = Path(current_app.instance_path) / "invoice_documents" / "correspondence" / f"{invoice.id}-{uuid4().hex}.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return {"correspondence_filename": str(path.relative_to(current_app.instance_path)), "correspondence_sha256": sha256(content).hexdigest()}


def delivery_correspondence_pdf(invoice, payload):
    from .invoice_delivery import final_invoice_pdf
    original = final_invoice_pdf(invoice)
    filename = payload.get("correspondence_filename")
    if not filename:
        return original
    try:
        content = resolve_invoice_document_path(filename).read_bytes()
    except (InvoiceDocumentError, OSError) as exc:
        raise ValueError("Die zum Versand vorgemerkte PDF mit Anschreiben ist nicht verfügbar.") from exc
    if sha256(content).hexdigest() != payload.get("correspondence_sha256"):
        raise ValueError("Die zum Versand vorgemerkte PDF mit Anschreiben wurde verändert.")
    return content
