from pathlib import Path

from .document_layout import compile_document_layout, document_layout_values, render_document_layout


DEFAULT_INVOICE_LAYOUT = (Path(__file__).parent / "templates" / "admin" / "invoice_pdf.html").read_text(encoding="utf-8")
INVOICE_LAYOUT_FIELDS = {
    "document_title": "Rechnung oder Stornorechnung",
    "invoice_number": "Rechnungsnummer oder Entwurfskennung",
    "invoice_date_formatted": "Rechnungsdatum",
    "due_date_formatted": "Fälligkeitsdatum",
    "service_date_formatted": "Leistungsdatum oder Leistungszeitraum",
    "recipient_address": "Anschrift des Rechnungsempfängers als Liste von Zeilen",
    "seller_address": "Anschrift des Ausstellers als Liste von Zeilen",
    "contract_number": "Vertragsnummer bei direkter Abrechnung",
    "tenant_billing_reference": "Leistungsbezug mit tenant_name, property_address und agreement_number",
    "insurance_claim_number": "Versicherungsfall-Nr., falls in der Rechnung hinterlegt",
    "intro_text": "Einleitungstext der Rechnung",
    "closing_text": "Schlusstext der Rechnung",
    "positions": "Liste der Rechnungspositionen",
    "show_article_numbers": "Wahr, wenn mindestens eine Position eine Artikelnummer hat",
    "vat_rows": "Liste der Summen nach Umsatzsteuersatz",
    "total_net_formatted": "Nettobetrag",
    "total_vat_formatted": "Umsatzsteuerbetrag",
    "total_gross_formatted": "Bruttobetrag",
    "total_label": "Rechnungsbetrag oder Stornobetrag",
    "currency": "Währung der Beträge",
    "payment_terms_text": "Zahlungshinweise",
    "payment_method": "bank_transfer oder sepa_debit",
    "payment_iban_display": "IBAN für die Anzeige, bei Lastschrift gekürzt",
    "payment_account_holder": "Kontoinhaber",
    "payment_bic": "BIC für Überweisungen",
    "payment_mandate_reference": "SEPA-Mandatsreferenz",
    "payment_creditor_id": "SEPA-Gläubiger-ID",
    "payment_reference": "Verwendungszweck",
    "is_cancellation": "Wahr bei Stornorechnungen",
    "original_invoice_number": "Nummer der stornierten Rechnung",
    "original_invoice_date_formatted": "Datum der stornierten Rechnung",
    "cancellation_reason": "Begründung des Stornos",
    "preview": "Wahr in der PDF-Vorschau",
}
_REFERENCE_FIELDS = ("tenant_name", "property_address", "agreement_number")
_LAYOUT_LABEL = "Rechnungs-Layout"


def _compile_invoice_layout(value):
    return compile_document_layout(
        value, label=_LAYOUT_LABEL, fields=INVOICE_LAYOUT_FIELDS, additional_members=_REFERENCE_FIELDS,
    )


def _render_invoice_layout(template, context):
    values = document_layout_values(context, INVOICE_LAYOUT_FIELDS)
    values["is_cancellation"] = bool(context.get("is_cancellation"))
    reference = context.get("tenant_billing_reference")
    values["tenant_billing_reference"] = {field: str(reference.get(field) or "") for field in _REFERENCE_FIELDS} if reference else {}
    if values["is_cancellation"] or reference:
        for name in values:
            if name.startswith("payment_"):
                values[name] = ""
    return render_document_layout(template, values, label=_LAYOUT_LABEL)


def render_invoice_html(context, layout):
    return _render_invoice_layout(_compile_invoice_layout(layout), context)


def example_invoice_layout_context() -> dict:
    return {
        "document_title": "Rechnung", "invoice_number": "RE-BEISPIEL-001", "invoice_date_formatted": "17.11.2026",
        "due_date_formatted": "01.12.2026", "service_date_formatted": "16.11.2026",
        "recipient_address": ["Max Mustermann", "Musterstraße 12", "12345 Musterstadt"],
        "seller_address": ["Beispielunternehmen", "Firmenstraße 1", "12345 Musterstadt"],
        "contract_number": "1000/123/01", "insurance_claim_number": "VS-BEISPIEL-42",
        "intro_text": "Wir berechnen Ihnen die folgenden Leistungen.",
        "closing_text": "Vielen Dank für Ihren Auftrag.",
        "currency": "EUR", "total_label": "Rechnungsbetrag",
        "total_net_formatted": "100,00", "total_vat_formatted": "19,00", "total_gross_formatted": "119,00",
        "positions": [{
            "number": 1, "article_number": "REP-001", "description": "Reparaturarbeiten", "details": "Dichtung ausgetauscht",
            "quantity_formatted": "2", "unit": "Stk", "unit_price_formatted": "50,00",
            "vat_rate_formatted": "19", "total_net_formatted": "100,00",
        }],
        "vat_rows": [{"rate_formatted": "19", "net_formatted": "100,00", "vat_formatted": "19,00"}],
        "payment_method": "bank_transfer", "payment_reference": "RE-BEISPIEL-001",
        "payment_terms_text": "Bitte überweisen Sie den Rechnungsbetrag bis zum 01.12.2026.",
        "payment_iban_display": "DE89 3704 0044 0532 0130 00", "payment_bic": "COBADEFFXXX",
        "payment_account_holder": "Beispielunternehmen",
    }


def validate_invoice_layout(value: str) -> str:
    template = _compile_invoice_layout(value)
    example = example_invoice_layout_context()
    for changes in (
        {},
        {"preview": True, "tenant_billing_reference": {
            "tenant_name": "Max Mustermann", "property_address": "Musterstraße 12", "agreement_number": "1000/123/01",
        }},
        {"is_cancellation": True, "original_invoice_number": "RE-BEISPIEL-001", "cancellation_reason": "Korrektur"},
        {"payment_method": "sepa_debit", "payment_iban_display": "DE12 **** **** **** 9890",
         "payment_mandate_reference": "MANDAT-01", "payment_creditor_id": "DE98ZZZ09999999999"},
    ):
        _render_invoice_layout(template, {**example, **changes})
    return value
