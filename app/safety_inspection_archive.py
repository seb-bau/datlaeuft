from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from dvelopdmspy.dvelopdmspy import (
    DvelopDmsPy,
)
from flask import current_app

from .audit import audit
from .extensions import db
from .models import (
    SafetyInspectionArchiveStatus,
    SafetyInspectionGroup,
    SafetyInspectionJob,
    SafetyInspectionObjectLevel,
    SafetyInspectionPhoto,
    SafetyInspectionProtocol,
    SafetyInspectionResult,
    User,
    utc_now,
)
from .photo_service import (
    commission_photo_path,
)
from .safety_inspection_document import (
    SafetyInspectionDocumentError,
    resolve_safety_inspection_protocol_path,
)
from .settings import (
    SettingDefinition,
    SettingKeys,
    settings,
)
from .dms_client import CloudDmsClient


class SafetyInspectionArchiveError(
    RuntimeError
):
    pass


@dataclass(frozen=True)
class SafetyInspectionArchiveResult:
    document_id: str
    already_archived: bool


class ArchiveValueMap(dict):
    def __missing__(
        self,
        key: str,
    ) -> str:
        return ""


def required_archive_setting(
    definition: SettingDefinition,
) -> str:
    value = str(
        settings.get(definition) or ""
    ).strip()

    if not value:
        raise (
            SafetyInspectionArchiveError(
                "Die Einstellung "
                f"„{definition.label}“ fehlt."
            )
        )

    return value


def category_definition(
    object_level: str,
) -> SettingDefinition:
    definitions = {
        SafetyInspectionObjectLevel
        .ECONOMIC_UNIT.value: (
            SettingKeys
            .DMS_SAFETY_ECONOMIC_UNIT_CATEGORY_ID
        ),
        SafetyInspectionObjectLevel
        .BUILDING.value: (
            SettingKeys
            .DMS_SAFETY_BUILDING_CATEGORY_ID
        ),
        SafetyInspectionObjectLevel
        .USE_UNIT.value: (
            SettingKeys
            .DMS_SAFETY_USE_UNIT_CATEGORY_ID
        ),
    }

    try:
        return definitions[object_level]
    except KeyError as exc:
        raise (
            SafetyInspectionArchiveError(
                "Die Objektebene des "
                "Prüfprotokolls ist unbekannt."
            )
        ) from exc


def protocol_archive_values(
    protocol: SafetyInspectionProtocol,
    archive_key: str,
) -> ArchiveValueMap:
    job = protocol.job

    return ArchiveValueMap(
        {
            "archive_key": archive_key,
            "protocol_id": str(
                protocol.id
            ),
            "protocol_version": str(
                protocol.version_number
            ),
            "job_id": str(job.id),
            "job_public_id": (
                job.public_id
            ),
            "inspection_name": (
                job.template_version
                .template.name
            ),
            "inspection_group": (
                job.group.name
            ),
            "status": job.status,
            "period_start": (
                job.period_start
                .isoformat()
            ),
            "period_end": (
                job.period_end.isoformat()
            ),
            "due_from": (
                job.due_from.isoformat()
            ),
            "due_to": (
                job.due_to.isoformat()
            ),
            "completed_at": (
                job.completed_at
                .isoformat()
                if job.completed_at
                else ""
            ),
            "completed_by": (
                job.completed_by
                .display_name
                if job.completed_by
                else ""
            ),
            "object_level": (
                job.object_level
            ),
            "object_erp_id": str(
                job.object_erp_id
            ),
            "company_code_erp_id": str(
                job.company_code_erp_id
                or ""
            ),
            "company_code_code": (
                job.company_code_code or ""
            ),
            "economic_unit_erp_id": str(
                job.economic_unit_erp_id
            ),
            "economic_unit_idnum": (
                job.economic_unit_idnum
                or ""
            ),
            "economic_unit_name": (
                job.economic_unit_name
                or ""
            ),
            "building_erp_id": str(
                job.building_erp_id or ""
            ),
            "building_idnum": (
                job.building_idnum or ""
            ),
            "use_unit_erp_id": str(
                job.use_unit_erp_id or ""
            ),
            "use_unit_idnum": (
                job.use_unit_idnum or ""
            ),
            "object_address": (
                job.object_address or ""
            ),
            "object_location": (
                job.object_location or ""
            ),
            "department": (
                job.department.name
                if job.department
                else ""
            ),
            "defect_count": str(
                len(job.defects)
            ),
            "correction_reason": (
                protocol.correction_reason
                or ""
            ),
        }
    )


def archived_protocol_for_photo(
    photo: SafetyInspectionPhoto,
) -> SafetyInspectionProtocol:
    protocols = [
        protocol
        for protocol in photo.result.job.protocols
        if (
            protocol.dms_archive_status
            == SafetyInspectionArchiveStatus
            .ARCHIVED.value
        )
    ]

    if not protocols:
        raise SafetyInspectionArchiveError(
            "Vor der Archivierung eines "
            "Originalfotos muss das "
            "Prüfprotokoll archiviert sein."
        )

    return max(
        protocols,
        key=lambda protocol: (
            protocol.version_number
        ),
    )


def photo_archive_values(
    photo: SafetyInspectionPhoto,
    protocol: SafetyInspectionProtocol,
    archive_key: str,
) -> ArchiveValueMap:
    values = protocol_archive_values(
        protocol,
        archive_key,
    )
    values.update(
        {
            "document_type": "photo",
            "photo_id": str(photo.id),
            "photo_public_id": photo.public_id,
            "photo_filename": (
                photo.original_filename
            ),
            "photo_caption": (
                photo.caption or ""
            ),
            "photo_captured_at": (
                photo.captured_at.isoformat()
                if photo.captured_at
                else ""
            ),
            "photo_sha256": (
                photo.content_sha256
            ),
            "inspection_point": (
                photo.result.item_title
            ),
        }
    )
    return values


def render_property_value(
    template: str,
    values: ArchiveValueMap,
) -> str:
    try:
        return template.format_map(
            values
        )
    except (
        ValueError,
        AttributeError,
    ) as exc:
        raise (
            SafetyInspectionArchiveError(
                "Eine d.velop-"
                "Eigenschaftszuordnung für "
                "Prüfprotokolle ist ungültig."
            )
        ) from exc


def create_archive_client() -> (
    DvelopDmsPy
):
    try:
        return CloudDmsClient(
            hostname=required_archive_setting(
                SettingKeys.DMS_HOSTNAME
            ),
            api_key=required_archive_setting(
                SettingKeys.DMS_API_KEY
            ),
            repository=required_archive_setting(
                SettingKeys.DMS_REPOSITORY
            ),
        )
    except SafetyInspectionArchiveError:
        raise
    except Exception as exc:
        raise (
            SafetyInspectionArchiveError(
                "Die Verbindung zu d.velop "
                "konnte nicht hergestellt "
                "werden."
            )
        ) from exc


def verify_protocol_pdf(
    protocol: SafetyInspectionProtocol,
) -> tuple[Path, str]:
    try:
        document_path = (
            resolve_safety_inspection_protocol_path(
                protocol.document_filename
            )
        )
    except (
        SafetyInspectionDocumentError
    ) as exc:
        raise (
            SafetyInspectionArchiveError(
                str(exc)
            )
        ) from exc

    if not document_path.is_file():
        raise (
            SafetyInspectionArchiveError(
                "Die Datei des "
                "Prüfprotokolls fehlt."
            )
        )

    content_hash = sha256(
        document_path.read_bytes()
    ).hexdigest()

    if (
        content_hash
        != protocol.document_sha256
    ):
        raise (
            SafetyInspectionArchiveError(
                "Die Prüfsumme des "
                "Prüfprotokolls stimmt nicht "
                "mit der freigegebenen Version "
                "überein."
            )
        )

    return document_path, content_hash


def verify_safety_inspection_photo(
    photo: SafetyInspectionPhoto,
) -> tuple[Path, str]:
    if not photo.active or photo.deleted_at is not None:
        raise SafetyInspectionArchiveError(
            "Das Prüffoto ist nicht mehr aktiv."
        )

    photo_path = commission_photo_path(
        photo.storage_name
    )

    if not photo_path.is_file():
        raise SafetyInspectionArchiveError(
            "Die Datei des Prüffotos fehlt."
        )

    content_hash = sha256(
        photo_path.read_bytes()
    ).hexdigest()

    if content_hash != photo.content_sha256:
        raise SafetyInspectionArchiveError(
            "Die Prüfsumme des Prüffotos "
            "stimmt nicht mit dem gespeicherten "
            "Datensatz überein."
        )

    return photo_path, content_hash


def existing_document_id(
    client: DvelopDmsPy,
    category_id: str,
    property_id: str,
    archive_key: str,
) -> str | None:
    search_properties = (
        client.add_property(
            "",
            archive_key,
            prop_guid=property_id,
        )
    )

    try:
        documents = client.get_documents(
            properties=search_properties,
            categories=[category_id],
            limit=2,
        )
    except Exception as exc:
        raise (
            SafetyInspectionArchiveError(
                "Das Prüfprotokoll konnte "
                "in d.velop nicht gesucht "
                "werden."
            )
        ) from exc

    if len(documents) > 1:
        raise (
            SafetyInspectionArchiveError(
                "In d.velop wurden mehrere "
                "Dokumente mit demselben "
                "Protokollschlüssel gefunden."
            )
        )

    if not documents:
        return None

    document_id = str(
        documents[0].id_ or ""
    ).strip()

    if not document_id:
        raise (
            SafetyInspectionArchiveError(
                "d.velop hat für das "
                "vorhandene Dokument keine "
                "ID geliefert."
            )
        )

    return document_id


def upload_properties(
    client: DvelopDmsPy,
    protocol: SafetyInspectionProtocol,
    archive_key: str,
    id_property_id: str,
    values: ArchiveValueMap | None = None,
) -> list[dict]:
    raw_mapping = settings.get(
        SettingKeys
        .DMS_SAFETY_PROPERTY_MAP
    )

    if raw_mapping is None:
        raw_mapping = {}

    if not isinstance(
        raw_mapping,
        dict,
    ):
        raise (
            SafetyInspectionArchiveError(
                "Die d.velop-"
                "Eigenschaftszuordnung für "
                "Prüfprotokolle muss ein "
                "JSON-Objekt sein."
            )
        )

    if values is None:
        values = protocol_archive_values(
            protocol,
            archive_key,
        )
    property_values = {
        id_property_id: archive_key,
    }

    for property_id, template in (
        raw_mapping.items()
    ):
        normalized_property_id = str(
            property_id or ""
        ).strip()

        if not normalized_property_id:
            raise (
                SafetyInspectionArchiveError(
                    "Die d.velop-"
                    "Eigenschaftszuordnung "
                    "enthält eine leere "
                    "Eigenschafts-ID."
                )
            )

        value = render_property_value(
            str(template),
            values,
        )

        if (
            normalized_property_id
            == id_property_id
            and value != archive_key
        ):
            raise (
                SafetyInspectionArchiveError(
                    "Der eindeutige "
                    "Protokollschlüssel darf "
                    "nicht überschrieben werden."
                )
            )

        property_values[
            normalized_property_id
        ] = value

    properties = []

    for property_id, value in (
        property_values.items()
    ):
        properties = (
            client.add_upload_property(
                "",
                value,
                prop_guid=property_id,
                plist=properties,
            )
        )

    return properties


def archive_safety_inspection_protocol(
    protocol: SafetyInspectionProtocol,
    user: User,
) -> SafetyInspectionArchiveResult:
    (
        document_path,
        content_hash,
    ) = verify_protocol_pdf(protocol)

    if (
        protocol.dms_archive_status
        == SafetyInspectionArchiveStatus
        .ARCHIVED.value
        and protocol.dms_document_id
    ):
        if (
            protocol
            .dms_archive_pdf_sha256
            and (
                protocol
                .dms_archive_pdf_sha256
                != content_hash
            )
        ):
            raise (
                SafetyInspectionArchiveError(
                    "Die archivierte Prüfsumme "
                    "stimmt nicht mit dem "
                    "aktuellen Prüfprotokoll "
                    "überein."
                )
            )

        return (
            SafetyInspectionArchiveResult(
                document_id=(
                    protocol.dms_document_id
                ),
                already_archived=True,
            )
        )

    category_id = (
        required_archive_setting(
            category_definition(
                protocol.job.object_level
            )
        )
    )
    id_property_id = (
        required_archive_setting(
            SettingKeys
            .DMS_SAFETY_PROTOCOL_ID_PROPERTY_ID
        )
    )
    archive_key = (
        protocol.dms_archive_key
        or (
            "datlaeuft-safety-protocol-"
            f"{protocol.id}"
        )
    )

    protocol.dms_archive_key = archive_key
    protocol.dms_archive_attempted_at = (
        utc_now()
    )

    client = create_archive_client()
    document_id = existing_document_id(
        client,
        category_id,
        id_property_id,
        archive_key,
    )
    already_archived = (
        document_id is not None
    )

    if document_id is None:
        properties = upload_properties(
            client,
            protocol,
            archive_key,
            id_property_id,
        )

        try:
            upload_result = (
                client.archive_file(
                    str(document_path),
                    category_id=(
                        category_id
                    ),
                    properties=properties,
                )
            )
        except Exception as exc:
            raise (
                SafetyInspectionArchiveError(
                    "Das Prüfprotokoll konnte "
                    "nicht nach d.velop "
                    "übertragen werden."
                )
            ) from exc

        document_id = str(
            upload_result or ""
        ).strip()

        if (
            not document_id
            or document_id == "unknown"
        ):
            raise (
                SafetyInspectionArchiveError(
                    "d.velop hat nach der "
                    "Archivierung keine "
                    "Dokument-ID geliefert."
                )
            )

    protocol.dms_archive_status = (
        SafetyInspectionArchiveStatus
        .ARCHIVED.value
    )
    protocol.dms_document_id = (
        document_id
    )
    protocol.dms_archive_pdf_sha256 = (
        content_hash
    )
    protocol.dms_archived_at = utc_now()
    protocol.dms_archived_by = user
    protocol.dms_archive_error = None

    audit(
        (
            "safety_inspection."
            "protocol.archive"
        ),
        "safety_inspection_protocol",
        protocol.id,
        {
            "job_id": protocol.job_id,
            "version_number": (
                protocol.version_number
            ),
            "dms_document_id": (
                document_id
            ),
            "archive_key": archive_key,
            "pdf_sha256": content_hash,
            "existing_document": (
                already_archived
            ),
        },
    )

    return SafetyInspectionArchiveResult(
        document_id=document_id,
        already_archived=already_archived,
    )


def archive_safety_inspection_photo(
    photo: SafetyInspectionPhoto,
    user: User,
) -> SafetyInspectionArchiveResult:
    photo_path, content_hash = (
        verify_safety_inspection_photo(
            photo
        )
    )

    if (
        photo.dms_archive_status
        == SafetyInspectionArchiveStatus
        .ARCHIVED.value
        and photo.dms_document_id
    ):
        return SafetyInspectionArchiveResult(
            document_id=photo.dms_document_id,
            already_archived=True,
        )

    protocol = archived_protocol_for_photo(
        photo
    )
    category_id = required_archive_setting(
        category_definition(
            photo.result.job.object_level
        )
    )
    id_property_id = required_archive_setting(
        SettingKeys
        .DMS_SAFETY_PROTOCOL_ID_PROPERTY_ID
    )
    archive_key = (
        "datlaeuft-safety-photo-"
        f"{photo.public_id}"
    )

    photo.dms_archive_attempted_at = (
        utc_now()
    )

    client = create_archive_client()
    document_id = existing_document_id(
        client,
        category_id,
        id_property_id,
        archive_key,
    )
    already_archived = (
        document_id is not None
    )

    if document_id is None:
        values = photo_archive_values(
            photo,
            protocol,
            archive_key,
        )
        properties = upload_properties(
            client,
            protocol,
            archive_key,
            id_property_id,
            values=values,
        )

        try:
            upload_result = client.archive_file(
                str(photo_path),
                category_id=category_id,
                properties=properties,
            )
        except Exception as exc:
            raise SafetyInspectionArchiveError(
                "Das Prüffoto konnte nicht "
                "nach d.velop übertragen werden."
            ) from exc

        document_id = str(
            upload_result or ""
        ).strip()

        if (
            not document_id
            or document_id == "unknown"
        ):
            raise SafetyInspectionArchiveError(
                "d.velop hat nach der "
                "Archivierung des Prüffotos "
                "keine Dokument-ID geliefert."
            )

    photo.dms_archive_status = (
        SafetyInspectionArchiveStatus
        .ARCHIVED.value
    )
    photo.dms_document_id = document_id
    photo.dms_archived_at = utc_now()
    photo.dms_archive_error = None

    audit(
        "safety_inspection.photo.archive",
        "safety_inspection_photo",
        photo.id,
        {
            "job_id": (
                photo.result.job_id
            ),
            "dms_document_id": document_id,
            "archive_key": archive_key,
            "sha256": content_hash,
            "existing_document": (
                already_archived
            ),
            "user_id": user.id,
        },
    )

    return SafetyInspectionArchiveResult(
        document_id=document_id,
        already_archived=already_archived,
    )


def mark_safety_inspection_photo_archive_failure(
    photo: SafetyInspectionPhoto,
    error_message: str,
) -> None:
    photo.dms_archive_status = (
        SafetyInspectionArchiveStatus
        .FAILED.value
    )
    photo.dms_archive_attempted_at = (
        utc_now()
    )
    photo.dms_archive_error = (
        error_message[:4000]
    )

    audit(
        "safety_inspection.photo.archive_failed",
        "safety_inspection_photo",
        photo.id,
        {
            "job_id": photo.result.job_id,
            "error": error_message[:4000],
        },
    )


def mark_safety_inspection_archive_failure(
    protocol: SafetyInspectionProtocol,
    error_message: str,
) -> None:
    protocol.dms_archive_status = (
        SafetyInspectionArchiveStatus
        .FAILED.value
    )
    protocol.dms_archive_attempted_at = (
        utc_now()
    )
    protocol.dms_archive_error = (
        error_message[:4000]
    )

    audit(
        (
            "safety_inspection."
            "protocol.archive_failed"
        ),
        "safety_inspection_protocol",
        protocol.id,
        {
            "job_id": protocol.job_id,
            "version_number": (
                protocol.version_number
            ),
            "error": (
                error_message[:4000]
            ),
        },
    )


def process_pending_safety_inspection_archives(
    limit: int = 50,
) -> dict[str, int]:
    if not settings.get(
        SettingKeys
        .DMS_SAFETY_ARCHIVE_ENABLED
    ):
        return {
            "seen": 0,
            "archived": 0,
            "existing": 0,
            "failed": 0,
            "photo_seen": 0,
            "photo_archived": 0,
            "photo_existing": 0,
            "photo_failed": 0,
            "disabled": 1,
        }

    archive_statuses = {
        SafetyInspectionArchiveStatus
        .NOT_ARCHIVED.value,
        SafetyInspectionArchiveStatus
        .FAILED.value,
    }

    result = {
        "seen": 0,
        "archived": 0,
        "existing": 0,
        "failed": 0,
        "photo_seen": 0,
        "photo_archived": 0,
        "photo_existing": 0,
        "photo_failed": 0,
        "disabled": 0,
    }

    photo_ids = [
        photo_id
        for photo_id, in (
            db.session.query(
                SafetyInspectionPhoto.id
            )
            .join(
                SafetyInspectionResult,
                SafetyInspectionPhoto.result_id
                == SafetyInspectionResult.id,
            )
            .join(
                SafetyInspectionJob,
                SafetyInspectionResult.job_id
                == SafetyInspectionJob.id,
            )
            .join(
                SafetyInspectionGroup,
                SafetyInspectionJob.group_id
                == SafetyInspectionGroup.id,
            )
            .filter(
                SafetyInspectionPhoto.active
                .is_(True),
                SafetyInspectionPhoto.deleted_at
                .is_(None),
                SafetyInspectionPhoto
                .dms_archive_status
                .in_(archive_statuses),
                SafetyInspectionGroup
                .archive_original_photos
                .is_(True),
                SafetyInspectionJob.protocols.any(
                    SafetyInspectionProtocol
                    .dms_archive_status
                    == SafetyInspectionArchiveStatus
                    .ARCHIVED.value
                ),
            )
            .order_by(
                SafetyInspectionPhoto
                .created_at.asc(),
                SafetyInspectionPhoto.id.asc(),
            )
            .limit(max(limit, 0))
            .all()
        )
    ]

    result["photo_seen"] = len(
        photo_ids
    )

    for photo_id in photo_ids:
        photo = db.session.get(
            SafetyInspectionPhoto,
            photo_id,
        )

        if photo is None:
            continue

        try:
            archive_result = (
                archive_safety_inspection_photo(
                    photo,
                    photo.uploaded_by,
                )
            )
            db.session.commit()

            if archive_result.already_archived:
                result["photo_existing"] += 1
            else:
                result["photo_archived"] += 1

        except Exception as exc:
            db.session.rollback()

            photo = db.session.get(
                SafetyInspectionPhoto,
                photo_id,
            )

            if photo is not None:
                mark_safety_inspection_photo_archive_failure(
                    photo,
                    str(exc),
                )
                db.session.commit()

            current_app.logger.exception(
                "safety_inspection_photo_"
                "archive_failed photo_id=%s",
                photo_id,
            )
            result["photo_failed"] += 1

    return result
