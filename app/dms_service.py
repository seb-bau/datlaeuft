from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
from string import Formatter
from urllib.parse import quote
from uuid import uuid4

from sqlalchemy import case, event, select, update
from sqlalchemy.orm import Session, aliased
from werkzeug.utils import secure_filename

from .audit import audit
from .dms_client import CloudDmsClient, DmsError, normalize_hostname
from .dms_context import DOCUMENT_TYPES, available_fields, template_fields
from .extensions import db
from .models import (
    AppSetting, DmsDocument, DmsProfile, Invoice, Offer,
    SafetyInspectionProtocol, utc_now,
)
from .settings import SettingKeys, settings


STATUS_LABELS = {
    "disabled": "Archivierung war deaktiviert",
    "pending": "Ausstehend",
    "manual_pending": "Einzelübertragung vorgemerkt",
    "processing": "Wird übertragen",
    "archived": "Archiviert",
    "failed": "Wird erneut versucht",
    "blocked": "Prüfung erforderlich",
    "uncertain": "Upload-Bestätigung ausstehend",
    "cancelled": "Aus Warteschlange entfernt",
}

INVOICE_DOCUMENT_TYPES = {"invoice", "cancellation_invoice"}
COMPLETED_INVOICE_JOBS = ("archived", "cancelled", "disabled")


def invoice_archive_documents(invoice_id):
    return DmsDocument.query.filter_by(entity_type="invoice", entity_id=invoice_id).filter(
        DmsDocument.invoice_version.is_not(None),
    )


def invoice_dms_url(invoice):
    if not invoice.dms_document_id:
        return None
    document = DmsDocument.query.filter_by(
        entity_type="invoice", entity_id=invoice.id, dms_document_id=invoice.dms_document_id,
    ).filter(DmsDocument.document_type.in_(INVOICE_DOCUMENT_TYPES)).order_by(
        DmsDocument.archived_at.desc(), DmsDocument.id.desc(),
    ).first()
    hostname = document.hostname if document and document.hostname else settings.get(SettingKeys.DMS_HOSTNAME)
    repository = document.repository_id if document and document.repository_id else settings.get(SettingKeys.DMS_REPOSITORY)
    if not hostname or not repository:
        return None
    try:
        host = normalize_hostname(hostname.strip())
    except DmsError:
        return None
    return f"https://{host}/dms/r/{quote(repository.strip(), safe='')}/o2m/{quote(invoice.dms_document_id, safe='')}"


def lock_invoice_archive(invoice_id):
    return db.session.get(Invoice, invoice_id, with_for_update=True, populate_existing=True)


def connection():
    hostname = normalize_hostname(
        str(settings.get(SettingKeys.DMS_HOSTNAME) or "").strip()
    )
    api_key = str(settings.get(SettingKeys.DMS_API_KEY) or "").strip()
    repository = str(settings.get(SettingKeys.DMS_REPOSITORY) or "").strip()
    if not api_key or not repository or len(repository) > 80:
        raise DmsError("API-Key und Repository-ID müssen eingetragen sein.")
    return hostname, api_key, repository


def refresh_metadata():
    hostname, api_key, repository = connection()
    client = CloudDmsClient(hostname, api_key, repository)
    try:
        metadata = client.repository_metadata()
    finally:
        client.close()
    metadata.update(
        hostname=hostname,
        repository_id=repository,
        updated_at=utc_now().isoformat(),
    )
    settings.set(SettingKeys.DMS_METADATA, metadata)
    for profile in DmsProfile.query.all():
        profile.schema = metadata["categories"].get(profile.category_id)
    audit(
        "dms.metadata.refresh",
        metadata={"category_count": len(metadata["categories"])},
    )
    return metadata


def repository_metadata():
    metadata = settings.get(SettingKeys.DMS_METADATA) or {}
    try:
        hostname, _, repository = connection()
    except DmsError:
        return {}
    if (
        metadata.get("hostname") != hostname
        or metadata.get("repository_id") != repository
    ):
        return {}
    return metadata


def seed_profiles():
    legacy = {
        "invoice": (
            SettingKeys.DMS_INVOICE_CATEGORY_ID,
            SettingKeys.DMS_INVOICE_ID_PROPERTY_ID,
            SettingKeys.DMS_INVOICE_PROPERTY_MAP,
        ),
        "cancellation_invoice": (
            SettingKeys.DMS_INVOICE_CATEGORY_ID,
            SettingKeys.DMS_INVOICE_ID_PROPERTY_ID,
            SettingKeys.DMS_INVOICE_PROPERTY_MAP,
        ),
        "offer": (
            SettingKeys.DMS_OFFER_CATEGORY_ID,
            SettingKeys.DMS_OFFER_ID_PROPERTY_ID,
            SettingKeys.DMS_OFFER_PROPERTY_MAP,
        ),
        "safety_protocol_economic_unit": (
            SettingKeys.DMS_SAFETY_ECONOMIC_UNIT_CATEGORY_ID,
            SettingKeys.DMS_SAFETY_PROTOCOL_ID_PROPERTY_ID,
            SettingKeys.DMS_SAFETY_PROPERTY_MAP,
        ),
        "safety_protocol_building": (
            SettingKeys.DMS_SAFETY_BUILDING_CATEGORY_ID,
            SettingKeys.DMS_SAFETY_PROTOCOL_ID_PROPERTY_ID,
            SettingKeys.DMS_SAFETY_PROPERTY_MAP,
        ),
        "safety_protocol_use_unit": (
            SettingKeys.DMS_SAFETY_USE_UNIT_CATEGORY_ID,
            SettingKeys.DMS_SAFETY_PROTOCOL_ID_PROPERTY_ID,
            SettingKeys.DMS_SAFETY_PROPERTY_MAP,
        ),
    }
    for document_type in DOCUMENT_TYPES:
        if db.session.get(DmsProfile, document_type) is not None:
            continue
        profile = DmsProfile(
            document_type=document_type,
            category_id="",
            key_property_id="",
            mappings={},
        )
        if document_type in legacy:
            category, key_property, mapping = legacy[document_type]
            profile.category_id = settings.get(category) or ""
            profile.key_property_id = settings.get(key_property) or ""
            raw_mapping = settings.get(mapping) or {}
            if isinstance(raw_mapping, dict):
                profile.mappings = {
                    key: {"kind": "text", "value": value}
                    for key, value in raw_mapping.items()
                }
        db.session.add(profile)


def validate_profile(profile, schema):
    if not schema:
        raise DmsError(
            "Bitte die Repository-Metadaten einlesen "
            "und eine Kategorie auswählen."
        )
    fields = {item["id"]: item for item in schema["fields"]}
    key_field = fields.get(profile.key_property_id)
    if profile.key_property_id:
        key_field = fields.get(profile.key_property_id)
        if not key_field or key_field["dataType"] != "STRING" or key_field.get("isMultiValue"):
            raise DmsError("Für den Archivschlüssel wird ein einwertiges Textfeld benötigt.")
    allowed = available_fields(profile.document_type)
    if set(profile.mappings) - set(fields):
        raise DmsError(
            "Eine Eigenschaftszuordnung gehört nicht mehr "
            "zur gewählten Kategorie."
        )
    for property_id, field in fields.items():
        if property_id == profile.key_property_id:
            continue
        mapping = profile.mappings.get(property_id)
        if not mapping or mapping.get("value") in (None, "", []):
            if field.get("required"):
                raise DmsError(
                    f"Für das Pflichtfeld {field_name(field)} "
                    "fehlt eine Zuordnung."
                )
            continue
        if mapping.get("kind") == "field":
            if mapping["value"] not in allowed:
                raise DmsError(
                    f"Die Datenquelle für {field_name(field)} ist ungültig."
                )
        elif mapping.get("kind") == "text":
            values = (
                mapping["value"]
                if isinstance(mapping["value"], list)
                else [mapping["value"]]
            )
            if len(values) > 1 and not field.get("isMultiValue"):
                raise DmsError(
                    f"Das Feld {field_name(field)} erlaubt nur einen Wert."
                )
            for value in values:
                text_fields(
                    str(value),
                    set(template_fields(profile.document_type)),
                )
                if "{" not in str(value) and str(value).strip():
                    normalize_value(value, field)
        else:
            raise DmsError(
                "Die Art einer Eigenschaftszuordnung ist ungültig."
            )


def field_name(field):
    from .dms_client import display_name

    return display_name(field["name"])


def text_fields(template, allowed):
    try:
        for _, name, spec, conversion in Formatter().parse(template):
            if name is not None and (
                name not in allowed or spec or conversion
            ):
                raise DmsError(
                    f"Ungültiger oder unbekannter Platzhalter: {name}."
                )
    except ValueError as exc:
        raise DmsError(
            "Ein Platzhalter ist nicht korrekt geschrieben."
        ) from exc


def profile_snapshot(document_type):
    profile = db.session.get(DmsProfile, document_type)
    if profile is None:
        raise DmsError(
            "Für diese Dokumentart fehlt die DMS-Konfiguration."
        )
    schema = repository_metadata().get("categories", {}).get(
        profile.category_id
    )
    validate_profile(profile, schema)
    return deepcopy({
        "category_id": profile.category_id,
        "key_property_id": profile.key_property_id,
        "mappings": profile.mappings,
        "schema": schema,
    })


def register_generated_pdf(
    document_type, entity_type, entity_id, filename, content, context,
):
    from flask import current_app

    if document_type not in DOCUMENT_TYPES:
        raise DmsError("Die PDF-Dokumentart ist nicht registriert.")
    content = content.read_bytes() if isinstance(content, Path) else content
    if not isinstance(content, bytes) or not content.startswith(b"%PDF-"):
        raise DmsError("Die erzeugte Datei ist kein PDF.")
    content_hash = sha256(content).hexdigest()
    invoice_version = None
    if entity_type == "invoice" and document_type in INVOICE_DOCUMENT_TYPES:
        if lock_invoice_archive(entity_id) is None:
            raise DmsError("Die zugehörige Rechnung wurde nicht gefunden.")
        latest = invoice_archive_documents(entity_id).order_by(
            DmsDocument.invoice_version.desc(),
        ).with_for_update().first()
        if latest is not None and latest.status != "cancelled" and latest.sha256 == content_hash:
            return latest
        invoice_version = 1 if latest is None else latest.invoice_version + 1

    document_id = str(uuid4())
    filename = secure_filename(filename)[:240] or "Dokument.pdf"
    root = Path(current_app.instance_path).resolve()
    target = root / "dms_documents" / document_id / filename
    target.parent.mkdir(parents=True, exist_ok=False)
    try:
        with target.open("xb") as output:
            output.write(content)
    except OSError:
        target.unlink(missing_ok=True)
        target.parent.rmdir()
        raise
    db.session.info.setdefault("dms_generated_files", []).append(target)
    now = utc_now()
    enabled = settings.get(SettingKeys.DMS_ENABLED)
    snapshot = None
    error = None
    hostname = str(settings.get(SettingKeys.DMS_HOSTNAME) or "")
    repository = str(settings.get(SettingKeys.DMS_REPOSITORY) or "")
    if enabled:
        try:
            hostname, _, repository = connection()
            snapshot = profile_snapshot(document_type)
        except DmsError as exc:
            error = str(exc)
    values = deepcopy(context)
    values.update(
        archive_key=f"datlaeuft:pdf:{document_id}",
        generation_id=document_id,
        filename=filename,
        document_type=DOCUMENT_TYPES[document_type],
        generated_at=now.isoformat(),
    )
    document = DmsDocument(
        id=document_id,
        document_type=document_type,
        entity_type=entity_type,
        entity_id=entity_id,
        invoice_version=invoice_version,
        filename=filename,
        storage_name=str(target.relative_to(root)),
        sha256=content_hash,
        context=values,
        profile_snapshot=snapshot,
        hostname=hostname,
        repository_id=repository,
        status="disabled" if not enabled else "blocked" if error else "pending",
        attempts=0,
        available_at=now,
        created_at=now,
        upload_started=False,
        error=error,
    )
    db.session.add(document)
    if document_type != "invoice_with_work_reports":
        model = {
            "invoice": Invoice,
            "offer": Offer,
            "safety_protocol": SafetyInspectionProtocol,
        }.get(entity_type)
        entity = db.session.get(model, entity_id) if model else None
        if entity is not None:
            entity.dms_archive_status = "not_archived"
            if invoice_version is None:
                entity.dms_archive_key = None
                entity.dms_document_id = None
                entity.dms_archive_pdf_sha256 = None
                entity.dms_archived_at = None
                entity.dms_archived_by = None
            entity.dms_archive_attempted_at = None
            entity.dms_archive_error = None
    audit(
        "dms.document.create",
        entity_type,
        entity_id,
        {"generation_id": document_id, "document_type": document_type},
    )
    return document


@event.listens_for(Session, "after_commit")
def clear_generated_files(session):
    if not session.in_nested_transaction():
        session.info.pop("dms_generated_files", None)


@event.listens_for(Session, "after_transaction_end")
def remove_rolled_back_files(session, transaction):
    if transaction.parent is None:
        for path in session.info.pop("dms_generated_files", []):
            path.unlink(missing_ok=True)
            try:
                path.parent.rmdir()
            except OSError:
                pass


def document_path(document):
    from flask import current_app

    root = (Path(current_app.instance_path) / "dms_documents").resolve()
    path = (Path(current_app.instance_path) / document.storage_name).resolve()
    if root not in path.parents or not path.is_file():
        raise DmsError("Die gespeicherte PDF-Datei wurde nicht gefunden.")
    if sha256(path.read_bytes()).hexdigest() != document.sha256:
        raise DmsError(
            "Die Prüfsumme der gespeicherten PDF-Datei stimmt nicht überein."
        )
    return path


def normalize_value(value, field):
    value = str(value).strip()
    data_type = field["dataType"]
    try:
        if data_type in ("NUMERIC", "CURRENCY"):
            number = Decimal(value.replace(",", "."))
            if not number.is_finite():
                raise ValueError()
            if (
                field.get("minValue") is not None
                and number < Decimal(str(field["minValue"]))
            ):
                raise ValueError()
            if (
                field.get("maxValue") is not None
                and number > Decimal(str(field["maxValue"]))
            ):
                raise ValueError()
            return format(number, "f")
        if data_type == "DATE":
            try:
                return date.fromisoformat(value).isoformat()
            except ValueError:
                try:
                    return datetime.fromisoformat(
                        value.replace("Z", "+00:00")
                    ).date().isoformat()
                except ValueError:
                    return datetime.strptime(
                        value, "%d.%m.%Y",
                    ).date().isoformat()
        if data_type == "DATETIME":
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                raise ValueError()
            return timestamp.astimezone(timezone.utc).isoformat()
        if data_type != "STRING":
            raise ValueError()
        return value
    except (ValueError, InvalidOperation) as exc:
        raise DmsError(
            f"Der Wert für {field_name(field)} passt nicht "
            "zum Datentyp oder Wertebereich."
        ) from exc


def upload_properties(document, client):
    snapshot = document.profile_snapshot
    properties = []
    for field in snapshot["schema"]["fields"]:
        property_id = field["id"]
        if property_id == snapshot["key_property_id"]:
            raw_value = document.context["archive_key"]
        else:
            mapping = snapshot["mappings"].get(property_id)
            if not mapping:
                raw_value = None
            elif mapping["kind"] == "field":
                raw_value = document.context.get(mapping["value"])
            else:
                templates = (
                    mapping["value"]
                    if isinstance(mapping["value"], list)
                    else [mapping["value"]]
                )
                raw_value = []
                for template in templates:
                    text_fields(
                        str(template),
                        {
                            key
                            for key, value in document.context.items()
                            if not isinstance(value, list)
                        },
                    )
                    raw_value.append(
                        str(template).format_map(document.context)
                    )
        raw_values = raw_value if isinstance(raw_value, list) else [raw_value]
        values = [
            normalize_value(value, field)
            for value in raw_values
            if value is not None and str(value).strip()
        ]
        if not values and field.get("required"):
            raise DmsError(
                f"Das Pflichtfeld {field_name(field)} "
                "ist für dieses Dokument leer."
            )
        if len(values) > 1 and not field.get("isMultiValue"):
            raise DmsError(
                f"Das Feld {field_name(field)} erlaubt nur einen Wert."
            )
        if values:
            properties = client.add_upload_property(
                "", values, prop_guid=property_id, plist=properties,
            )
    return properties


def retry_document(document, *, confirmed_absent=False, manual=False):
    if document.invoice_version is not None:
        lock_invoice_archive(document.entity_id)
    db.session.refresh(document, with_for_update=True)
    if document.status == "cancelled":
        raise DmsError("Dieses Dokument wurde aus der Warteschlange entfernt.")
    if document.status == "archived":
        raise DmsError("Dieses Dokument ist bereits archiviert.")
    if document.status == "processing":
        raise DmsError("Dieses Dokument wird gerade übertragen.")
    if not settings.get(SettingKeys.DMS_ENABLED):
        raise DmsError("Die automatische Archivierung ist deaktiviert.")
    if document.invoice_version is not None and document.status == "disabled":
        newer = invoice_archive_documents(document.entity_id).filter(
            DmsDocument.invoice_version > document.invoice_version,
            DmsDocument.status != "cancelled",
        ).with_for_update().first()
        if newer is not None:
            raise DmsError("Für diese Rechnung liegt bereits eine neuere Fassung zur Archivierung vor.")

    upload_started = document.upload_started and not confirmed_absent
    if upload_started and not can_check_upload(document):
        raise DmsError(
            "Bitte die vorhandene d.velop-ID zuordnen oder nach Prüfung bestätigen, "
            "dass dieses PDF nicht archiviert wurde."
        )

    values = {
        "upload_started": upload_started,
        "status": (
            "manual_pending" if manual
            else "uncertain" if upload_started else "pending"
        ),
        "available_at": utc_now(),
        "error": None,
    }
    if not upload_started:
        hostname, _, repository = connection()
        values.update(
            hostname=hostname,
            repository_id=repository,
            profile_snapshot=profile_snapshot(document.document_type),
        )

    changed = db.session.execute(
        update(DmsDocument).where(
            DmsDocument.id == document.id,
            DmsDocument.lock_token.is_(None),
            DmsDocument.status.notin_(("processing", "archived", "cancelled")),
            DmsDocument.upload_started == document.upload_started,
        ).values(**values)
    ).rowcount
    if changed != 1:
        raise DmsError(
            "Der Archivierungsauftrag wird bereits anderweitig bearbeitet."
        )

    db.session.refresh(document)
    audit(
        "dms.document.retry",
        "dms_document",
        document.id,
        {"confirmed_absent": confirmed_absent, "manual": manual},
    )


def remove_from_queue(document, *, confirmed_absent=False):
    db.session.refresh(document, with_for_update=True)
    if document.status == "cancelled":
        return
    if document.status in ("archived", "processing") or document.lock_token:
        raise DmsError(
            "Archivierte oder laufende Übertragungen können nicht entfernt werden."
        )
    if document.upload_started and not confirmed_absent:
        raise DmsError(
            "Bitte zuerst im ursprünglichen Repository prüfen und bestätigen, "
            "dass dieses PDF nicht archiviert wurde."
        )

    changed = db.session.execute(
        update(DmsDocument).where(
            DmsDocument.id == document.id,
            DmsDocument.status.notin_(("processing", "archived", "cancelled")),
            DmsDocument.lock_token.is_(None),
            DmsDocument.upload_started == document.upload_started,
        ).values(
            status="cancelled",
            upload_started=False,
            locked_at=None,
            lock_token=None,
            error=None,
        )
    ).rowcount
    if changed != 1:
        raise DmsError(
            "Der Archivierungsauftrag wird bereits anderweitig bearbeitet."
        )

    db.session.refresh(document)
    audit(
        "dms.document.remove_from_queue",
        "dms_document",
        document.id,
        {"confirmed_absent": confirmed_absent},
    )


def retry_latest_document(entity_type, entity_id):
    if entity_type == "invoice":
        lock_invoice_archive(entity_id)
        versions = invoice_archive_documents(entity_id)
        latest = versions.filter(DmsDocument.status != "cancelled").order_by(
            DmsDocument.invoice_version.desc(),
        ).with_for_update().first()
        if latest is not None:
            if latest.status != "archived":
                next_version = versions.filter(DmsDocument.status.notin_(COMPLETED_INVOICE_JOBS)).order_by(
                    DmsDocument.invoice_version,
                ).with_for_update().first()
                retry_document(next_version or latest)
            return latest
    document = (
        DmsDocument.query
        .filter_by(entity_type=entity_type, entity_id=entity_id)
        .filter(DmsDocument.document_type != "invoice_with_work_reports")
        .order_by(DmsDocument.created_at.desc())
        .first()
    )
    if document is None:
        raise DmsError(
            "Für dieses Dokument liegt kein neuer Archivierungsauftrag vor."
        )
    if document.status != "archived":
        retry_document(document)
    return document


def sync_legacy_archive_fields(document):
    if document.invoice_version is not None:
        entity = db.session.get(Invoice, document.entity_id)
        latest = invoice_archive_documents(document.entity_id).filter(DmsDocument.status != "cancelled").order_by(
            DmsDocument.invoice_version.desc(),
        ).with_for_update().first()
        if latest is None or latest.id != document.id:
            return
    else:
        latest_id = db.session.execute(
            select(DmsDocument.id).where(
                DmsDocument.entity_type == document.entity_type,
                DmsDocument.entity_id == document.entity_id,
                DmsDocument.document_type != "invoice_with_work_reports",
            ).order_by(DmsDocument.created_at.desc()).limit(1)
        ).scalar_one_or_none()
        if latest_id != document.id:
            return
        model = {
            "invoice": Invoice,
            "offer": Offer,
            "safety_protocol": SafetyInspectionProtocol,
        }.get(document.entity_type)
        entity = db.session.get(model, document.entity_id) if model else None
        if entity is None or entity.document_sha256 != document.sha256:
            return
    if entity is None:
        return
    entity.dms_archive_status = "archived"
    entity.dms_archive_key = document.context["archive_key"]
    entity.dms_document_id = document.dms_document_id
    entity.dms_archive_pdf_sha256 = document.sha256
    entity.dms_archived_at = document.archived_at
    entity.dms_archive_attempted_at = utc_now()
    entity.dms_archive_error = None


def can_check_upload(document):
    return bool(
        (document.invoice_version is not None and document.dms_document_id)
        or (document.profile_snapshot or {}).get("key_property_id")
    )


def link_existing_document(document, remote_id):
    remote_id = str(remote_id or "").strip()
    if (
        not remote_id or len(remote_id) > 80 or not remote_id.isascii()
        or any(not (character.isalnum() or character in "-_") for character in remote_id)
    ):
        raise DmsError("Bitte eine gültige d.velop-Dokument-ID eingeben.")

    if document.invoice_version is not None:
        lock_invoice_archive(document.entity_id)
    db.session.refresh(document, with_for_update=True)
    if (
        document.status not in ("blocked", "uncertain")
        or not document.upload_started or document.lock_token is not None
        or not document.profile_snapshot
    ):
        raise DmsError("Für diesen Archivierungsauftrag ist keine manuelle Zuordnung möglich.")

    hostname, api_key, repository = connection()
    if (hostname, repository) != (document.hostname, document.repository_id):
        raise DmsError("Bitte zuerst die ursprüngliche DMS-Anbindung wiederherstellen.")

    client = CloudDmsClient(hostname, api_key, repository)
    try:
        if document.invoice_version is not None and document.dms_document_id:
            verified = (
                remote_id == document.dms_document_id
                and client.document_sha256(remote_id) == document.sha256
            )
        else:
            matches = client.get_documents(doc_id=remote_id)
            verified = (
                len(matches) == 1 and str(matches[0].id_) == remote_id
                and document.profile_snapshot["category_id"] in (getattr(matches[0], "source_categories", None) or [])
            )
    except DmsError:
        raise
    except Exception as exc:
        raise DmsError("Das DMS-Dokument konnte nicht geprüft werden.") from exc
    finally:
        client.close()

    if not verified:
        raise DmsError("Das angegebene DMS-Dokument konnte für diese PDF nicht bestätigt werden.")

    assignments = select(DmsDocument.id).where(
        DmsDocument.id != document.id,
        DmsDocument.hostname == hostname,
        DmsDocument.repository_id == repository,
        DmsDocument.dms_document_id == remote_id,
    )
    if document.invoice_version is not None and document.dms_document_id:
        assignments = assignments.where(~(
            (DmsDocument.entity_type == document.entity_type)
            & (DmsDocument.entity_id == document.entity_id)
            & DmsDocument.invoice_version.is_not(None)
        ))
    assigned_id = db.session.execute(assignments.limit(1)).scalar_one_or_none()
    if assigned_id is not None:
        raise DmsError("Diese d.velop-ID ist bereits einem anderen Archivierungsauftrag zugeordnet.")

    changed = db.session.execute(
        update(DmsDocument).where(
            DmsDocument.id == document.id,
            DmsDocument.status.in_(("blocked", "uncertain")),
            DmsDocument.upload_started.is_(True),
            DmsDocument.lock_token.is_(None),
        ).values(
            status="archived",
            dms_document_id=remote_id,
            archived_at=utc_now(),
            locked_at=None,
            lock_token=None,
            error=None,
        )
    ).rowcount
    if changed != 1:
        raise DmsError("Der Archivierungsauftrag wird bereits anderweitig bearbeitet.")

    db.session.refresh(document)
    sync_legacy_archive_fields(document)
    audit(
        "dms.document.link",
        "dms_document",
        document.id,
        {"dms_document_id": remote_id},
    )


def process_dms_batch(limit=10):
    if not settings.get(SettingKeys.DMS_ENABLED):
        return 0

    threshold = utc_now() - timedelta(minutes=10)
    db.session.execute(
        update(DmsDocument).where(
            DmsDocument.status == "processing",
            DmsDocument.locked_at < threshold,
        ).values(
            status=case(
                (DmsDocument.upload_started.is_(True), "uncertain"),
                else_="pending",
            ),
            locked_at=None,
            lock_token=None,
            available_at=utc_now(),
        )
    )
    db.session.commit()

    earlier = aliased(DmsDocument)
    has_earlier_job = select(earlier.id).where(
        earlier.entity_type == DmsDocument.entity_type,
        earlier.entity_id == DmsDocument.entity_id,
        earlier.invoice_version < DmsDocument.invoice_version,
        earlier.status.notin_(COMPLETED_INVOICE_JOBS),
    ).exists()
    seen = 0
    for _ in range(limit):
        control_rows = db.session.execute(
            select(AppSetting).where(
                AppSetting.key.in_((
                    SettingKeys.DMS_ENABLED.key,
                    SettingKeys.DMS_PROCESSING_PAUSED.key,
                )),
            ).order_by(AppSetting.key).with_for_update()
            .execution_options(populate_existing=True)
        ).scalars().all()
        control_values = {
            row.key: str(row.value_text).lower()
            in {"1", "true", "yes", "on", "ja"}
            for row in control_rows
        }
        if not control_values.get(SettingKeys.DMS_ENABLED.key, False):
            db.session.rollback()
            break

        paused = control_values.get(
            SettingKeys.DMS_PROCESSING_PAUSED.key, False,
        )
        allowed_statuses = (
            ("manual_pending",) if paused
            else ("pending", "failed", "uncertain", "manual_pending")
        )

        document_id = db.session.execute(
            select(DmsDocument.id).where(
                DmsDocument.status.in_(allowed_statuses),
                DmsDocument.available_at <= utc_now(),
                DmsDocument.lock_token.is_(None),
                ~has_earlier_job,
            ).order_by(
                DmsDocument.available_at,
                DmsDocument.created_at,
            ).limit(1)
        ).scalar_one_or_none()
        if document_id is None:
            db.session.rollback()
            break

        token = str(uuid4())
        claimed = db.session.execute(
            update(DmsDocument).where(
                DmsDocument.id == document_id,
                DmsDocument.status.in_(allowed_statuses),
                DmsDocument.available_at <= utc_now(),
                DmsDocument.lock_token.is_(None),
            ).values(
                status="processing",
                locked_at=utc_now(),
                lock_token=token,
                attempts=DmsDocument.attempts + 1,
            )
        ).rowcount
        db.session.commit()
        if claimed != 1:
            continue

        seen += 1
        process_claimed_document(document_id, token)

    return seen


def invoice_version_target(document):
    if document.invoice_version is None or (document.upload_started and not document.dms_document_id):
        return None
    earlier = invoice_archive_documents(document.entity_id).filter(
        DmsDocument.invoice_version < document.invoice_version,
        DmsDocument.status == "archived",
        DmsDocument.dms_document_id.is_not(None),
    ).order_by(DmsDocument.invoice_version.desc()).first()
    if earlier is not None:
        if (earlier.hostname, earlier.repository_id) != (document.hostname, document.repository_id):
            raise DmsError("Bitte die ursprüngliche DMS-Anbindung für die Rechnung wiederherstellen.")
        return earlier.dms_document_id, earlier.context["archive_key"], None

    invoice = db.session.get(Invoice, document.entity_id)
    if invoice is not None and (invoice.dms_document_id or invoice.dvelop_document_id):
        remote_id = invoice.dms_document_id or invoice.dvelop_document_id
        previous_sha = invoice.dms_archive_pdf_sha256 or invoice.document_sha256 or ""
        return remote_id, invoice.dms_archive_key, previous_sha
    return None


def archive_invoice_version(document, client, path, version_id, previous_sha):
    if document.upload_started or previous_sha is not None:
        remote_sha = client.document_sha256(version_id)
        if remote_sha == document.sha256:
            return
        if document.upload_started:
            raise DmsError(
                "Die neue PDF-Version ist in d.velop noch nicht bestätigt.",
                retryable=True, uncertain=True,
            )
        if remote_sha != previous_sha:
            raise DmsError("Die vorhandene d.velop-ID konnte für diese Rechnung nicht bestätigt werden.")
    client.archive_invoice_version(path, version_id)


def process_claimed_document(document_id, token):
    document = db.session.get(DmsDocument, document_id)
    client = None
    created_in_this_attempt = False
    try:
        hostname, api_key, repository = connection()
        if (hostname, repository) != (document.hostname, document.repository_id):
            raise DmsError("Die DMS-Anbindung wurde geändert. Bitte den Archivierungsauftrag prüfen.")
        if not document.profile_snapshot:
            raise DmsError("Die Eigenschaftszuordnung ist unvollständig.")

        snapshot = document.profile_snapshot
        if document.upload_started and not can_check_upload(document):
            raise DmsError(
                "Die Upload-Bestätigung fehlt. Bitte die vorhandene d.velop-ID zuordnen "
                "oder nach Prüfung einen erneuten Upload freigeben.",
                uncertain=True,
            )

        path = document_path(document)
        version_target = invoice_version_target(document)
        version_id = version_target[0] if version_target else None
        archive_context = document.context
        if version_target and version_target[1]:
            archive_context = {**archive_context, "archive_key": version_target[1]}

        def before_create():
            nonlocal created_in_this_attempt
            claimed = db.session.execute(
                update(DmsDocument).where(
                    DmsDocument.id == document_id,
                    DmsDocument.lock_token == token,
                    DmsDocument.status == "processing",
                ).values(
                    upload_started=True, locked_at=utc_now(),
                    dms_document_id=version_id, context=archive_context,
                )
            ).rowcount
            if claimed != 1:
                db.session.rollback()
                raise DmsError("Der Archivierungsauftrag wird bereits anderweitig bearbeitet.")
            db.session.commit()
            created_in_this_attempt = True

        client = CloudDmsClient(
            hostname, api_key, repository, before_create=before_create
        )
        if version_id:
            remote_id = version_id
            archive_invoice_version(document, client, path, version_id, version_target[2])
        else:
            remote_id = archive_new_document(document, client, path)

        if not remote_id or remote_id == "unknown":
            raise DmsError(
                "d.velop hat keine Dokument-ID bestätigt.",
                retryable=True,
                uncertain=True,
            )

        if document.invoice_version is not None:
            lock_invoice_archive(document.entity_id)
        db.session.refresh(document, with_for_update=True)
        if document.lock_token == token:
            document.status = "archived"
            document.dms_document_id = remote_id
            document.context = archive_context
            document.archived_at = utc_now()
            document.locked_at = None
            document.lock_token = None
            document.error = None
            sync_legacy_archive_fields(document)
            audit(
                "dms.document.archive",
                "dms_document",
                document.id,
                {"dms_document_id": remote_id},
            )
            db.session.commit()
        else:
            db.session.rollback()
    except Exception as exc:
        db.session.rollback()
        document = db.session.execute(
            select(DmsDocument)
            .where(DmsDocument.id == document_id)
            .with_for_update()
        ).scalar_one()

        if document.lock_token == token:
            if created_in_this_attempt and isinstance(exc, DmsError) and not exc.uncertain:
                document.upload_started = False

            can_check = can_check_upload(document)
            if document.upload_started:
                document.status = "uncertain" if can_check else "blocked"
            else:
                document.status = "failed" if getattr(exc, "retryable", False) else "blocked"

            document.available_at = utc_now() + timedelta(
                seconds=min(3600, 30 * 2 ** min(document.attempts, 7))
            )
            document.locked_at = None
            document.lock_token = None
            document.error = (
                str(exc)[:1000]
                if isinstance(exc, DmsError)
                else "Unerwarteter Fehler bei der Archivierung."
            )
            if document.upload_started and not can_check:
                document.error = (
                    "Die Upload-Bestätigung fehlt. Bitte die vorhandene d.velop-ID zuordnen "
                    "oder nach Prüfung einen erneuten Upload freigeben."
                )
            db.session.commit()
        else:
            db.session.rollback()
    finally:
        if client is not None:
            client.close()


def archive_new_document(document, client, path):
    snapshot = document.profile_snapshot
    key_property_id = snapshot.get("key_property_id")
    if key_property_id:
        filters = client.add_property(
            "", document.context["archive_key"], prop_guid=key_property_id,
        )
        matches = client.get_documents(
            properties=filters, categories=[snapshot["category_id"]], limit=2,
        )
        if len(matches) > 1:
            raise DmsError("Mehrere DMS-Dokumente besitzen denselben Erzeugungsschlüssel.")
        if matches:
            return str(matches[0].id_ or "")
    if document.upload_started:
        raise DmsError(
            "Die Upload-Bestätigung steht aus. Das DMS wird erneut geprüft.",
            retryable=True, uncertain=True,
        )
    if document.document_type == "invoice_with_work_reports":
        raise DmsError("Rechnungspakete werden über die Archivierung der zugehörigen Rechnung abgelegt.")
    return str(client.archive_file(
        str(path), category_id=snapshot["category_id"],
        properties=upload_properties(document, client),
    ) or "")
