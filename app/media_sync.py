from __future__ import annotations

from datetime import timedelta
from typing import Any, TYPE_CHECKING

from .extensions import db
from .models import (
    Commission,
    CommissionPhoto,
    CommissionSyncState,
    utc_now,
)
from .photo_service import (
    commission_photo_path,
    store_commission_photo_bytes,
)
from .settings import SettingKeys, settings
from .utils import ensure_utc, parse_datetime

if TYPE_CHECKING:
    from .wowiport import WowiportService


MEDIA_SOURCE = "openwowi_media"


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


def _external_datetime(
    value: Any,
):
    if value is None:
        return None

    try:
        return parse_datetime(value)
    except (TypeError, ValueError):
        return None


def commission_media_sync_due(
    commission: Commission,
) -> bool:
    if commission.media_synced_at is None:
        return True

    interval_minutes = max(
        settings.get(
            SettingKeys
            .WOWI_MEDIA_SYNC_INTERVAL_MINUTES
        ),
        5,
    )

    return (
            ensure_utc(commission.media_synced_at)
            <= utc_now()
            - timedelta(minutes=interval_minutes)
    )


def sync_commission_object_photos(
    commission: Commission,
    service: WowiportService,
    force: bool = False,
) -> dict[str, int]:
    result = {
        "sources": 0,
        "seen": 0,
        "created": 0,
        "updated": 0,
        "deactivated": 0,
        "errors": 0,
        "skipped": 0,
    }

    if (
        not force
        and not commission_media_sync_due(
            commission
        )
    ):
        result["skipped"] = 1
        return result

    sources = [
        (
            "building",
            "Building",
            commission.building_erp_id,
        ),
        (
            "use_unit",
            "UseUnit",
            commission.use_unit_id,
        ),
    ]

    error_messages: list[str] = []
    now = utc_now()

    for (
        context,
        entity_name,
        entity_id,
    ) in sources:
        obsolete_query = (
            CommissionPhoto.query
            .filter(
                CommissionPhoto.commission_id
                == commission.id,
                CommissionPhoto.source
                == MEDIA_SOURCE,
                CommissionPhoto.context
                == context,
                CommissionPhoto.active.is_(True),
            )
        )

        if entity_id is None:
            deactivated = obsolete_query.update(
                {
                    CommissionPhoto.active: False,
                    CommissionPhoto.deleted_at: now,
                },
                synchronize_session=False,
            )
            result["deactivated"] += deactivated
            continue

        deactivated = obsolete_query.filter(
            CommissionPhoto.external_entity_id
            != entity_id
        ).update(
            {
                CommissionPhoto.active: False,
                CommissionPhoto.deleted_at: now,
            },
            synchronize_session=False,
        )
        result["deactivated"] += deactivated

        try:
            rows = service.get_media_rows(
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
        seen_external_ids: set[str] = set()

        for row in rows:
            file_data = _value(
                row,
                "File",
                "file",
            )

            if not isinstance(file_data, dict):
                result["errors"] += 1
                error_messages.append(
                    f"{entity_name} {entity_id}: Dateiinformation fehlt"
                )
                continue

            file_guid = _text(
                _value(
                    file_data,
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

            external_id = (
                f"{entity_name}:{file_guid}"
            )
            seen_external_ids.add(external_id)
            result["seen"] += 1

            file_name = (
                _text(
                    _value(
                        file_data,
                        "FileName",
                        "file_name",
                    )
                )
                or f"{file_guid}.jpg"
            )

            picture_type = _value(
                row,
                "EstatePictureType",
                "estate_picture_type",
            )

            picture_type_erp_id = (
                _integer(
                    _value(
                        picture_type,
                        "Id",
                        "id",
                    )
                )
                if isinstance(picture_type, dict)
                else None
            )

            picture_type_name = (
                _text(
                    _value(
                        picture_type,
                        "Name",
                        "name",
                    )
                )
                if isinstance(picture_type, dict)
                else None
            )

            photo = (
                CommissionPhoto.query
                .filter(
                    CommissionPhoto.commission_id
                    == commission.id,
                    CommissionPhoto.source
                    == MEDIA_SOURCE,
                    CommissionPhoto.external_id
                    == external_id,
                )
                .one_or_none()
            )

            needs_content = (
                photo is None
                or not commission_photo_path(
                    photo.storage_name
                ).is_file()
                or not commission_photo_path(
                    photo.thumbnail_storage_name
                ).is_file()
            )

            if needs_content:
                try:
                    raw_data = (
                        service.download_media_content(
                            entity_name=entity_name,
                            file_guid=file_guid,
                        )
                    )

                    stored_photo = (
                        store_commission_photo_bytes(
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
                stored_photo = None

            if photo is None:
                photo = CommissionPhoto(
                    commission=commission,
                    context=context,
                    source=MEDIA_SOURCE,
                    external_id=external_id,
                    external_entity_name=entity_name,
                    external_entity_id=entity_id,
                    picture_type_erp_id=(
                        picture_type_erp_id
                    ),
                    picture_type_name=(
                        picture_type_name
                    ),
                    original_filename=(
                        stored_photo.original_filename
                    ),
                    storage_name=(
                        stored_photo.storage_name
                    ),
                    thumbnail_storage_name=(
                        stored_photo
                        .thumbnail_storage_name
                    ),
                    mime_type=(
                        stored_photo.mime_type
                    ),
                    size_bytes=(
                        stored_photo.size_bytes
                    ),
                    content_sha256=(
                        stored_photo.content_sha256
                    ),
                    caption=_text(
                        _value(
                            row,
                            "Remark",
                            "remark",
                        )
                    ),
                    active=True,
                    erp_sync_state=(
                        CommissionSyncState
                        .SYNCED.value
                    ),
                    erp_synced_at=now,
                    external_updated_at=(
                        _external_datetime(
                            _value(
                                file_data,
                                "CreationDate",
                                "creation_date",
                            )
                        )
                    ),
                )

                db.session.add(photo)
                result["created"] += 1
            else:
                photo.context = context
                photo.external_entity_name = (
                    entity_name
                )
                photo.external_entity_id = entity_id
                photo.picture_type_erp_id = (
                    picture_type_erp_id
                )
                photo.picture_type_name = (
                    picture_type_name
                )
                photo.caption = _text(
                    _value(
                        row,
                        "Remark",
                        "remark",
                    )
                )
                photo.active = True
                photo.deleted_at = None
                photo.erp_sync_state = (
                    CommissionSyncState.SYNCED.value
                )
                photo.erp_sync_message = None
                photo.erp_synced_at = now
                photo.external_updated_at = (
                    _external_datetime(
                        _value(
                            file_data,
                            "CreationDate",
                            "creation_date",
                        )
                    )
                )

                if stored_photo is not None:
                    photo.original_filename = (
                        stored_photo.original_filename
                    )
                    photo.storage_name = (
                        stored_photo.storage_name
                    )
                    photo.thumbnail_storage_name = (
                        stored_photo
                        .thumbnail_storage_name
                    )
                    photo.mime_type = (
                        stored_photo.mime_type
                    )
                    photo.size_bytes = (
                        stored_photo.size_bytes
                    )
                    photo.content_sha256 = (
                        stored_photo.content_sha256
                    )

                result["updated"] += 1

        stale_query = (
            CommissionPhoto.query
            .filter(
                CommissionPhoto.commission_id
                == commission.id,
                CommissionPhoto.source
                == MEDIA_SOURCE,
                CommissionPhoto.context
                == context,
                CommissionPhoto.external_entity_id
                == entity_id,
                CommissionPhoto.active.is_(True),
            )
        )

        if seen_external_ids:
            stale_query = stale_query.filter(
                CommissionPhoto.external_id.notin_(
                    seen_external_ids
                )
            )

        deactivated = stale_query.update(
            {
                CommissionPhoto.active: False,
                CommissionPhoto.deleted_at: now,
            },
            synchronize_session=False,
        )
        result["deactivated"] += deactivated

    if error_messages:
        commission.media_sync_error = (
            "\n".join(error_messages)
        )[:4000]
    else:
        commission.media_synced_at = now
        commission.media_sync_error = None

    return result