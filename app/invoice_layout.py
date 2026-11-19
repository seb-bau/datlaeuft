from pathlib import Path

from jinja2 import TemplateError, meta, nodes
from jinja2.defaults import DEFAULT_FILTERS

from .invoice_cover_letter_layout import LAYOUT_ENV


DEFAULT_INVOICE_LAYOUT = (Path(__file__).parent / "templates" / "admin" / "invoice_pdf.html").read_text(encoding="utf-8")
INVOICE_LAYOUT_LIMIT = 50000
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
INVOICE_POSITION_FIELDS = (
    "number", "article_number", "description", "details", "quantity_formatted", "unit",
    "unit_price_formatted", "vat_rate_formatted", "total_net_formatted",
)
INVOICE_VAT_FIELDS = ("rate_formatted", "net_formatted", "vat_formatted")
_REFERENCE_FIELDS = ("tenant_name", "property_address", "agreement_number")
_LIST_FIELDS = {"recipient_address", "seller_address", "positions", "vat_rows"}
_MEMBER_FIELDS = {*INVOICE_POSITION_FIELDS, *INVOICE_VAT_FIELDS, *_REFERENCE_FIELDS}
_ALLOWED_NODES = (
    nodes.Template, nodes.Output, nodes.TemplateData, nodes.Name, nodes.Const,
    nodes.If, nodes.And, nodes.Or, nodes.Not, nodes.Compare, nodes.Operand, nodes.CondExpr,
    nodes.For, nodes.Getattr, nodes.Filter,
)
_LAYOUT_ENV = LAYOUT_ENV.overlay()
_LAYOUT_ENV.filters = {"join": DEFAULT_FILTERS["join"]}


def _compile_invoice_layout(value):
    if not isinstance(value, str) or not value.strip() or len(value) > INVOICE_LAYOUT_LIMIT or "\x00" in value:
        raise ValueError("Bitte ein Rechnungs-Layout mit höchstens 50.000 Zeichen ohne Nullzeichen angeben.")
    try:
        tree = _LAYOUT_ENV.parse(value)
        if any(not isinstance(node, _ALLOWED_NODES) for node in (tree, *tree.find_all(nodes.Node))):
            raise ValueError("Im Rechnungs-Layout sind HTML, CSS, die aufgeführten Platzhalter, Bedingungen und Schleifen über die Rechnungslisten erlaubt.")
        for loop in tree.find_all(nodes.For):
            if (
                not isinstance(loop.target, nodes.Name) or not isinstance(loop.iter, nodes.Name)
                or loop.iter.name not in _LIST_FIELDS or loop.recursive or loop.test is not None
                or any(loop.find_all(nodes.For))
            ):
                raise ValueError("Schleifen im Rechnungs-Layout dürfen nur über Adresszeilen, Positionen oder Steuersummen laufen.")
        for expression in tree.find_all(nodes.Filter):
            if (
                expression.name != "join" or not isinstance(expression.node, nodes.Name)
                or expression.node.name not in {"recipient_address", "seller_address"}
                or len(expression.args) != 1 or not isinstance(expression.args[0], nodes.Const)
                or not isinstance(expression.args[0].value, str)
                or expression.kwargs or expression.dyn_args is not None or expression.dyn_kwargs is not None
            ):
                raise ValueError("Der Filter join ist für die Adresszeilen mit einem festen Trennzeichen verfügbar.")
        unknown = meta.find_undeclared_variables(tree) - INVOICE_LAYOUT_FIELDS.keys()
        unknown.update(field.attr for field in tree.find_all(nodes.Getattr) if field.attr not in _MEMBER_FIELDS)
        if unknown:
            raise ValueError("Unbekannte Platzhalter im Rechnungs-Layout: " + ", ".join(sorted(unknown)))
        return _LAYOUT_ENV.from_string(value)
    except (TemplateError, RecursionError) as exc:
        raise ValueError(f"Das Rechnungs-Layout ist ungültig: {exc}") from exc


def _render_invoice_layout(template, context):
    values = {
        name: str(context.get(name) or "") for name in INVOICE_LAYOUT_FIELDS
        if name not in _LIST_FIELDS and name != "tenant_billing_reference"
    }
    for name in ("preview", "is_cancellation"):
        values[name] = bool(context.get(name))
    for name in ("recipient_address", "seller_address"):
        values[name] = tuple(str(line) for line in context.get(name, ()))
    for name, fields in (("positions", INVOICE_POSITION_FIELDS), ("vat_rows", INVOICE_VAT_FIELDS)):
        values[name] = tuple({field: str(row.get(field) or "") for field in fields} for row in context.get(name, ()))
    values["show_article_numbers"] = any(position["article_number"] for position in values["positions"])
    reference = context.get("tenant_billing_reference")
    values["tenant_billing_reference"] = {field: str(reference.get(field) or "") for field in _REFERENCE_FIELDS} if reference else {}
    if values["is_cancellation"] or reference:
        for name in values:
            if name.startswith("payment_"):
                values[name] = ""
    remaining = 2000000
    chunks = []
    try:
        for chunk in template.generate(values):
            remaining -= len(chunk)
            if remaining < 0:
                raise ValueError("Das erzeugte Rechnungs-HTML ist zu lang. Bitte das Layout kürzen.")
            chunks.append(chunk)
    except (TemplateError, TypeError, RecursionError) as exc:
        raise ValueError(f"Das Rechnungs-Layout ist ungültig: {exc}") from exc
    return "".join(chunks)


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
