from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
from string import Formatter
from uuid import uuid4

from sqlalchemy import case, event, select, update
from sqlalchemy.orm import Session
from werkzeug.utils import secure_filename

from .audit import audit
from .dms_client import CloudDmsClient, DmsError, normalize_hostname
from .dms_context import DOCUMENT_TYPES, available_fields
from .extensions import db
from .models import (
    DmsDocument, DmsProfile, Invoice, Offer, SafetyInspectionProtocol, utc_now,
)
from .settings import SettingKeys, settings


STATUS_LABELS = {
    "disabled": "Archivierung war deaktiviert",
    "pending": "Ausstehend",
    "processing": "Wird übertragen",
    "archived": "Archiviert",
    "failed": "Wird erneut versucht",
    "blocked": "Prüfung erforderlich",
    "uncertain": "Upload-Bestätigung ausstehend",
}


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
    if (
        not key_field
        or key_field["dataType"] != "STRING"
        or key_field.get("isMultiValue")
    ):
        raise DmsError(
            "Für den Archivschlüssel wird ein einwertiges Textfeld benötigt."
        )
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
                    set(allowed) - {"commission_numbers", "craftsmen"},
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
        filename=filename,
        storage_name=str(target.relative_to(root)),
        sha256=sha256(content).hexdigest(),
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


def retry_document(document, *, confirmed_absent=False):
    db.session.refresh(document, with_for_update=True)
    if document.status == "archived":
        raise DmsError("Dieses Dokument ist bereits archiviert.")
    if document.status == "processing":
        raise DmsError("Dieses Dokument wird gerade übertragen.")
    if not settings.get(SettingKeys.DMS_ENABLED):
        raise DmsError("Die automatische Archivierung ist deaktiviert.")
    upload_started = document.upload_started and not confirmed_absent
    values = {
        "upload_started": upload_started,
        "status": "uncertain" if upload_started else "pending",
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
            DmsDocument.status.notin_(("processing", "archived")),
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
        {"confirmed_absent": confirmed_absent},
    )


def retry_latest_document(entity_type, entity_id):
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
    if entity is not None and entity.document_sha256 == document.sha256:
        entity.dms_archive_status = "archived"
        entity.dms_archive_key = document.context["archive_key"]
        entity.dms_document_id = document.dms_document_id
        entity.dms_archive_pdf_sha256 = document.sha256
        entity.dms_archived_at = document.archived_at
        entity.dms_archive_attempted_at = utc_now()
        entity.dms_archive_error = None


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
    seen = 0
    for _ in range(limit):
        document_id = db.session.execute(
            select(DmsDocument.id).where(
                DmsDocument.status.in_(("pending", "failed", "uncertain")),
                DmsDocument.available_at <= utc_now(),
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
                DmsDocument.status.in_(("pending", "failed", "uncertain")),
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


def process_claimed_document(document_id, token):
    document = db.session.get(DmsDocument, document_id)
    client = None
    created_in_this_attempt = False
    try:
        hostname, api_key, repository = connection()
        if (hostname, repository) != (
            document.hostname, document.repository_id,
        ):
            raise DmsError(
                "Die DMS-Anbindung wurde geändert. "
                "Bitte den Archivierungsauftrag prüfen."
            )
        if not document.profile_snapshot:
            raise DmsError("Die Eigenschaftszuordnung ist unvollständig.")
        path = document_path(document)

        def before_create():
            nonlocal created_in_this_attempt
            claimed = db.session.execute(
                update(DmsDocument).where(
                    DmsDocument.id == document_id,
                    DmsDocument.lock_token == token,
                    DmsDocument.status == "processing",
                ).values(upload_started=True, locked_at=utc_now())
            ).rowcount
            if claimed != 1:
                db.session.rollback()
                raise DmsError(
                    "Der Archivierungsauftrag wird bereits "
                    "anderweitig bearbeitet."
                )
            db.session.commit()
            created_in_this_attempt = True

        client = CloudDmsClient(
            hostname, api_key, repository, before_create=before_create,
        )
        snapshot = document.profile_snapshot
        filters = client.add_property(
            "",
            document.context["archive_key"],
            prop_guid=snapshot["key_property_id"],
        )
        matches = client.get_documents(
            properties=filters,
            categories=[snapshot["category_id"]],
            limit=2,
        )
        if len(matches) > 1:
            raise DmsError(
                "Mehrere DMS-Dokumente besitzen "
                "denselben Erzeugungsschlüssel."
            )
        if matches:
            remote_id = str(matches[0].id_ or "")
        elif document.upload_started:
            raise DmsError(
                "Die Upload-Bestätigung steht aus. "
                "Das DMS wird erneut geprüft.",
                retryable=True,
                uncertain=True,
            )
        else:
            remote_id = str(
                client.archive_file(
                    str(path),
                    category_id=snapshot["category_id"],
                    properties=upload_properties(document, client),
                ) or ""
            )
        if not remote_id or remote_id == "unknown":
            raise DmsError(
                "d.velop hat keine Dokument-ID bestätigt.",
                retryable=True,
                uncertain=True,
            )
        db.session.refresh(document, with_for_update=True)
        if document.lock_token == token:
            document.status = "archived"
            document.dms_document_id = remote_id
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
            if (
                created_in_this_attempt
                and isinstance(exc, DmsError)
                and not exc.uncertain
            ):
                document.upload_started = False
            document.status = (
                "uncertain"
                if document.upload_started
                else "failed"
                if getattr(exc, "retryable", False)
                else "blocked"
            )
            document.available_at = utc_now() + timedelta(
                seconds=min(
                    3600,
                    30 * 2 ** min(document.attempts, 7),
                )
            )
            document.locked_at = None
            document.lock_token = None
            document.error = (
                str(exc)[:1000]
                if isinstance(exc, DmsError)
                else "Unerwarteter Fehler bei der Archivierung."
            )
            db.session.commit()
        else:
            db.session.rollback()
    finally:
        if client is not None:
            client.close()