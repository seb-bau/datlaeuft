from collections import Counter
from io import BytesIO
from pathlib import Path
import re
from uuid import UUID

from pypdf import PdfReader

from .audit import audit
from .dms_client import DmsError
from .extensions import db
from .models import DmsDocument, User
from .settings import SettingKeys, settings


PAPERWORK_ID = re.compile(r"[A-Za-z0-9-]{1,40}")


def entra_object_id(value):
    if not isinstance(value, str):
        return None
    try:
        return str(UUID(value))
    except ValueError:
        return None


def paperwork_signer_marker(develop_id, d3id):
    if not isinstance(develop_id, str) or not PAPERWORK_ID.fullmatch(develop_id):
        raise ValueError("Bitte die d.velop-Benutzer-ID aus Paperwork verwenden.")
    if (
        not isinstance(d3id, str) or not 1 <= len(d3id) <= 10 or "_" in d3id
        or any(not character.isprintable() or character.isspace() for character in d3id)
    ):
        raise ValueError("Für die Poststelle muss die d3Id aus Paperwork höchstens zehn Zeichen lang sein und darf keine Unterstriche oder Leerzeichen enthalten.")
    return f"manual_{d3id}_{develop_id}_"


def resolve_paperwork_signer(user):
    if not user.paperwork_user_id or not user.paperwork_d3id:
        raise ValueError("Für das angemeldete Konto fehlt die Paperwork-Zuordnung.")
    if user.paperwork_entra_oid != user.entra_oid:
        raise ValueError("Das Entra-Konto hat sich geändert. Bitte die Paperwork-Zuordnung prüfen.")
    return paperwork_signer_marker(user.paperwork_user_id, user.paperwork_d3id)


def save_paperwork_signer(user, develop_id, d3id):
    if develop_id or d3id:
        paperwork_signer_marker(develop_id, d3id)
        if (
            (develop_id, d3id) == (user.paperwork_user_id, user.paperwork_d3id)
            and user.paperwork_entra_oid != user.entra_oid
        ):
            raise ValueError("Das Entra-Konto hat sich geändert. Bitte die Paperwork-Zuordnung neu importieren oder erneut eintragen.")
        other = User.query.filter_by(paperwork_user_id=develop_id).first()
        if other is not None and other.id != user.id:
            raise ValueError("Das Paperwork-Konto ist bereits einem anderen Benutzer zugeordnet.")
    user.paperwork_user_id = develop_id
    user.paperwork_d3id = d3id
    user.paperwork_entra_oid = user.entra_oid if develop_id else None


def import_paperwork_signers(payload):
    if (
        not isinstance(payload, dict) or not isinstance(payload.get("data"), list)
        or payload.get("total") != len(payload["data"])
        or any(not isinstance(row, dict) for row in payload["data"])
    ):
        raise ValueError("Bitte die vollständige JSON-Benutzerliste aus Paperwork auswählen.")
    accounts = payload["data"]
    object_counts = Counter(entra_object_id(row.get("oid")) for row in accounts)
    id_counts = Counter(row["internal_id"] for row in accounts if isinstance(row.get("internal_id"), str))
    valid_accounts = {}
    for account in accounts:
        oid = entra_object_id(account.get("oid"))
        if oid is None or object_counts[oid] != 1 or account.get("is_enabled") is not True:
            continue
        develop_id, d3id = account.get("internal_id"), account.get("d3id")
        try:
            paperwork_signer_marker(develop_id, d3id)
        except ValueError:
            continue
        if id_counts[develop_id] == 1:
            valid_accounts[oid] = (develop_id, d3id)

    users = User.query.order_by(User.id).with_for_update().all()
    local_object_counts = Counter(entra_object_id(user.entra_oid) for user in users)
    owners = {user.paperwork_user_id: user.id for user in users if user.paperwork_user_id}
    matched = skipped = 0
    for user in users:
        if not user.is_active_flag or not user.entra_oid:
            continue
        oid = entra_object_id(user.entra_oid)
        account = valid_accounts.get(oid)
        if not account or local_object_counts[oid] != 1:
            skipped += 1
            continue
        develop_id, d3id = account
        if (
            user.paperwork_user_id not in (None, develop_id)
            or owners.get(develop_id, user.id) != user.id
        ):
            skipped += 1
            continue
        user.paperwork_user_id = develop_id
        user.paperwork_d3id = d3id
        user.paperwork_entra_oid = user.entra_oid
        owners[develop_id] = user.id
        matched += 1
    return matched, skipped


def dispatch_settings():
    return {
        "postal_property": settings.get(SettingKeys.DMS_POSTAL_PROPERTY),
        "portal_property": settings.get(SettingKeys.DMS_PORTAL_PROPERTY),
        "postal_default": settings.get(SettingKeys.DMS_POSTAL_DEFAULT),
        "portal_default": settings.get(SettingKeys.DMS_PORTAL_DEFAULT),
        "signer_property": settings.get(SettingKeys.DMS_POSTAL_SIGNER_PROPERTY),
        "system_user_id": settings.get(SettingKeys.DMS_POSTAL_SYSTEM_USER_ID),
        "alternate_signer_property": settings.get(SettingKeys.DMS_POSTAL_ALTERNATE_SIGNER_PROPERTY),
    }


def dispatch_options(user):
    config = dispatch_settings()
    enabled = settings.get(SettingKeys.DMS_ENABLED)
    try:
        resolve_paperwork_signer(user)
        signer_linked = user.paperwork_user_id != config["system_user_id"]
    except ValueError:
        signer_linked = False
    return {
        "postal_property": config["postal_property"],
        "portal_property": config["portal_property"],
        "postal_default": config["postal_default"],
        "portal_default": config["portal_default"],
        "postal_signer_linked": signer_linked,
        "postal_available": bool(
            enabled and config["postal_property"] and config["signer_property"]
            and config["system_user_id"]
        ),
        "portal_available": bool(enabled and config["portal_property"]),
    }


def dispatch_channel(mode, postal, portal):
    if type(postal) is not bool or type(portal) is not bool:
        raise ValueError("Die Auswahl für Poststelle und Mieter-App ist ungültig.")
    if postal and mode == "print":
        raise ValueError("Für „Kein Versand (nur Druck)“ ist nur die Mieter-App als Übertragung möglich.")
    return "postal" if postal else "portal" if portal else None


def existing_dispatch(invoice_id, *, lock=False):
    query = DmsDocument.query.filter(
        DmsDocument.entity_type == "invoice", DmsDocument.entity_id == invoice_id,
        DmsDocument.invoice_dispatch.is_not(None), DmsDocument.status != "cancelled",
    ).order_by(DmsDocument.invoice_version.desc())
    if lock:
        query = query.populate_existing().with_for_update()
    return query.first()


def queue_invoice_dispatch(invoice, content, mode, channel, user):
    from .dms_context import invoice_context
    from .dms_service import connection, profile_snapshot, register_generated_pdf

    existing = existing_dispatch(invoice.id, lock=True)
    if existing:
        if existing.invoice_dispatch["channel"] == channel:
            return existing
        raise ValueError("Für diese Rechnung wurde bereits eine DMS-Übergabe ausgewählt.")

    if not settings.get(SettingKeys.DMS_ENABLED):
        raise ValueError("Die DMS-Übertragung ist deaktiviert.")
    try:
        connection()
        document_type = "cancellation_invoice" if invoice.invoice_kind == "cancellation" else "invoice"
        snapshot = profile_snapshot(document_type)
    except DmsError as exc:
        raise ValueError(str(exc)) from exc
    config = dispatch_settings()
    property_id = config[f"{channel}_property"]
    field_ids = {
        field["id"] for field in snapshot["schema"]["fields"]
        if field["dataType"] == "STRING" and not field.get("isMultiValue")
    }
    reserved_ids = {snapshot["key_property_id"], *snapshot["mappings"]}
    if not property_id or property_id not in field_ids or property_id in reserved_ids:
        raise ValueError("Die Versand-Eigenschaft ist für die Dokumentkategorie nicht verfügbar. Bitte die DMS-Zuordnung prüfen.")

    properties = {property_id: "Ja"}
    identity = {}
    if channel == "postal":
        signer_property = config["signer_property"]
        if not signer_property or signer_property in reserved_ids or signer_property in {property_id, config["portal_property"]}:
            raise ValueError("Bitte die Feld-ID für den Poststellenbenutzer unter Archiv/DMS prüfen.")
        system_user_id = config["system_user_id"]
        if not PAPERWORK_ID.fullmatch(system_user_id):
            raise ValueError("Bitte die d.velop-ID des Paperwork-Systembenutzers unter Archiv/DMS eintragen.")
        marker = resolve_paperwork_signer(user)
        if user.paperwork_user_id == system_user_id:
            raise ValueError("Für den Versand über die Poststelle ist ein persönliches Paperwork-Konto erforderlich.")
        alternate_signer = config["alternate_signer_property"]
        if alternate_signer and alternate_signer in reserved_ids | {property_id, signer_property, config["portal_property"]}:
            raise ValueError("Bitte die Feld-ID für die abweichende Unterschrift unter Archiv/DMS prüfen.")
        if not any("{sig}" in (page.extract_text() or "") for page in PdfReader(BytesIO(content)).pages):
            raise ValueError("Für die Poststelle fehlt der Signaturanker im Rechnungsanschreiben.")
        properties[signer_property] = marker
        identity = {"system_user_id": system_user_id, "alternate_signer_property": alternate_signer}

    try:
        document = register_generated_pdf(
            document_type, "invoice", invoice.id, Path(invoice.document_filename).name, content,
            invoice_context(invoice), invoice_dispatch={
                "actor_user_id": user.id, "mode": mode, "channel": channel,
                "flag_property": property_id, "properties": properties, **identity,
            },
        )
        if document.status in {"disabled", "blocked"}:
            raise ValueError(document.error or "Die DMS-Übergabe ist derzeit nicht verfügbar.")
        audit("invoice.dms_dispatch.queued", "invoice", invoice.id, {"channel": channel, "mode": mode})
        return document
    except DmsError as exc:
        raise ValueError(str(exc)) from exc
