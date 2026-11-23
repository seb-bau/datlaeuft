from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from flask import current_app, render_template
from .stationery_layout import company_page_layout


def address_lines(item, prefix="recipient") -> list[str]:
    values = [getattr(item, prefix + "_name", None), getattr(item, prefix + "_name_addition", None), getattr(item, prefix + "_street", None)]
    values.append(" ".join(str(getattr(item, prefix + field, "") or "").strip() for field in ("_postal_code", "_town")).strip())
    country = str(getattr(item, prefix + "_country_code", "DE") or "DE")
    if country != "DE":
        values.append(country)
    return [str(value).strip() for value in values if value and str(value).strip()]


def invoice_preview_context(invoice) -> dict:
    from .invoice_document import _company_value, _format_date, _format_money, _prepare_positions, _prepare_vat_rows
    from .models import InvoiceKind

    positions = _prepare_positions(invoice)
    net = sum((position["total_net"] for position in positions), Decimal("0"))
    vat = sum((position["vat_amount"] for position in positions), Decimal("0"))
    due = _format_date(invoice.due_date)
    service_from = _format_date(invoice.service_date_from)
    service_to = _format_date(invoice.service_date_to)
    number = invoice.invoice_number or "Entwurf"
    is_cancellation = invoice.invoice_kind == InvoiceKind.CANCELLATION.value
    original = invoice.original_invoice if is_cancellation else None
    context = {
        "document_title": "Stornorechnung" if is_cancellation else "Rechnung",
        "invoice_number": number, "invoice_date_formatted": _format_date(invoice.invoice_date),
        "due_date_formatted": due, "service_date_formatted": service_from if service_from == service_to else " bis ".join(value for value in (service_from, service_to) if value),
        "recipient_address": address_lines(invoice) or ["Empfänger noch nicht angegeben"],
        "seller_address": address_lines(invoice, "seller") or address_lines(invoice.company_code, "seller"),
        "contract_number": invoice.recipient_license_agreement_idnum or "", "is_cancellation": is_cancellation,
        "original_invoice_number": original.invoice_number if original else "",
        "original_invoice_date_formatted": _format_date(original.invoice_date) if original else "",
        "cancellation_reason": invoice.cancellation_reason or "",
        "tenant_billing_reference": invoice.tenant_billing_reference,
        "insurance_claim_number": invoice.insurance_claim_number,
        "intro_text": invoice.introductory_text or "", "closing_text": invoice.concluding_text or "",
        "currency": invoice.currency_code or "EUR", "positions": positions, "vat_rows": _prepare_vat_rows(positions),
        "total_net_formatted": _format_money(net), "total_vat_formatted": _format_money(vat),
        "total_gross_formatted": _format_money(net + vat), "total_label": "Stornobetrag" if is_cancellation else "Rechnungsbetrag",
        "payment_reference": number, "payment_terms_text": f"Bitte überweisen Sie den Rechnungsbetrag bis zum {due}." if due else "",
    }
    for field, company_field in (("seller_iban", "invoice_iban"), ("seller_bic", "invoice_bic"), ("seller_bank_name", "invoice_bank_name")):
        context[field] = str(_company_value(invoice, (field,), (company_field, field), default="") or "")
    from .invoice_payments import invoice_payment_context
    if not is_cancellation:
        context.update(invoice_payment_context(invoice))
    return context


def render_invoice_preview(invoice, *, layout=None, include_attachments=True) -> bytes:
    from .invoice_attachments import append_invoice_attachments
    from .invoice_document import _apply_stationery, _stationery_path, render_invoice_content_pdf

    content = render_invoice_content_pdf({**invoice_preview_context(invoice), "preview": True}, layout=layout, page_layout=company_page_layout(invoice.company_code))
    company = invoice.company_code
    if getattr(company, "invoice_stationery_filename", None) or company.stationery_filename:
        with TemporaryDirectory() as directory:
            source, target = Path(directory) / "content.pdf", Path(directory) / "preview.pdf"
            source.write_bytes(content)
            _apply_stationery(source, target, _stationery_path(invoice))
            content = target.read_bytes()
    if include_attachments:
        content = append_invoice_attachments(invoice, content)
    return content


def render_offer_preview(offer, *, layout=None) -> bytes:
    from .offer_document import (
        apply_stationery, document_context, prepare_positions, prepare_vat_rows,
        render_offer_content_pdf, stationery_path,
    )

    positions = prepare_positions(offer)
    context = document_context(offer, positions, prepare_vat_rows(positions), preview=True)
    content = render_offer_content_pdf(context, layout=layout, page_layout=company_page_layout(offer.company_code))
    if offer.company_code.stationery_filename:
        with TemporaryDirectory() as directory:
            source, target = Path(directory) / "content.pdf", Path(directory) / "preview.pdf"
            source.write_bytes(content)
            apply_stationery(source, target, stationery_path(offer))
            content = target.read_bytes()
    return content


def render_document_preview(kind: str, document) -> bytes:
    if kind == "invoice":
        return render_invoice_preview(document)
    if kind == "offer":
        return render_offer_preview(document)
    if kind == "commission":
        from weasyprint import HTML

        html = render_template("admin/commission_pdf.html", **{**document, "preview": True})
        return HTML(string=html, base_url=current_app.root_path).write_pdf()
    raise ValueError("Die Dokumentart ist ungültig.")
