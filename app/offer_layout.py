from pathlib import Path

from .document_layout import compile_document_layout, document_layout_values, render_document_layout


DEFAULT_OFFER_LAYOUT = (Path(__file__).parent / "templates" / "admin" / "offer_pdf.html").read_text(encoding="utf-8")
OFFER_LAYOUT_FIELDS = {
    "document_title": "Angebot",
    "offer_number": "Angebotsnummer oder Entwurfskennung",
    "offer_date_formatted": "Angebotsdatum",
    "valid_until_formatted": "Gültigkeitsdatum",
    "recipient_address": "Empfängeranschrift als Liste von Zeilen",
    "recipient_name": "Name des Angebotsempfängers",
    "contract_number": "Vertragsnummer, falls hinterlegt",
    "object_address": "Anschrift des Objekts",
    "object_location": "Lage des Objekts",
    "craft_activity_name": "Gewerk",
    "title": "Titel des Angebots",
    "introductory_text": "Einleitungstext des Angebots",
    "concluding_text": "Schlusstext des Angebots",
    "positions": "Liste der Angebotspositionen",
    "show_article_numbers": "Wahr, wenn mindestens eine Position eine Artikelnummer hat",
    "vat_rows": "Liste der Summen nach Umsatzsteuersatz",
    "total_net_formatted": "Nettobetrag",
    "total_vat_formatted": "Umsatzsteuerbetrag",
    "total_gross_formatted": "Bruttobetrag",
    "currency": "Währung der Beträge",
    "preview": "Wahr in der PDF-Vorschau",
}
_LAYOUT_LABEL = "Angebots-Layout"


def render_offer_html(context, layout):
    template = compile_document_layout(layout, label=_LAYOUT_LABEL, fields=OFFER_LAYOUT_FIELDS)
    return render_document_layout(template, document_layout_values(context, OFFER_LAYOUT_FIELDS), label=_LAYOUT_LABEL)


def example_offer_layout_context() -> dict:
    return {
        "document_title": "Angebot", "offer_number": "AN-BEISPIEL-001", "offer_date_formatted": "17.11.2026",
        "valid_until_formatted": "17.12.2026", "recipient_name": "Max Mustermann",
        "recipient_address": ["Max Mustermann", "Musterstraße 12", "12345 Musterstadt"],
        "contract_number": "1000/123/01", "object_address": "Musterstraße 12", "object_location": "1. Obergeschoss",
        "craft_activity_name": "Sanitär", "title": "Reparatur im Badezimmer",
        "introductory_text": "Vielen Dank für Ihre Anfrage. Wir bieten Ihnen folgende Leistungen an:",
        "concluding_text": "Wir freuen uns auf Ihre Rückmeldung.",
        "currency": "EUR", "total_net_formatted": "100,00", "total_vat_formatted": "19,00", "total_gross_formatted": "119,00",
        "positions": [{
            "number": 1, "article_number": "REP-001", "description": "Reparaturarbeiten", "details": "Dichtung austauschen",
            "quantity_formatted": "2", "unit": "Stk", "unit_price_formatted": "50,00",
            "vat_rate_formatted": "19", "total_net_formatted": "100,00",
        }],
        "vat_rows": [{"rate_formatted": "19", "net_formatted": "100,00", "vat_formatted": "19,00"}],
    }


def validate_offer_layout(value: str) -> str:
    example = example_offer_layout_context()
    render_offer_html(example, value)
    example["positions"][0]["article_number"] = ""
    render_offer_html({**example, "preview": True}, value)
    return value
