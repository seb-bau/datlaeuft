from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from flask import current_app, render_template


def address_lines(item, prefix="recipient") -> list[str]:
    values = [getattr(item, prefix + "_name", None), getattr(item, prefix + "_name_addition", None), getattr(item, prefix + "_street", None)]
    values.append(" ".join(str(getattr(item, prefix + field, "") or "").strip() for field in ("_postal_code", "_town")).strip())
    country = str(getattr(item, prefix + "_country_code", "DE") or "DE")
    if country != "DE":
        values.append(country)
    return [str(value).strip() for value in values if value and str(value).strip()]


def invoice_preview_context(invoice) -> dict:
    from .invoice_document import _company_value, _format_date, _format_money, _prepare_positions, _prepare_vat_rows

    positions = _prepare_positions(invoice)
    net = sum((position["total_net"] for position in positions), Decimal("0"))
    vat = sum((position["vat_amount"] for position in positions), Decimal("0"))
    due = _format_date(invoice.due_date)
    service_from = _format_date(invoice.service_date_from)
    service_to = _format_date(invoice.service_date_to)
    number = invoice.invoice_number or "Entwurf"
    context = {
        "document_title": "Rechnung", "invoice_number": number, "invoice_date_formatted": _format_date(invoice.invoice_date),
        "due_date_formatted": due, "service_date_formatted": service_from if service_from == service_to else " bis ".join(value for value in (service_from, service_to) if value),
        "recipient_address": address_lines(invoice) or ["Empfänger noch nicht angegeben"],
        "contract_number": invoice.recipient_license_agreement_idnum or "", "is_cancellation": False,
        "intro_text": invoice.introductory_text or "", "closing_text": invoice.concluding_text or "",
        "currency": invoice.currency_code or "EUR", "positions": positions, "vat_rows": _prepare_vat_rows(positions),
        "total_net_formatted": _format_money(net), "total_vat_formatted": _format_money(vat),
        "total_gross_formatted": _format_money(net + vat), "total_label": "Rechnungsbetrag",
        "payment_reference": number, "payment_terms_text": f"Bitte überweisen Sie den Rechnungsbetrag bis zum {due}." if due else "",
    }
    for field, company_field in (("seller_iban", "invoice_iban"), ("seller_bic", "invoice_bic"), ("seller_bank_name", "invoice_bank_name")):
        context[field] = str(_company_value(invoice, (field,), (company_field, field), default="") or "")
    return context


def render_document_preview(kind: str, document) -> bytes:
    from weasyprint import HTML

    background = None
    merge = None
    if kind == "invoice":
        from .invoice_document import _apply_stationery, _stationery_path
        context = invoice_preview_context(document)
        filename = getattr(document.company_code, "invoice_stationery_filename", None) or getattr(document.company_code, "stationery_filename", None)
        if filename:
            background, merge = _stationery_path(document), _apply_stationery
        template = "admin/invoice_pdf.html"
    elif kind == "offer":
        from .offer_document import apply_stationery, document_context, prepare_positions, prepare_vat_rows, stationery_path
        positions = prepare_positions(document)
        context = document_context(document, positions, prepare_vat_rows(positions), preview=True)
        if document.company_code.stationery_filename:
            background, merge = stationery_path(document), apply_stationery
        template = "admin/offer_pdf.html"
    elif kind == "commission":
        context = document
        template = "admin/commission_pdf.html"
    else:
        raise ValueError("Die Dokumentart ist ungültig.")
    context = {**context, "preview": True}
    raw_data = HTML(string=render_template(template, **context), base_url=current_app.root_path).write_pdf()
    if background is not None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "content.pdf"
            target = Path(directory) / "preview.pdf"
            source.write_bytes(raw_data)
            merge(source, target, background)
            return target.read_bytes()
    return raw_data
