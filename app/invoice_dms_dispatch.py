from io import BytesIO
from pathlib import Path
import re
from urllib.parse import urlsplit
from uuid import UUID

from pypdf import PdfReader
from requests import RequestException, post

from .audit import audit
from .dms_client import DmsError
from .extensions import db
from .models import DmsDocument, User
from .settings import SettingKeys, settings


PAPERWORK_ID = re.compile(r"[A-Za-z0-9-]{1,40}")


def paperwork_base_url(value):
    value = value.strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError as exc:
        raise ValueError("Bitte eine gültige HTTPS-Basis-URL für Paperwork eingeben.") from exc
    if value and (
        parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
        or parsed.query or parsed.fragment or len(value) > 255 or "\\" in value
        or any(not character.isprintable() or character.isspace() for character in value)
    ):
        raise ValueError("Bitte eine gültige HTTPS-Basis-URL für Paperwork eingeben.")
    return value


def paperwork_lookup(payload):
    url = paperwork_base_url(settings.get(SettingKeys.DMS_PAPERWORK_URL))
    api_key = settings.get(SettingKeys.DMS_PAPERWORK_API_KEY).strip()
    if not url or not api_key:
        raise ValueError("Bitte die Paperwork-Anbindung unter Archiv/DMS einrichten.")
    try:
        response = post(
            url + "/integration/datlaeuft/signer",
            json=payload, headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=(3, 8), allow_redirects=False,
        )
        data = response.json()
        if isinstance(data, dict) and response.status_code == 404 and data.get("code") == "no_match":
            return None
        if isinstance(data, dict) and response.status_code == 409:
            if data.get("code") == "no_signature":
                raise ValueError("Für das angemeldete Konto fehlt in Paperwork eine Unterschrift.")
            raise ValueError("Das Konto ist in Paperwork nicht eindeutig zugeordnet oder für die Poststelle gesperrt.")
        if response.status_code != 200:
            raise DmsError("Die Paperwork-Benutzerabfrage wurde abgelehnt.")
    except (DmsError, RequestException) as exc:
        raise ValueError("Die Paperwork-Benutzerabfrage ist fehlgeschlagen. Bitte die Anbindung unter Archiv/DMS prüfen.") from exc
    if not isinstance(data, dict):
        raise ValueError("Paperwork hat ungültige Benutzerdaten geliefert.")
    return data


def resolve_paperwork_signer(user):
    oid = user.entra_oid
    tenant = settings.get(SettingKeys.ENTRA_TENANT_ID)
    signer = None
    if oid and tenant:
        try:
            oid, tenant = str(UUID(oid)), str(UUID(tenant))
        except ValueError as exc:
            raise ValueError("Die Entra-Zuordnung des angemeldeten Kontos ist ungültig.") from exc
        signer = paperwork_lookup({"entra_oid": oid, "entra_tenant": tenant})
        if signer and (signer.get("entra_oid") != oid or signer.get("entra_tenant") != tenant):
            raise ValueError("Paperwork hat ein anderes Entra-Konto zurückgegeben.")
    if signer is None and user.paperwork_user_id:
        signer = paperwork_lookup({"develop_id": user.paperwork_user_id})
        if signer and (
            signer.get("develop_id") != user.paperwork_user_id
            or (oid and signer.get("entra_tenant") == tenant and signer.get("entra_oid") not in (None, oid))
        ):
            raise ValueError("Die manuelle Paperwork-Zuordnung passt nicht zum angemeldeten Konto.")
    if signer is None:
        raise ValueError("Für das angemeldete Konto fehlt eine Zuordnung in Paperwork.")
    develop_id, d3id = signer.get("develop_id"), signer.get("d3id")
    if (
        not isinstance(develop_id, str) or not PAPERWORK_ID.fullmatch(develop_id)
        or not isinstance(d3id, str) or not 1 <= len(d3id) <= 10
        or any(not character.isprintable() or character.isspace() for character in d3id)
    ):
        raise ValueError("Paperwork hat ungültige Benutzerkennungen geliefert.")
    other = User.query.filter(User.paperwork_user_id == develop_id, User.id != user.id).first()
    if other is not None:
        raise ValueError("Das Paperwork-Konto ist bereits einem anderen datlaeuft-Benutzer zugeordnet.")
    return signer


def dispatch_settings():
    return {
        "postal_property": settings.get(SettingKeys.DMS_POSTAL_PROPERTY),
        "portal_property": settings.get(SettingKeys.DMS_PORTAL_PROPERTY),
        "postal_default": settings.get(SettingKeys.DMS_POSTAL_DEFAULT),
        "portal_default": settings.get(SettingKeys.DMS_PORTAL_DEFAULT),
        "signer_property": settings.get(SettingKeys.DMS_POSTAL_SIGNER_PROPERTY),
        "paperwork_url": settings.get(SettingKeys.DMS_PAPERWORK_URL),
        "has_paperwork_key": bool(settings.get(SettingKeys.DMS_PAPERWORK_API_KEY)),
    }


def dispatch_options(user):
    config = dispatch_settings()
    enabled = settings.get(SettingKeys.DMS_ENABLED)
    return {
        **config,
        "postal_available": bool(
            enabled and config["postal_property"] and config["signer_property"]
            and config["paperwork_url"] and config["has_paperwork_key"]
            and (user.entra_oid or user.paperwork_user_id)
        ),
        "portal_available": bool(enabled and config["portal_property"]),
    }


def dispatch_channel(mode, postal, portal):
    if type(postal) is not bool or type(portal) is not bool:
        raise ValueError("Die Auswahl für Poststelle und Mieter-App ist ungültig.")
    if postal and mode == "print":
        raise ValueError("Für „Kein Versand (nur Druck)“ ist nur die Mieter-App als Übertragung möglich.")
    return "postal" if postal else "portal" if portal else None


def existing_dispatch(invoice_id):
    return DmsDocument.query.filter(
        DmsDocument.entity_type == "invoice", DmsDocument.entity_id == invoice_id,
        DmsDocument.invoice_dispatch.is_not(None), DmsDocument.status != "cancelled",
    ).order_by(DmsDocument.invoice_version.desc()).first()


def queue_invoice_dispatch(invoice, content, mode, channel, user):
    from .dms_context import invoice_context
    from .dms_service import connection, profile_snapshot, register_generated_pdf

    existing = existing_dispatch(invoice.id)
    if existing:
        if existing.invoice_dispatch["channel"] == channel:
            return existing
        raise ValueError("Für diese Rechnung wurde bereits eine DMS-Übergabe ausgewählt.")

    if not settings.get(SettingKeys.DMS_ENABLED):
        raise ValueError("Die DMS-Übertragung ist deaktiviert.")
    try:
        hostname, _, repository = connection()
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
    if channel == "postal":
        signer_property = config["signer_property"]
        if not signer_property or signer_property in reserved_ids or signer_property in {property_id, config["portal_property"]}:
            raise ValueError("Bitte die Feld-ID für den Poststellenbenutzer unter Archiv/DMS prüfen.")
        signer = resolve_paperwork_signer(user)
        if (
            signer.get("hostname") != hostname or signer.get("repository_id") != repository
            or signer.get("postal_property") != property_id or signer.get("signer_property") != signer_property
            or signer.get("postal_value") != properties[property_id]
        ):
            raise ValueError("Anbindung und Eigenschaften für die Poststelle stimmen in datlaeuft und Paperwork nicht überein.")
        anchor = signer.get("signature_anchor")
        if not isinstance(anchor, str) or not anchor or not any(
            anchor in (page.extract_text() or "") for page in PdfReader(BytesIO(content)).pages
        ):
            raise ValueError("Für die Poststelle fehlt der Signaturanker im Rechnungsanschreiben.")
        properties[signer_property] = f"manual_{signer['d3id']}_{signer['develop_id']}_datlaeuft"

    try:
        document = register_generated_pdf(
            document_type, "invoice", invoice.id, Path(invoice.document_filename).name, content,
            invoice_context(invoice), invoice_dispatch={
                "actor_user_id": user.id, "mode": mode, "channel": channel,
                "flag_property": property_id, "properties": properties,
            },
        )
        if document.status in {"disabled", "blocked"}:
            raise ValueError(document.error or "Die DMS-Übergabe ist derzeit nicht verfügbar.")
        audit("invoice.dms_dispatch.queued", "invoice", invoice.id, {"channel": channel, "mode": mode})
        return document
    except DmsError as exc:
        raise ValueError(str(exc)) from exc
