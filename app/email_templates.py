from __future__ import annotations

import json
import re
from html import escape

from jinja2 import StrictUndefined, TemplateError, meta, nodes
from jinja2.sandbox import ImmutableSandboxedEnvironment

from .models import AppSetting, SettingValueType
from .settings import SettingDefinition, SettingKeys, normalize_external_portal_name, settings


ORDER_FIELDS = "commission_number commission_title description damage_location address location tenant_name positions commission_url"

ORDER_DETAILS = """Auftrag: {{ commission_number }}
Titel: {{ commission_title }}
Beschreibung: {{ description }}
Schadensort: {{ damage_location }}
Adresse: {{ address or '-' }}
Lage: {{ location }}

Leistungspositionen:
{% for position in positions %}{{ loop.index }}. {{ position.text }} · Menge {{ position.units }}
{% else %}Keine Leistungspositionen vorhanden
{% endfor %}"""

GREETING = "Guten Tag{% if recipient_name %} {{ recipient_name }}{% endif %},\n\n"

APPOINTMENT_LINE = "{{ appointment.date }}, {% if appointment.custom %}Beginn {{ appointment.start }} Uhr{% else %}{{ appointment.start }} bis {{ appointment.end }} Uhr ({{ appointment.window_name }}){% endif %}"

APPOINTMENT_DETAILS = "Handwerker: {{ craftsman_name or 'Ihr Handwerker' }}{% if topic %}\nThema: {{ topic }}{% endif %}"

EMAIL_TEMPLATES = {
    "tenant_assignment": {
        "label": "Mieter: Beauftragung",
        "fields": ORDER_FIELDS + " craft_names craftsman_name",
        "subject": "Information zur Beauftragung",
        "body": "Guten Tag,\n\nfür Ihr Mietverhältnis wurde eine Beauftragung erteilt.\n\nGewerk: {{ craft_names|join('; ') }}\nBeauftragt: {{ craftsman_name }}\n\nFreundliche Grüße\n",
    },
    "completion": {
        "label": "Sachbearbeitung: Auftrag abgeschlossen",
        "fields": ORDER_FIELDS + " review_pending",
        "subject": "Auftrag {{ commission_number }} abgeschlossen",
        "body": "Guten Tag,\n\nder von Ihnen angelegte Auftrag {{ commission_number }} wurde als abgeschlossen gemeldet.\n\nAuftrag: {{ commission_title }}{% if address %}\nObjekt: {{ address }}{% endif %}{% if review_pending %}\n\nDie Mieterbelastung befindet sich noch in Prüfung.{% endif %}{% if commission_url %}\n\n{{ commission_url }}{% endif %}\n\nFreundliche Grüße\n",
    },
    "external_invitation": {
        "label": "Handwerkerportal: Einladung",
        "fields": "portal_name recipient_name username invitation_url valid_hours",
        "required": ("invitation_url",),
        "subject": "{{ portal_name }} – Einladung",
        "body": GREETING + "für Sie wurde ein Zugang zum {{ portal_name }}-Auftragnehmerportal eingerichtet.\n\nBenutzername: {{ username }}\n\nÜber den folgenden Link vergeben Sie Ihr persönliches Kennwort:\n{{ invitation_url }}\n\nDer Link ist {{ valid_hours }} Stunden gültig.",
    },
    "external_assignment": {
        "label": "Handwerkerportal: Neuer Auftrag",
        "fields": ORDER_FIELDS + " portal_name recipient_name",
        "subject": "{{ portal_name }} – Neuer Auftrag: {{ commission_number }}",
        "body": GREETING + "Ihnen wurde ein Auftrag zugewiesen.\n\n" + ORDER_DETAILS + "\nAuftrag im Portal öffnen:\n{{ commission_url }}",
    },
    "appointment_request": {
        "label": "Termin: Vorschläge an Bewohner",
        "fields": ORDER_FIELDS + " recipient_name craftsman_name topic message proposals public_url",
        "required": ("public_url",),
        "subject": "Terminvorschläge zu Auftrag {{ commission_number }}",
        "body": GREETING + "zu Ihrem Auftrag {{ commission_number }} stehen folgende Terminvorschläge zur Auswahl:\n\n" + APPOINTMENT_DETAILS + "\n\n{% for proposal in proposals %}- " + APPOINTMENT_LINE.replace("appointment.", "proposal.") + "\n{% endfor %}{% if message %}\nNachricht des Handwerkers:\n{{ message }}{% endif %}\n\nBitte öffnen Sie den folgenden Link, um einen Termin anzunehmen oder alle Vorschläge abzulehnen:\n\n{{ public_url }}\n\nFür die Terminbestätigung ist keine Anmeldung erforderlich.",
    },
    "appointment_confirmation": {
        "label": "Termin: Bestätigung an Bewohner",
        "fields": ORDER_FIELDS + " recipient_name craftsman_name topic appointment custom_appointment calendar_duration_minutes",
        "subject": "Terminbestätigung – Auftrag {{ commission_number }}",
        "body": GREETING + "Ihr Termin wurde bestätigt:\n\n" + APPOINTMENT_LINE + "\n\nAuftrag: {{ commission_number }}\n" + APPOINTMENT_DETAILS + "{% if address %}\nAdresse: {{ address }}{% endif %}\n\n{% if custom_appointment %}Die Kalenderdatei reserviert ab dem Terminbeginn {{ calendar_duration_minutes }} Minuten. Die tatsächliche Arbeitsdauer kann davon abweichen.{% else %}Öffnen Sie die Kalenderdatei im Anhang, um den Termin in Ihren Kalender zu übernehmen.{% endif %}\n",
    },
    "appointment_accepted": {
        "label": "Termin: Zusage an Handwerker",
        "fields": ORDER_FIELDS + " recipient_name appointment tenant_comment",
        "subject": "Termin bestätigt – Auftrag {{ commission_number }}",
        "body": GREETING + "der Bewohner hat folgenden Termin bestätigt:\n\n" + APPOINTMENT_LINE + "\n\nAuftrag: {{ commission_number }}{% if address %}\nAdresse: {{ address }}{% endif %}{% if tenant_comment %}\n\nKommentar des Bewohners:\n{{ tenant_comment }}{% endif %}",
    },
    "appointment_rejected": {
        "label": "Termin: Absage an Handwerker",
        "fields": ORDER_FIELDS + " recipient_name tenant_comment",
        "subject": "Terminvorschläge abgelehnt – Auftrag {{ commission_number }}",
        "body": GREETING + "der Bewohner hat alle Terminvorschläge abgelehnt.\n\nAuftrag: {{ commission_number }}{% if address %}\nAdresse: {{ address }}{% endif %}{% if tenant_comment %}\n\nKommentar des Bewohners:\n{{ tenant_comment }}{% endif %}",
    },
    "commission_rejected": {
        "label": "Auftragsstatus: Auftrag abgelehnt",
        "fields": ORDER_FIELDS + " craftsman_name reason",
        "subject": "Auftrag abgelehnt: {{ commission_number }}",
        "body": "Auftrag abgelehnt\n\n" + ORDER_DETAILS + "Mieter: {{ tenant_name }}\nHandwerker: {{ craftsman_name }}\n\nBegründung / Kommentar:\n{{ reason }}",
    },
    "commission_unsuccessful": {
        "label": "Auftragsstatus: Nicht erfolgreich abgeschlossen",
        "fields": ORDER_FIELDS + " craftsman_name reason",
        "subject": "Auftrag nicht erfolgreich abgeschlossen: {{ commission_number }}",
        "body": "Auftrag nicht erfolgreich abgeschlossen\n\n" + ORDER_DETAILS + "Mieter: {{ tenant_name }}\nHandwerker: {{ craftsman_name }}\n\nBegründung / Kommentar:\n{{ reason }}",
    },
    "process_internal": {
        "label": "Vorgang: Neue Information an Sachbearbeitung",
        "fields": "process_id author_name commission_numbers message",
        "subject": "Neue Information zu Vorgang {{ process_id }}",
        "body": "Handwerker: {{ author_name or 'Unbekannt' }}\nVorgang: {{ process_id }}\nAufträge: {{ commission_numbers|join(', ') }}\n\n{{ message }}",
    },
    "process_external": {
        "label": "Handwerkerportal: Neue Zusatzinformation",
        "fields": ORDER_FIELDS + " portal_name recipient_name author_name message",
        "subject": "{{ portal_name }} – Neue Zusatzinformation zu Auftrag {{ commission_number }}",
        "body": GREETING + "zu Ihrem Auftrag wurde eine neue Zusatzinformation erfasst.\n\nAuftrag: {{ commission_number }}\nTitel: {{ commission_title }}\nAbsender: {{ author_name or 'Sachbearbeitung' }}\n\n{{ message }}\n\nDen vollständigen Verlauf finden Sie hier:\n{{ commission_url }}",
    },
    "appointment_calendar": {
        "label": "Termin: Kalendereintrag im Anhang",
        "fields": ORDER_FIELDS + " recipient_name craftsman_name topic appointment custom_appointment calendar_duration_minutes",
        "subject": "Handwerkertermin – Auftrag {{ commission_number }}",
        "body": "Auftrag: {{ commission_number }}\n" + APPOINTMENT_DETAILS + "{% if address %}\nAdresse: {{ address }}{% endif %}",
    },
}

FIELD_LABELS = {
    "commission_number": "Auftragsnummer",
    "commission_title": "Kurztitel",
    "description": "Auftragstext",
    "damage_location": "Schadensort",
    "address": "Adresse",
    "location": "Lage",
    "tenant_name": "Mietername",
    "positions": "Leistungspositionen: Liste mit text und units",
    "commission_url": "Link zum Auftrag",
    "craft_names": "Gewerke: Liste von Namen",
    "craftsman_name": "Handwerkername",
    "review_pending": "Mieterbelastung noch in Prüfung: wahr oder falsch",
    "portal_name": "Name des Handwerkerportals",
    "recipient_name": "Empfängername, gegebenenfalls leer",
    "username": "Benutzername",
    "invitation_url": "Einladungslink, muss im Text erhalten bleiben",
    "valid_hours": "Gültigkeit der Einladung in Stunden",
    "topic": "Auftragsthema, sofern für Terminmails aktiviert",
    "message": "Nachricht des Absenders",
    "proposals": "Terminvorschläge: Liste mit date, start, end, window_name und custom",
    "public_url": "Antwortlink für Terminvorschläge, muss im Text erhalten bleiben",
    "appointment": "Bestätigter Termin mit date, start, end, window_name und custom",
    "custom_appointment": "Individueller Termin: wahr oder falsch",
    "calendar_duration_minutes": "Reservierte Kalenderdauer in Minuten",
    "tenant_comment": "Kommentar des Bewohners",
    "reason": "Begründung oder Kommentar zum Auftragsstatus",
    "process_id": "Vorgangsnummer",
    "author_name": "Name des Absenders",
    "commission_numbers": "Auftragsnummern: Liste",
}

EXAMPLE_CONTEXT = {
    "commission_number": "2026-0042",
    "commission_title": "Wasserhahn undicht",
    "description": "Bitte den Wasserhahn in der Küche prüfen.",
    "damage_location": "Küche",
    "address": "Musterstraße 12, 12345 Musterstadt",
    "location": "2. Obergeschoss links",
    "tenant_name": "Max Mustermann",
    "positions": [{"text": "Reparatur Wasserhahn", "units": "1"}],
    "commission_url": "https://beispiel.invalid/auftraege/42",
    "craft_names": ["Sanitär"],
    "craftsman_name": "Petra Beispiel",
    "review_pending": True,
    "portal_name": "Handwerkerportal",
    "recipient_name": "Max Mustermann",
    "username": "max.mustermann",
    "invitation_url": "https://beispiel.invalid/einladung/beispiel",
    "valid_hours": 48,
    "topic": "Wasserhahn undicht",
    "message": "Bitte halten Sie den Zugang frei.",
    "proposals": [{
        "date": "Montag, 09.11.2026", "start": "08:00", "end": "10:00",
        "window_name": "Vormittag", "custom": False,
    }],
    "public_url": "https://beispiel.invalid/termine/beispiel",
    "appointment": {
        "date": "Montag, 09.11.2026", "start": "08:00", "end": "10:00",
        "window_name": "Vormittag", "custom": False,
    },
    "custom_appointment": False,
    "calendar_duration_minutes": 120,
    "tenant_comment": "Bitte am Hauseingang klingeln.",
    "reason": "Ersatzteil fehlt.",
    "process_id": 1234,
    "author_name": "Petra Beispiel",
    "commission_numbers": ["2026-0042"],
}

TEMPLATE_ENV = ImmutableSandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
TEMPLATE_ENV.globals.clear()
TEMPLATE_ENV.filters = {
    name: TEMPLATE_ENV.filters[name]
    for name in ("default", "join", "trim", "upper", "lower", "replace", "length")
}


def email_template_definition(key):
    definition = EMAIL_TEMPLATES[key]
    return SettingDefinition(
        f"email.templates.{key}", SettingValueType.JSON,
        {"subject": definition["subject"], "body": definition["body"]},
        definition["label"], "E-Mail-Vorlagen",
    )


def get_email_template(key):
    definition = email_template_definition(key)
    row = AppSetting.query.filter_by(key=definition.key).one_or_none()
    return json.loads(row.value_text) if row is not None else dict(definition.default)


def compile_email_template(key, text):
    tree = TEMPLATE_ENV.parse(text)
    allowed = (
        nodes.Template, nodes.Output, nodes.TemplateData, nodes.Name, nodes.Const,
        nodes.Getattr, nodes.If, nodes.For, nodes.And, nodes.Or, nodes.Not,
        nodes.Compare, nodes.Operand, nodes.Filter,
    )
    for node in (tree, *tree.find_all(nodes.Node)):
        if not isinstance(node, allowed):
            raise ValueError("Bitte nur Platzhalter, Bedingungen und einfache Schleifen verwenden.")
        if isinstance(node, nodes.Getattr) and node.attr.startswith("_"):
            raise ValueError("Dieser Platzhalterzugriff ist nicht erlaubt.")
        if isinstance(node, nodes.For) and any(node.find_all(nodes.For)):
            raise ValueError("Verschachtelte Schleifen sind nicht erlaubt.")
    unknown = meta.find_undeclared_variables(tree) - set(EMAIL_TEMPLATES[key]["fields"].split())
    if unknown:
        raise ValueError("Unbekannte Platzhalter: " + ", ".join(sorted(unknown)))
    return TEMPLATE_ENV.from_string(text), meta.find_undeclared_variables(tree)


def render_email_value(key, template, context):
    try:
        subject_template, subject_fields = compile_email_template(key, template["subject"])
        body_template, body_fields = compile_email_template(key, template["body"])
        clean_context = json.loads(json.dumps(context))
        subject_context = json.loads(json.dumps(clean_context))

        def clean_header(value):
            if isinstance(value, str):
                return " ".join(value.split())
            if isinstance(value, list):
                return [clean_header(entry) for entry in value]
            if isinstance(value, dict):
                return {name: clean_header(entry) for name, entry in value.items()}
            return value

        subject = subject_template.render(clean_header(subject_context)).strip()
        body = body_template.render(clean_context).replace("\r\n", "\n").replace("\r", "\n").strip()
        if not subject or len(subject) > 998 or any(ord(character) < 32 or ord(character) == 127 for character in subject):
            raise ValueError("Der erzeugte Betreff ist leer, zu lang oder enthält Steuerzeichen.")
        if not body or len(body) > 250000 or "\x00" in body:
            raise ValueError("Der erzeugte Mailtext ist leer, zu lang oder enthält ungültige Zeichen.")
        for field in EMAIL_TEMPLATES[key].get("required", ()):
            if field not in body_fields or not context.get(field) or str(context[field]) not in body:
                raise ValueError(f"Der Platzhalter {field} muss als vollständiger Link im Mailtext erhalten bleiben.")
        return {"subject": subject, "body": body + "\n"}
    except TemplateError as exc:
        raise ValueError(f"Die E-Mail-Vorlage ist ungültig: {exc}") from exc


def validate_email_template(key, value):
    if not isinstance(value, dict) or set(value) != {"subject", "body"}:
        raise ValueError("Bitte Betreff und Mailtext angeben.")
    if any(not isinstance(value[name], str) for name in ("subject", "body")):
        raise ValueError("Betreff und Mailtext müssen Texte sein.")
    if len(value["subject"]) > 500 or len(value["body"]) > 50000:
        raise ValueError("Die E-Mail-Vorlage ist zu lang.")
    if any(ord(character) < 32 or ord(character) == 127 for character in value["subject"]):
        raise ValueError("Der Betreff darf keine Zeilenumbrüche oder Steuerzeichen enthalten.")
    render_email_value(key, value, EXAMPLE_CONTEXT)
    return dict(value)


def render_email_template(key, context):
    template = validate_email_template(key, get_email_template(key))
    return render_email_value(key, template, context)


def email_body_html(body):
    parts = []
    offset = 0
    for match in re.finditer(r"https?://[^\s<>]+", body):
        parts.append(escape(body[offset:match.start()]))
        url = escape(match.group(), quote=True)
        parts.append(f'<a href="{url}">{url}</a>')
        offset = match.end()
    parts.append(escape(body[offset:]))
    content = "".join(parts).replace("\n", "<br>\n")
    return '<!doctype html><html lang="de"><body style="font-family:Arial,sans-serif;line-height:1.5">' + content + "</body></html>"


def set_email_content(message, subject, body):
    if "Subject" in message:
        message.replace_header("Subject", subject)
    else:
        message["Subject"] = subject
    message.set_content(body)
    message.add_alternative(email_body_html(body), subtype="html")


def apply_email_template(message, key, context):
    content = render_email_template(key, context)
    set_email_content(message, content["subject"], content["body"])
    return content


def commission_email_context(commission):
    base_url = settings.get(SettingKeys.PUBLIC_BASE_URL).strip().rstrip("/")
    return {
        "commission_number": str(commission.erp_idnum or commission.code or commission.id),
        "commission_title": str(commission.display_title or ""),
        "description": str(commission.description or "-"),
        "damage_location": str(commission.damage_location or "-"),
        "address": str(commission.apartment_address or ""),
        "location": str(commission.apartment_location or "-"),
        "tenant_name": str(commission.tenant_name or "-"),
        "positions": [
            {"text": str(position.display_text), "units": f"{position.units:g}"}
            for position in commission.positions
        ],
        "commission_url": f"{base_url}/admin/commissions/{commission.id}" if base_url else "",
        "portal_name": normalize_external_portal_name(settings.get(SettingKeys.EXTERNAL_PORTAL_NAME)),
    }