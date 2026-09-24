from __future__ import annotations

from datetime import timedelta
from typing import Any, TYPE_CHECKING
from uuid import uuid4

from .attachment_service import (
    commission_attachment_path,
    store_commission_attachment_bytes,
)
from .extensions import db
from .models import (
    Commission,
    CommissionAttachment,
    CommissionPhoto,
    CommissionSyncState,
    CommissionTicketLink,
    utc_now,
)
from .settings import SettingKeys, settings
from .utils import ensure_utc, parse_datetime

if TYPE_CHECKING:
    from .wowiport import WowiportService


DOCUMENT_SOURCE = "openwowi_document"


def _value(
    mapping: dict[str, Any] | None,
    *names: str,
) -> Any:
    if not isinstance(mapping, dict):
        return None

    normalized = {
        str(key)
        .replace("_", "")
        .casefold(): value
        for key, value in mapping.items()
    }

    for name in names:
        value = normalized.get(
            name.replace("_", "").casefold()
        )

        if value is not None:
            return value

    return None


def _integer(value: Any) -> int | None:
    if value is None:
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    if value is None:
        return None

    text_value = str(value).strip()

    return text_value or None


def _external_datetime(value: Any):
    if value is None:
        return None

    try:
        return parse_datetime(value)
    except (TypeError, ValueError):
        return None


def commission_document_sync_due(
    commission: Commission,
) -> bool:
    if commission.document_synced_at is None:
        return True

    interval_minutes = max(
        settings.get(
            SettingKeys
            .WOWI_DOCUMENT_SYNC_INTERVAL_MINUTES
        ),
        5,
    )

    return (
            ensure_utc(commission.document_synced_at)
            <= utc_now()
            - timedelta(minutes=interval_minutes)
    )


def _ticket_sources(
    commission: Commission,
    service: WowiportService,
) -> tuple[list[tuple[str, int]], bool]:
    local_ticket_ids = (
        CommissionTicketLink.query
        .with_entities(
            CommissionTicketLink.ticket_erp_id
        )
        .filter(
            CommissionTicketLink.commission_id
            == commission.id
        )
        .all()
    )

    sources = [
        (
            "Ticket",
            int(ticket_erp_id),
        )
        for ticket_erp_id, in local_ticket_ids
    ]

    if commission.erp_id is None:
        return list(dict.fromkeys(sources)), True

    try:
        rows = service.get_commission_ticket_rows(
            commission.erp_id
        )
    except Exception as exc:
        if sources:
            return list(dict.fromkeys(sources)), False

        raise RuntimeError(
            "Die Ticket-Zuordnungen konnten nicht "
            f"gelesen werden: {exc}"
        ) from exc

    for row in rows:
        ticket_id = _integer(
            _value(
                row,
                "Id",
                "id",
            )
        )

        if ticket_id is not None:
            sources.append(
                (
                    "Ticket",
                    ticket_id,
                )
            )

    return list(dict.fromkeys(sources)), True


def _deactivate_obsolete_sources(
    commission: Commission,
    current_sources: set[tuple[str, int]],
    tickets_known: bool,
    now,
) -> int:
    deactivated = 0

    attachments = (
        CommissionAttachment.query
        .filter(
            CommissionAttachment.commission_id
            == commission.id,
            CommissionAttachment.source
            == DOCUMENT_SOURCE,
            CommissionAttachment.active.is_(True),
        )
        .all()
    )

    for attachment in attachments:
        source_key = (
            attachment.source_entity_name,
            attachment.source_entity_id,
        )

        if (
            attachment.source_entity_name
            == "Ticket"
            and not tickets_known
        ):
            continue

        if source_key not in current_sources:
            attachment.active = False
            attachment.deleted_at = now
            deactivated += 1

    return deactivated


def sync_commission_documents(
    commission: Commission,
    service: WowiportService,
    force: bool = False,
) -> dict[str, int]:
    result = {
        "sources": 0,
        "tickets": 0,
        "seen": 0,
        "created": 0,
        "updated": 0,
        "deactivated": 0,
        "linked_photos": 0,
        "errors": 0,
        "skipped": 0,
    }

    if (
        not force
        and not commission_document_sync_due(
            commission
        )
    ):
        result["skipped"] = 1
        return result

    sources: list[tuple[str, int]] = []

    if commission.erp_id is not None:
        sources.append(
            (
                "Commission",
                commission.erp_id,
            )
        )

    if commission.crafts_process_erp_id is not None:
        sources.append(
            (
                "CraftsProcess",
                commission.crafts_process_erp_id,
            )
        )

    ticket_sources, tickets_known = (
        _ticket_sources(
            commission=commission,
            service=service,
        )
    )

    sources.extend(ticket_sources)
    sources = list(dict.fromkeys(sources))
    result["tickets"] = len(ticket_sources)

    current_sources = set(sources)
    now = utc_now()
    error_messages: list[str] = []

    result["deactivated"] += (
        _deactivate_obsolete_sources(
            commission=commission,
            current_sources=current_sources,
            tickets_known=tickets_known,
            now=now,
        )
    )

    if not tickets_known:
        result["errors"] += 1
        error_messages.append(
            "Die zugeordneten Tickets konnten nicht ermittelt werden."
        )

    for entity_name, entity_id in sources:
        try:
            rows = service.get_document_rows(
                entity_name=entity_name,
                entity_id=entity_id,
            )
        except Exception as exc:
            result["errors"] += 1
            error_messages.append(
                f"{entity_name} {entity_id}: {exc}"
            )
            continue

        result["sources"] += 1
        seen_file_guids: set[str] = set()

        for row in rows:
            file_guid = _text(
                _value(
                    row,
                    "FileGuid",
                    "file_guid",
                )
            )

            if not file_guid:
                result["errors"] += 1
                error_messages.append(
                    f"{entity_name} {entity_id}: FileGuid fehlt"
                )
                continue

            seen_file_guids.add(file_guid)
            result["seen"] += 1

            attachment = (
                CommissionAttachment.query
                .filter(
                    CommissionAttachment.commission_id
                    == commission.id,
                    CommissionAttachment.external_file_guid
                    == file_guid,
                )
                .one_or_none()
            )

            file_is_obsolete = bool(
                _value(
                    row,
                    "FileIsObsolete",
                    "file_is_obsolete",
                )
            )

            if file_is_obsolete:
                if attachment is not None:
                    attachment.active = False
                    attachment.deleted_at = now
                    result["deactivated"] += 1

                continue

            external_document_id = (
                    _text(
                        _value(
                            row,
                            "ExternalDocumentId",
                            "external_document_id",
                        )
                    )
                    or str(uuid4())
            )[:80]

            local_photo = None

            if entity_name == "Commission":
                local_photo = (
                    CommissionPhoto.query
                    .filter(
                        CommissionPhoto.commission_id
                        == commission.id,
                        CommissionPhoto.context == "order",
                        CommissionPhoto.external_document_id
                        == external_document_id,
                        CommissionPhoto.active.is_(True),
                    )
                    .one_or_none()
                )

            if local_photo is not None:
                result["linked_photos"] += 1

                if (
                        attachment is not None
                        and attachment.active
                ):
                    attachment.active = False
                    attachment.deleted_at = now
                    result["deactivated"] += 1

                continue

            file_name = (
                _text(
                    _value(
                        row,
                        "FileName",
                        "file_name",
                    )
                )
                or file_guid
            )

            file_type = _value(
                row,
                "FileType",
                "file_type",
            )
            privacy_category = _value(
                row,
                "DataPrivacyCategory",
                "data_privacy_category",
            )

            content_missing = (
                attachment is None
                or not commission_attachment_path(
                    attachment.storage_name
                ).is_file()
            )

            if content_missing:
                try:
                    raw_data = (
                        service
                        .download_document_content(
                            entity_name=entity_name,
                            file_guid=file_guid,
                        )
                    )

                    stored_attachment = (
                        store_commission_attachment_bytes(
                            commission=commission,
                            raw_data=raw_data,
                            original_filename=file_name,
                        )
                    )
                except Exception as exc:
                    result["errors"] += 1
                    error_messages.append(
                        f"{entity_name} {entity_id}, "
                        f"{file_name}: {exc}"
                    )
                    continue
            else:
                stored_attachment = None

            external_created_at = (
                _external_datetime(
                    _value(
                        row,
                        "CreationDate",
                        "creation_date",
                    )
                )
            )

            if attachment is None:
                attachment = CommissionAttachment(
                    commission=commission,
                    source=DOCUMENT_SOURCE,
                    source_entity_name=entity_name,
                    source_entity_id=entity_id,
                    external_id=_text(
                        _value(
                            row,
                            "Id",
                            "id",
                        )
                    ),
                    external_file_guid=file_guid,
                    external_document_id=(
                        external_document_id
                    ),
                    original_filename=(
                        stored_attachment
                        .original_filename
                    ),
                    storage_name=(
                        stored_attachment.storage_name
                    ),
                    thumbnail_storage_name=(
                        stored_attachment
                        .thumbnail_storage_name
                    ),
                    mime_type=(
                        stored_attachment.mime_type
                    ),
                    size_bytes=(
                        stored_attachment.size_bytes
                    ),
                    content_sha256=(
                        stored_attachment.content_sha256
                    ),
                    file_type_erp_id=(
                        _integer(
                            _value(
                                file_type,
                                "Id",
                                "id",
                            )
                        )
                        if isinstance(
                            file_type,
                            dict,
                        )
                        else None
                    ),
                    file_type_name=(
                        _text(
                            _value(
                                file_type,
                                "Name",
                                "name",
                            )
                        )
                        if isinstance(
                            file_type,
                            dict,
                        )
                        else None
                    ),
                    privacy_category_erp_id=(
                        _integer(
                            _value(
                                privacy_category,
                                "Id",
                                "id",
                            )
                        )
                        if isinstance(
                            privacy_category,
                            dict,
                        )
                        else None
                    ),
                    privacy_category_name=(
                        _text(
                            _value(
                                privacy_category,
                                "Name",
                                "name",
                            )
                        )
                        if isinstance(
                            privacy_category,
                            dict,
                        )
                        else None
                    ),
                    active=True,
                    erp_sync_state=(
                        CommissionSyncState
                        .SYNCED.value
                    ),
                    erp_synced_at=now,
                    external_created_at=(
                        external_created_at
                    ),
                    external_updated_at=(
                        external_created_at
                    ),
                )

                db.session.add(attachment)
                result["created"] += 1
            else:
                attachment.source_entity_name = (
                    entity_name
                )
                attachment.source_entity_id = entity_id
                attachment.external_id = _text(
                    _value(
                        row,
                        "Id",
                        "id",
                    )
                )
                attachment.file_type_erp_id = (
                    _integer(
                        _value(
                            file_type,
                            "Id",
                            "id",
                        )
                    )
                    if isinstance(file_type, dict)
                    else None
                )
                attachment.file_type_name = (
                    _text(
                        _value(
                            file_type,
                            "Name",
                            "name",
                        )
                    )
                    if isinstance(file_type, dict)
                    else None
                )
                attachment.privacy_category_erp_id = (
                    _integer(
                        _value(
                            privacy_category,
                            "Id",
                            "id",
                        )
                    )
                    if isinstance(
                        privacy_category,
                        dict,
                    )
                    else None
                )
                attachment.privacy_category_name = (
                    _text(
                        _value(
                            privacy_category,
                            "Name",
                            "name",
                        )
                    )
                    if isinstance(
                        privacy_category,
                        dict,
                    )
                    else None
                )
                attachment.active = True
                attachment.deleted_at = None
                attachment.erp_sync_state = (
                    CommissionSyncState.SYNCED.value
                )
                attachment.erp_sync_message = None
                attachment.erp_synced_at = now
                attachment.external_created_at = (
                    external_created_at
                )
                attachment.external_updated_at = (
                    external_created_at
                )

                if stored_attachment is not None:
                    attachment.original_filename = (
                        stored_attachment
                        .original_filename
                    )
                    attachment.storage_name = (
                        stored_attachment.storage_name
                    )
                    attachment.thumbnail_storage_name = (
                        stored_attachment
                        .thumbnail_storage_name
                    )
                    attachment.mime_type = (
                        stored_attachment.mime_type
                    )
                    attachment.size_bytes = (
                        stored_attachment.size_bytes
                    )
                    attachment.content_sha256 = (
                        stored_attachment
                        .content_sha256
                    )

                result["updated"] += 1

        stale_query = (
            CommissionAttachment.query
            .filter(
                CommissionAttachment.commission_id
                == commission.id,
                CommissionAttachment.source
                == DOCUMENT_SOURCE,
                CommissionAttachment.source_entity_name
                == entity_name,
                CommissionAttachment.source_entity_id
                == entity_id,
                CommissionAttachment.active.is_(True),
            )
        )

        if seen_file_guids:
            stale_query = stale_query.filter(
                CommissionAttachment
                .external_file_guid
                .notin_(seen_file_guids)
            )

        deactivated = stale_query.update(
            {
                CommissionAttachment.active: False,
                CommissionAttachment.deleted_at: now,
            },
            synchronize_session=False,
        )
        result["deactivated"] += deactivated

    if error_messages:
        commission.document_sync_error = (
            "\n".join(error_messages)
        )[:4000]
    else:
        commission.document_synced_at = now
        commission.document_sync_error = None

    return result