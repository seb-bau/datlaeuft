from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any, TYPE_CHECKING

from flask import current_app

from .extensions import db
from .models import (
    CommissionPhoto,
    CommissionSyncState,
    utc_now,
)
from .photo_service import (
    commission_photo_path,
    store_commission_photo_bytes,
)
from .utils import parse_datetime

if TYPE_CHECKING:
    from .wowiport import WowiportService


MEDIA_SOURCE = "openwowi_media"
OBJECT_MEDIA_ENTITIES = (
    ("building", "Building"),
    ("use_unit", "UseUnit"),
)


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


def _metadata_signature(
    row: dict[str, Any],
    entity_name: str,
    entity_id: int,
    media_id: int | None,
    file_guid: str,
) -> str:
    payload = {
        "entity_name": entity_name,
        "entity_id": entity_id,
        "media_id": media_id,
        "file_guid": file_guid,
        "position": _integer(
            _value(row, "Position", "position")
        ),
        "remark": _text(
            _value(row, "Remark", "remark")
        ),
        "marketing_release": _value(
            row,
            "MarketingRelease",
            "marketing_release",
        ),
        "is_for_license_agreements": _value(
            row,
            "IsForLicenseAgreements",
            "is_for_license_agreements",
        ),
        "file": _value(row, "File", "file"),
        "picture_type": _value(
            row,
            "EstatePictureType",
            "estate_picture_type",
        ),
    }

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    ).encode("utf-8")

    return sha256(encoded).hexdigest()


def _stored_files_exist(
    photo: CommissionPhoto,
) -> bool:
    return (
        commission_photo_path(
            photo.storage_name
        ).is_file()
        and commission_photo_path(
            photo.thumbnail_storage_name
        ).is_file()
    )


def _delete_storage_names(
    storage_names: set[str],
) -> int:
    deleted = 0

    for storage_name in storage_names:
        if not storage_name:
            continue

        try:
            path = commission_photo_path(
                storage_name
            )
            existed = path.is_file()
            path.unlink(missing_ok=True)

            if existed:
                deleted += 1
        except (OSError, ValueError):
            current_app.logger.exception(
                "object_photo_file_delete_failed "
                "storage_name=%s",
                storage_name,
            )

    return deleted


def sync_object_photos_from_wowiport(
    service: WowiportService | None = None,
) -> dict[str, int]:
    if service is None:
        from .wowiport import WowiportService

        service = WowiportService()

    result = {
        "entities": 0,
        "seen": 0,
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "downloaded": 0,
        "deleted": 0,
        "deleted_files": 0,
        "legacy_deleted": 0,
        "errors": 0,
    }
    completed_entities: set[str] = set()

    for context, entity_name in OBJECT_MEDIA_ENTITIES:
        current_app.logger.info(
            "object_photo_sync_entity_started "
            "entity_name=%s",
            entity_name,
        )

        try:
            rows = service.get_media_rows(
                entity_name=entity_name,
                entity_id=None,
            )
        except Exception:
            result["errors"] += 1
            current_app.logger.exception(
                "object_photo_sync_entity_failed "
                "entity_name=%s",
                entity_name,
            )
            continue

        completed_entities.add(entity_name)
        result["entities"] += 1

        existing_photos = (
            CommissionPhoto.query
            .filter(
                CommissionPhoto.commission_id.is_(
                    None
                ),
                CommissionPhoto.source
                == MEDIA_SOURCE,
                CommissionPhoto
                .external_entity_name
                == entity_name,
            )
            .all()
        )

        existing_by_external_id = {
            photo.external_id: photo
            for photo in existing_photos
            if photo.external_id
        }

        seen_external_ids: set[str] = set()
        cleanup_storage_names: set[str] = set()
        new_storage_names: set[str] = set()

        for row in rows:
            entity = _value(
                row,
                "Entity",
                "entity",
            )
            file_data = _value(
                row,
                "File",
                "file",
            )
            entity_id = _integer(
                _value(entity, "Id", "id")
            )
            media_id = _integer(
                _value(row, "Id", "id")
            )
            file_guid = _text(
                _value(
                    file_data,
                    "FileGuid",
                    "file_guid",
                )
            )

            if (
                entity_id is None
                or not isinstance(
                    file_data,
                    dict,
                )
                or not file_guid
            ):
                result["errors"] += 1
                current_app.logger.error(
                    "object_photo_sync_invalid_row "
                    "entity_name=%s media_id=%s "
                    "entity_id=%s",
                    entity_name,
                    media_id,
                    entity_id,
                )
                continue

            external_identity = (
                str(media_id)
                if media_id is not None
                else file_guid
            )
            external_id = (
                f"{entity_name}:"
                f"{external_identity}"
            )

            seen_external_ids.add(
                external_id
            )
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
                if isinstance(
                    picture_type,
                    dict,
                )
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
                if isinstance(
                    picture_type,
                    dict,
                )
                else None
            )

            metadata_signature = (
                _metadata_signature(
                    row=row,
                    entity_name=entity_name,
                    entity_id=entity_id,
                    media_id=media_id,
                    file_guid=file_guid,
                )
            )

            photo = existing_by_external_id.get(
                external_id
            )
            is_new = photo is None
            content_changed = (
                photo is None
                or photo.external_file_guid
                != file_guid
                or not _stored_files_exist(
                    photo
                )
            )
            metadata_changed = (
                photo is None
                or photo.metadata_signature
                != metadata_signature
            )
            stored_photo = None

            if content_changed:
                try:
                    raw_data = (
                        service
                        .download_media_content(
                            entity_name=(
                                entity_name
                            ),
                            file_guid=file_guid,
                        )
                    )

                    storage_directory = (
                        Path("objects")
                        / entity_name.casefold()
                        / str(entity_id)
                    )

                    stored_photo = (
                        store_commission_photo_bytes(
                            commission=None,
                            raw_data=raw_data,
                            original_filename=(
                                file_name
                            ),
                            storage_directory=(
                                storage_directory
                            ),
                        )
                    )

                    new_storage_names.update(
                        {
                            stored_photo
                            .storage_name,
                            stored_photo
                            .thumbnail_storage_name,
                        }
                    )
                    result["downloaded"] += 1
                except Exception as exc:
                    result["errors"] += 1

                    if photo is not None:
                        photo.erp_sync_message = (
                            str(exc)[:4000]
                        )

                    current_app.logger.exception(
                        "object_photo_download_failed "
                        "entity_name=%s "
                        "entity_id=%s "
                        "file_guid=%s",
                        entity_name,
                        entity_id,
                        file_guid,
                    )
                    continue

            if photo is None:
                photo = CommissionPhoto(
                    commission_id=None,
                    source=MEDIA_SOURCE,
                    context=context,
                    external_id=external_id,
                    external_document_id=(
                        f"openwowi-"
                        f"{entity_name}-"
                        f"{external_identity}"
                    )[:80],
                    external_file_guid=(
                        file_guid
                    ),
                    external_entity_name=(
                        entity_name
                    ),
                    external_entity_id=(
                        entity_id
                    ),
                    original_filename=(
                        stored_photo
                        .original_filename
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
                        stored_photo
                        .content_sha256
                    ),
                )
                db.session.add(photo)
                existing_by_external_id[
                    external_id
                ] = photo

            elif stored_photo is not None:
                cleanup_storage_names.update(
                    {
                        photo.storage_name,
                        photo
                        .thumbnail_storage_name,
                    }
                )

                photo.original_filename = (
                    stored_photo
                    .original_filename
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

            photo.context = context
            photo.source = MEDIA_SOURCE
            photo.external_file_guid = (
                file_guid
            )
            photo.external_entity_name = (
                entity_name
            )
            photo.external_entity_id = (
                entity_id
            )
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
            photo.metadata_signature = (
                metadata_signature
            )
            photo.external_updated_at = (
                _external_datetime(
                    _value(
                        file_data,
                        "CreationDate",
                        "creation_date",
                    )
                )
            )
            photo.active = True
            photo.deleted_at = None
            photo.erp_sync_state = (
                CommissionSyncState
                .SYNCED.value
            )
            photo.erp_sync_message = None
            photo.erp_synced_at = utc_now()

            if is_new:
                result["created"] += 1
            elif (
                content_changed
                or metadata_changed
            ):
                result["updated"] += 1
            else:
                result["unchanged"] += 1

        for photo in existing_photos:
            if (
                photo.external_id
                in seen_external_ids
            ):
                continue

            cleanup_storage_names.update(
                {
                    photo.storage_name,
                    photo
                    .thumbnail_storage_name,
                }
            )
            db.session.delete(photo)
            result["deleted"] += 1

        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            _delete_storage_names(
                new_storage_names
            )
            raise

        result["deleted_files"] += (
            _delete_storage_names(
                cleanup_storage_names
            )
        )

        current_app.logger.info(
            "object_photo_sync_entity_completed "
            "entity_name=%s result=%s",
            entity_name,
            result,
        )

    expected_entities = {
        entity_name
        for _, entity_name
        in OBJECT_MEDIA_ENTITIES
    }

    if (
        completed_entities
        == expected_entities
    ):
        legacy_photos = (
            CommissionPhoto.query
            .filter(
                CommissionPhoto
                .commission_id
                .isnot(None),
                CommissionPhoto.source
                == MEDIA_SOURCE,
            )
            .all()
        )
        legacy_storage_names: set[
            str
        ] = set()

        for photo in legacy_photos:
            legacy_storage_names.update(
                {
                    photo.storage_name,
                    photo
                    .thumbnail_storage_name,
                }
            )
            db.session.delete(photo)

        if legacy_photos:
            db.session.commit()
            result["legacy_deleted"] = len(
                legacy_photos
            )
            result["deleted_files"] += (
                _delete_storage_names(
                    legacy_storage_names
                )
            )

    current_app.logger.info(
        "object_photo_sync_completed "
        "result=%s",
        result,
    )

    return result