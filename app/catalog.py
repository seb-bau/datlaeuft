from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import false, or_

from .extensions import db
from .models import CraftActivity, TimeTaskTemplate, User, utc_now
from .wowiport import WowiportService


@dataclass
class CatalogSyncResult:
    craft_activities_created: int = 0
    craft_activities_updated: int = 0
    service_positions_created: int = 0
    service_positions_updated: int = 0
    service_positions_skipped_unassigned_craft_activity: int = 0
    service_positions_without_craft_activity: int = 0
    service_positions_deactivated: int = 0


def sync_catalogs_from_wowiport() -> CatalogSyncResult:
    service = WowiportService()
    result = CatalogSyncResult()

    craft_result = sync_craft_activities_from_wowiport(service)
    result.craft_activities_created = craft_result.craft_activities_created
    result.craft_activities_updated = craft_result.craft_activities_updated

    db.session.flush()

    service_result = sync_service_positions_from_wowiport(service)
    result.service_positions_created = service_result.service_positions_created
    result.service_positions_updated = service_result.service_positions_updated
    result.service_positions_without_craft_activity = service_result.service_positions_without_craft_activity

    result.service_positions_skipped_unassigned_craft_activity = (
        service_result
        .service_positions_skipped_unassigned_craft_activity
    )
    result.service_positions_deactivated = (
        service_result.service_positions_deactivated
    )

    return result


def sync_craft_activities_from_wowiport(service: WowiportService | None = None) -> CatalogSyncResult:
    service = service or WowiportService()
    result = CatalogSyncResult()

    for entry in service.get_craft_activities():
        erp_id = _as_int(_attr(entry, "id_"))
        if erp_id is None:
            continue

        craft_activity = CraftActivity.query.filter_by(erp_id=erp_id).one_or_none()
        if craft_activity is None:
            craft_activity = CraftActivity(erp_id=erp_id)
            db.session.add(craft_activity)
            result.craft_activities_created += 1
        else:
            result.craft_activities_updated += 1

        craft_activity.code = _first_text(_attr(entry, "code"), _attr(entry, "id_num"), str(erp_id))
        craft_activity.name = _first_text(_attr(entry, "name"), _attr(entry, "description"), craft_activity.code, str(erp_id)) or str(erp_id)
        craft_activity.active = not _as_bool(_first_text(_attr(entry, "inactive"), _attr(entry, "invalid"), _attr(entry, "is_inactive"), _attr(entry, "is_invalid")))
        craft_activity.synced_at = utc_now()

    return result


def sync_service_positions_from_wowiport(service: WowiportService | None = None) -> CatalogSyncResult:
    service = service or WowiportService()
    result = CatalogSyncResult()

    craft_activities_by_erp_id = {
        item.erp_id: item
        for item in CraftActivity.query.filter(
            CraftActivity.active.is_(True),
            CraftActivity.erp_id.isnot(None),
        ).all()
    }

    existing_positions_by_erp_id = {
        item.erp_service_catalogue_id: item
        for item in TimeTaskTemplate.query
        .filter(TimeTaskTemplate.erp_service_catalogue_id.isnot(None))
        .all()
    }

    used_codes = {
        row[0]
        for row in db.session.query(TimeTaskTemplate.code).all()
        if row[0]
    }

    synced_position_erp_ids = set()

    for entry in service.get_service_catalogues():
        erp_id = _as_int(_attr(entry, "id_"))
        if erp_id is None:
            continue

        craft_activity_erp_id = _extract_craft_activity_erp_id(entry)

        if craft_activity_erp_id is None:
            result.service_positions_without_craft_activity += 1
            _deactivate_existing_position(existing_positions_by_erp_id.get(erp_id), result)
            continue

        craft_activity = craft_activities_by_erp_id.get(
            craft_activity_erp_id
        )

        if craft_activity is None:
            result.service_positions_skipped_unassigned_craft_activity += 1
            _deactivate_existing_position(existing_positions_by_erp_id.get(erp_id), result)
            continue

        service_position = existing_positions_by_erp_id.get(erp_id)

        if service_position is not None and service_position.code:
            used_codes.discard(service_position.code)

        base_code = _extract_service_position_code(entry, erp_id)
        code = _make_unique_code(base_code, erp_id, used_codes)
        title = _extract_service_position_title(entry, code)

        if service_position is None:
            service_position = TimeTaskTemplate(
                code=code,
                title=title,
                suggested_minutes=0,
                erp_service_catalogue_id=erp_id,
                source="wowiport",
                active=True,
                synced_at=utc_now(),
            )
            db.session.add(service_position)
            existing_positions_by_erp_id[erp_id] = service_position
            result.service_positions_created += 1
        else:
            result.service_positions_updated += 1

        service_position.code = code
        service_position.title = title
        service_position.erp_idnum = _extract_service_position_erp_idnum(entry)
        service_position.craft_activity = craft_activity
        service_position.source = "wowiport"
        service_position.active = not _entry_is_inactive(entry)
        service_position.synced_at = utc_now()

        used_codes.add(code)
        synced_position_erp_ids.add(erp_id)

    for erp_id, service_position in existing_positions_by_erp_id.items():
        if (
                service_position.source == "wowiport"
                and erp_id not in synced_position_erp_ids
        ):
            _deactivate_existing_position(
                service_position,
                result,
            )

    return result


def service_position_query_for_user(user: User):
    query = TimeTaskTemplate.query.filter(TimeTaskTemplate.active.is_(True))

    if user.is_admin or user.is_foreman:
        return query

    craft_activity_ids = [item.id for item in user.craft_activities if item.active]

    if not craft_activity_ids:
        return query.filter(false())

    return query.filter(TimeTaskTemplate.craft_activity_id.in_(craft_activity_ids))


def get_user_visible_time_task(user: User, task_template_id: int) -> TimeTaskTemplate | None:
    return service_position_query_for_user(user).filter(TimeTaskTemplate.id == task_template_id).one_or_none()


def _extract_service_position_code(entry: Any, erp_id: int) -> str:
    return _first_text(
        _attr(entry, "id_num"),
        _attr(entry, "idnum"),
        _attr(entry, "code"),
        _attr(entry, "external_identification_number"),
        f"wowi-{erp_id}",
    ) or f"wowi-{erp_id}"


def _extract_service_position_erp_idnum(entry: Any) -> str | None:
    return _first_text(
        _attr(entry, "id_num"),
        _attr(entry, "idnum"),
    )


def _extract_service_position_title(entry: Any, fallback_code: str) -> str:
    return _first_text(
        _attr(entry, "name"),
        _attr(entry, "title"),
        _attr(entry, "short_text"),
        _attr(entry, "short_description"),
        _attr(entry, "description"),
        _attr(entry, "long_text"),
        _attr(entry, "long_description"),
        _attr(entry, "text"),
        fallback_code,
    ) or fallback_code


def _extract_suggested_minutes(entry: Any, current_value: int | None) -> int:
    value = _as_int(_first_text(
        _attr(entry, "suggested_minutes"),
        _attr(entry, "duration_minutes"),
        _attr(entry, "default_duration_minutes"),
        _attr(entry, "time_value_minutes"),
    ))

    if value is not None:
        return value

    return current_value or 0


def _entry_is_inactive(entry: Any) -> bool:
    return _as_bool(_first_text(
        _attr(entry, "inactive"),
        _attr(entry, "invalid"),
        _attr(entry, "is_inactive"),
        _attr(entry, "is_invalid"),
    ))


def _make_unique_code(base_code: str, erp_id: int, used_codes: set[str]) -> str:
    code = base_code.strip() or f"wowi-{erp_id}"

    if code not in used_codes:
        return code

    fallback_code = f"{code}-{erp_id}"

    if fallback_code not in used_codes:
        return fallback_code

    counter = 2

    while f"{fallback_code}-{counter}" in used_codes:
        counter += 1

    return f"{fallback_code}-{counter}"


def _extract_craft_activity_erp_id(entry: Any) -> int | None:
    direct_value = _first_text(
        _attr(entry, "craft_activity_id"),
        _attr(entry, "commissioning_craft_activity_id"),
        _attr(entry, "crafts_activity_id"),
        _attr(entry, "craft_activity_id_"),
    )

    if direct_value is not None:
        return _as_int(direct_value)

    for name in [
        "craft_activity",
        "commissioning_craft_activity",
        "crafts_activity",
    ]:
        related = _attr(entry, name)
        related_id = _as_int(_attr(related, "id_"))
        if related_id is not None:
            return related_id

    related_items = _attr(entry, "craft_activities") or _attr(entry, "commissioning_craft_activities")

    if related_items:
        for related in related_items:
            related_id = _as_int(_attr(related, "id_"))
            if related_id is not None:
                return related_id

    return None


def _attr(source: Any, name: str, default: Any = None) -> Any:
    if source is None:
        return default

    return getattr(source, name, default)


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value

    if value is None:
        return False

    return str(value).strip().lower() in {"1", "true", "yes", "ja", "on"}


def _first_text(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue

        text = str(value).strip()

        if text:
            return text

    return None


def _deactivate_existing_position(service_position: TimeTaskTemplate | None, result: CatalogSyncResult) -> None:
    if service_position is None:
        return

    if service_position.source != "wowiport":
        return

    if service_position.active:
        service_position.active = False
        service_position.synced_at = utc_now()
        result.service_positions_deactivated += 1
