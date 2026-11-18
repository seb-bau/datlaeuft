from pathlib import Path

from jinja2 import StrictUndefined, TemplateError, meta, nodes
from jinja2.sandbox import ImmutableSandboxedEnvironment


DEFAULT_COVER_LETTER_LAYOUT = (Path(__file__).parent / "templates" / "admin" / "invoice_cover_letter_pdf.html").read_text(encoding="utf-8")

COVER_LETTER_LAYOUT_FIELDS = {
    "title": "Überschrift des Anschreibens",
    "text": "Individueller Anschreibentext; Zeilenumbrüche mit white-space: pre-wrap darstellen",
    "recipient_address": "Vollständige Empfängeranschrift mit Zeilenumbrüchen",
    "recipient_name": "Name des Empfängers",
    "recipient_name_addition": "Namenszusatz des Empfängers",
    "recipient_street": "Straße und Hausnummer des Empfängers",
    "recipient_postal_code": "Postleitzahl des Empfängers",
    "recipient_town": "Ort des Empfängers",
    "recipient_country_code": "Ländercode des Empfängers",
    "invoice_number": "Rechnungsnummer oder Entwurfskennung",
    "invoice_date": "Rechnungsdatum",
    "contract_number": "Vertragsnummer",
    "letter_date": "Datum des Anschreibens",
    "caseworker_name": "Name des Sachbearbeiters bzw. Erstellers",
    "caseworker_email": "E-Mail-Adresse des Sachbearbeiters",
    "caseworker_phone": "Telefonnummer des Sachbearbeiters",
    "sender_line": "Optionale vollständige Absenderzeile; im Standard ausgeblendet",
    "seller_name": "Name des Rechnungsausstellers",
    "amount": "Formatierter Bruttobetrag",
    "currency": "Währung",
    "due_date": "Fälligkeitsdatum",
    "payment_terms": "Zahlungshinweise der Rechnung",
    "preview": "Im Rechnungsentwurf wahr; für einen optionalen Entwurfshinweis",
}

LAYOUT_ENV = ImmutableSandboxedEnvironment(undefined=StrictUndefined, autoescape=True)
LAYOUT_ENV.globals.clear()
LAYOUT_ENV.filters.clear()


def compile_cover_letter_layout(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 50000 or "\x00" in value:
        raise ValueError("Bitte ein HTML-Layout mit höchstens 50.000 Zeichen ohne Nullzeichen angeben.")
    try:
        tree = LAYOUT_ENV.parse(value)
        allowed = (nodes.Template, nodes.Output, nodes.TemplateData, nodes.Name, nodes.If, nodes.Not, nodes.And, nodes.Or, nodes.Const)
        if any(not isinstance(node, allowed) for node in (tree, *tree.find_all(nodes.Node))):
            raise ValueError("Im Anschreiben-Layout sind HTML, CSS, die aufgeführten Platzhalter und einfache if/else-Bedingungen erlaubt.")
        unknown = meta.find_undeclared_variables(tree) - set(COVER_LETTER_LAYOUT_FIELDS)
        if unknown:
            raise ValueError("Unbekannte Platzhalter im Anschreiben-Layout: " + ", ".join(sorted(unknown)))
        return LAYOUT_ENV.from_string(value)
    except TemplateError as exc:
        raise ValueError(f"Das Anschreiben-Layout ist ungültig: {exc}") from exc


def validate_cover_letter_layout(value):
    compile_cover_letter_layout(value)
    return value


def render_cover_letter_html(context, layout):
    values = {name: str(context.get(name) or "") for name in COVER_LETTER_LAYOUT_FIELDS}
    values["preview"] = bool(context.get("preview"))
    html = compile_cover_letter_layout(layout).render(values)
    if len(html) > 2000000:
        raise ValueError("Das erzeugte Anschreiben ist zu lang. Bitte das Layout kürzen.")
    return html
