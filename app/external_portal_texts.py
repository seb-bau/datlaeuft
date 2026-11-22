from __future__ import annotations

from email.message import EmailMessage
from functools import lru_cache
from html import escape
from types import SimpleNamespace

from flask import abort, flash, redirect, render_template, request, url_for
from jinja2 import StrictUndefined, TemplateError, meta, nodes
from jinja2.sandbox import ImmutableSandboxedEnvironment

from .extensions import db
from .external_branding import get_external_portal_name
from .models import SettingValueType
from .settings import SettingDefinition, settings


PORTAL_TEXTS_TEMPLATE_KEY = "external_portal_texts"
PORTAL_TEXT_FIELDS = {
    "mail_actions_intro": ("E-Mail-Schaltflächen", "Einleitung vor den Schaltflächen", "Ohne Anmeldung antworten:", True),
    "accept_label": ("E-Mail-Schaltflächen", "Beschriftung für Annahme (Mail und Antwortseite)", "Auftrag annehmen", False),
    "reject_label": ("E-Mail-Schaltflächen", "Beschriftung für Ablehnung (Mail und Antwortseite)", "Auftrag ablehnen", False),
    "page_title": ("Auswahlseite", "Seitentitel im Browser", "Antwort zu Auftrag {{ commission_number }}", False),
    "order_heading": ("Auswahlseite", "Überschrift zum Auftrag", "Auftrag {{ commission_number }}", False),
    "intro_accept": ("Auswahlseite", "Einleitung bei Annahme", "Vielen Dank, dass Sie den Auftrag übernehmen möchten. Bitte bestätigen Sie Ihre Entscheidung.", True),
    "intro_reject": ("Auswahlseite", "Einleitung bei Ablehnung", "Bitte bestätigen Sie Ihre Entscheidung zum Auftrag.", True),
    "decision_legend": ("Auswahlseite", "Überschrift der Auswahl", "Ihre Entscheidung", False),
    "additional_label": ("Auswahlseite", "Beschriftung für weitere Mitteilungen", "Weitere Mitteilungen", False),
    "rejection_label": ("Auswahlseite", "Beschriftung für den Ablehnungsgrund", "Ablehnungsgrund", False),
    "optional_label": ("Auswahlseite", "Zusatz für freiwillige Angaben", "optional", False),
    "input_hint": ("Auswahlseite", "Hinweis zum Eingabefeld", "Ihre Eingabe ist freiwillig. Sie können Ihre Entscheidung bis zum Absenden ändern.", True),
    "submit_label": ("Auswahlseite", "Beschriftung zum Absenden", "Entscheidung absenden", False),
    "accepted_message": ("Bestätigung und Portalzugang", "Bestätigung einer Annahme", "Vielen Dank! Sie haben den Auftrag angenommen.", True),
    "rejected_message": ("Bestätigung und Portalzugang", "Bestätigung einer Ablehnung", "Vielen Dank für Ihre Rückmeldung. Ihre Ablehnung wurde übermittelt.", True),
    "portal_heading": ("Bestätigung und Portalzugang", "Überschrift des Portalhinweises", "Nutzen Sie unser Auftragnehmerportal", False),
    "portal_description": ("Bestätigung und Portalzugang", "Beschreibung des Portals", "Im Portal können Sie Ihre Aufträge einsehen, Zusatzinformationen austauschen und den Bearbeitungsstand melden.", True),
    "upgrade_intro": ("Bestätigung und Portalzugang", "Einleitung zur Kennwortvergabe", "Bei Bedarf können Sie jetzt ein Kennwort für Ihren persönlichen Zugang vergeben. Ihre Entscheidung zum Auftrag ist bereits gespeichert.", True),
    "username_label": ("Bestätigung und Portalzugang", "Beschriftung des Benutzernamens", "Ihr Benutzername:", False),
    "password_label": ("Bestätigung und Portalzugang", "Beschriftung des Kennwortfelds", "Kennwort", False),
    "password_hint": ("Bestätigung und Portalzugang", "Hinweis zur Kennwortvergabe", "Mindestens {{ min_password_length }}, höchstens {{ max_password_length }} Zeichen. Die Einrichtung über diesen Link ist {{ upgrade_hours }} Stunden möglich.", True),
    "password_confirmation_label": ("Bestätigung und Portalzugang", "Beschriftung der Kennwortwiederholung", "Kennwort wiederholen", False),
    "upgrade_label": ("Bestätigung und Portalzugang", "Beschriftung zum Einrichten des Zugangs", "Portalzugang einrichten", False),
    "existing_account_message": ("Bestätigung und Portalzugang", "Hinweis bei bestehendem Zugang", "Ihr Zugang besteht bereits.", True),
    "login_label": ("Bestätigung und Portalzugang", "Beschriftung zur Portalanmeldung", "Im Portal anmelden", False),
    "contact_office_message": ("Bestätigung und Portalzugang", "Hinweis bei gesperrter oder abgelaufener Zugangseinrichtung", "Für die Einrichtung oder Freischaltung Ihres Zugangs wenden Sie sich bitte an die Sachbearbeitung.", True),
    "closed_message": ("Nicht verfügbare Antwort", "Hinweis bei bereits bearbeitetem Auftrag", "Für diesen Auftrag kann keine Entscheidung mehr abgegeben werden. Der Bearbeitungsstand wurde bereits geändert. Bei Rückfragen wenden Sie sich bitte an die Sachbearbeitung.", True),
    "unavailable_heading": ("Nicht verfügbare Antwort", "Überschrift bei ungültigem Antwort-Link", "Antwort-Link nicht verfügbar", False),
    "unavailable_message": ("Nicht verfügbare Antwort", "Hinweis bei ungültigem Antwort-Link", "Dieser Antwort-Link ist ungültig, abgelaufen oder nicht mehr verfügbar. Bitte wenden Sie sich an die Sachbearbeitung.", True),
}
PORTAL_TEXT_PLACEHOLDERS = {
    "commission_number": "Auftragsnummer", "commission_title": "Auftragstitel",
    "craftsman_name": "Name des externen Handwerkers", "portal_name": "Name des Portals",
    "username": "Benutzername", "min_password_length": "Mindestlänge des Kennworts",
    "max_password_length": "Maximallänge des Kennworts", "upgrade_hours": "Frist zur Kennwortvergabe in Stunden",
}
PORTAL_PREVIEW_STATES = {
    "accept": "Auswahl: Annahme", "reject": "Auswahl: Ablehnung",
    "accepted": "Bestätigung: Annahme", "rejected": "Bestätigung: Ablehnung",
    "existing": "Bestätigung mit bestehendem Portalzugang", "closed": "Auftrag bereits bearbeitet",
    "unavailable": "Antwort-Link nicht verfügbar",
}
TEXT_ENV = ImmutableSandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
TEXT_ENV.globals.clear()
TEXT_ENV.filters.clear()


def portal_text_definition():
    return SettingDefinition("external_portal.response_texts", SettingValueType.JSON,
                             {key: field[2] for key, field in PORTAL_TEXT_FIELDS.items()},
                             "Portal light: Antwortseite und Mail-Schaltflächen", "E-Mail-Vorlagen")


def get_portal_text_templates():
    values = settings.get(portal_text_definition())
    return {key: values.get(key, default) for key, default in portal_text_definition().default.items()}


def portal_text_context(commission=None, craftsman=None, *, username=""):
    if not username and craftsman is not None:
        username = craftsman.username or f"handwerker-{craftsman.craftsman_erp_id}"
    return {
        "commission_number": str(commission.erp_idnum or commission.code or commission.id) if commission is not None else "",
        "commission_title": str(commission.display_title or "") if commission is not None else "",
        "craftsman_name": str(craftsman.display_name or "") if craftsman is not None else "",
        "portal_name": get_external_portal_name(), "username": username,
        "min_password_length": "12", "max_password_length": "128", "upgrade_hours": "48",
    }


@lru_cache(maxsize=256)
def compile_portal_text(text):
    tree = TEXT_ENV.parse(text)
    if any(not isinstance(node, (nodes.Template, nodes.Output, nodes.TemplateData, nodes.Name))
           for node in (tree, *tree.find_all(nodes.Node))):
        raise ValueError("In Portaltexten sind Text und die angegebenen Platzhalter erlaubt.")
    unknown = meta.find_undeclared_variables(tree) - set(PORTAL_TEXT_PLACEHOLDERS)
    if unknown:
        raise ValueError("Unbekannte Platzhalter: " + ", ".join(sorted(unknown)))
    return TEXT_ENV.from_string(text)


def validate_portal_texts(values):
    if not isinstance(values, dict) or set(values) != set(PORTAL_TEXT_FIELDS):
        raise ValueError("Bitte alle Portaltexte angeben.")
    result = {}
    context = {name: "Beispiel" for name in PORTAL_TEXT_PLACEHOLDERS}
    for key, (_, label, _, multiline) in PORTAL_TEXT_FIELDS.items():
        text = values[key]
        maximum = 4000 if multiline else 200
        if not isinstance(text, str) or not text.strip() or len(text) > maximum:
            raise ValueError(f"{label}: Bitte einen Text mit höchstens {maximum} Zeichen eingeben.")
        text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if any((ord(character) < 32 and (not multiline or character not in "\n\t")) or ord(character) == 127 for character in text):
            raise ValueError(f"{label}: Der Text enthält ungültige Steuerzeichen.")
        try:
            compile_portal_text(text).render(context)
        except TemplateError as exc:
            raise ValueError(f"{label}: Die Textvorlage ist ungültig.") from exc
        result[key] = text
    return result


def render_portal_texts(context=None, *, values=None):
    values = validate_portal_texts(values if values is not None else get_portal_text_templates())
    context = {name: str((context or {}).get(name, "")) for name in PORTAL_TEXT_PLACEHOLDERS}
    result = {}
    for key, text in values.items():
        rendered = compile_portal_text(text).render(context)
        result[key] = rendered if PORTAL_TEXT_FIELDS[key][3] else " ".join(rendered.split())
    return result


def set_response_email_content(message, subject, body, *, accept_url=None, reject_url=None, texts=None, layout=None):
    from .email_templates import set_email_content

    actions = []
    if accept_url and reject_url:
        actions = [(texts["accept_label"], accept_url, "#198754"), (texts["reject_label"], reject_url, "#b02a37")]
        body = body.rstrip() + "\n\n" + texts["mail_actions_intro"] + "\n" + "\n".join(f"{label}: {url}" for label, url, _ in actions) + "\n"
    message.clear_content()
    set_email_content(message, subject, body, layout=layout)
    for part in message.walk():
        if part.get_content_type() != "text/html":
            continue
        html = part.get_content()
        for label, url, color in actions:
            escaped_url = escape(url, quote=True)
            html = html.replace(f'<a href="{escaped_url}">{escaped_url}</a>',
                                f'<a href="{escaped_url}" style="display:inline-block;padding:12px 18px;margin:6px 0;'
                                f'background:{color};color:#fff;text-decoration:none;border-radius:4px">{escape(label)}</a>')
        part.set_content(html, subtype="html")


def external_mail_preview(key, preview, *, layout=None):
    from .email_templates import EXAMPLE_CONTEXT, email_body_html

    if key not in {"external_assignment", "external_reminder", "external_reminder_second"}:
        return preview, email_body_html(preview["body"], preview["subject"], layout=layout)
    message = EmailMessage()
    texts = render_portal_texts({**portal_text_context(), **EXAMPLE_CONTEXT})
    set_response_email_content(message, preview["subject"], preview["body"],
                               accept_url="https://beispiel.invalid/auftragnehmer/antwort/beispiel?decision=accept",
                               reject_url="https://beispiel.invalid/auftragnehmer/antwort/beispiel?decision=reject", texts=texts, layout=layout)
    return {**preview, "body": message.get_body(preferencelist=("plain",)).get_content()}, message.get_body(preferencelist=("html",)).get_content()


def portal_text_preview(values, state):
    commission = SimpleNamespace(erp_idnum="2026-0042", code="2026-0042", id=42, display_title="Wasserhahn undicht")
    craftsman = SimpleNamespace(display_name="Sanitär Beispiel", portal_enabled=state == "existing", password_hash="preview" if state == "existing" else None)
    context = portal_text_context(commission, craftsman, username="handwerker-123")
    texts = render_portal_texts(context, values=values)
    item = SimpleNamespace(commission=commission, external_craftsman=craftsman,
                           response_at=state in {"accepted", "rejected", "existing"},
                           response_decision="reject" if state == "rejected" else "accept", response_text=None)
    return render_template("external_portal/order_response_unavailable.html" if state == "unavailable" else "external_portal/order_response.html",
                           portal_texts=texts, item=item, raw_token="vorschau", username=context["username"], text="",
                           decision="reject" if state == "reject" else "accept", can_respond=state in {"accept", "reject"},
                           can_create_password=state in {"accepted", "rejected"}, external_craftsman=None,
                           external_portal_name=context["portal_name"], csrf_token=lambda: "")


def edit_external_portal_texts():
    from .audit import audit

    value = get_portal_text_templates()
    state = request.values.get("preview_state", "accept")
    if state not in PORTAL_PREVIEW_STATES:
        abort(400)
    preview_html = None
    if request.method == "POST":
        action = request.form.get("action", "save")
        if action not in {"save", "reset", "preview"}:
            abort(400)
        value = (dict(portal_text_definition().default) if action == "reset" else
                 {key: request.form.get("text_" + key, value[key]) for key in PORTAL_TEXT_FIELDS})
        try:
            value = validate_portal_texts(value)
            if action == "preview":
                preview_html = portal_text_preview(value, state)
            else:
                settings.set(portal_text_definition(), value)
                audit("external_portal_texts." + action, "app_setting", portal_text_definition().key)
                db.session.commit()
                flash("Portaltexte gespeichert.", "success")
                return redirect(url_for("admin.email_templates_page", template=PORTAL_TEXTS_TEMPLATE_KEY))
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
    groups = {}
    for key, (group, label, _, multiline) in PORTAL_TEXT_FIELDS.items():
        groups.setdefault(group, []).append({"key": key, "label": label, "multiline": multiline, "maximum": 4000 if multiline else 200})
    return render_template("admin/external_portal_texts.html", text_groups=groups, template_value=value,
                           preview_states=PORTAL_PREVIEW_STATES, preview_state=state, preview_html=preview_html,
                           fields=PORTAL_TEXT_PLACEHOLDERS)
