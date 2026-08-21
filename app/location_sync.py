from __future__ import annotations

from typing import Any

from flask import current_app

from .extensions import db
from .geocoding import geocode_address
from .models import WowiBuilding, WowiUseUnit, utc_now
from .utils import parse_datetime
from .wowiport import WowiportService


def sync_wowi_buildings() -> dict[str, int]:
    service = WowiportService()

    synced_at = utc_now()
    eligible_building_ids = (
        _eligible_building_ids(service)
    )
    seen_building_ids = set()

    result = {
        "seen": 0,
        "created": 0,
        "updated": 0,
        "geocoded": 0,
        "kept_existing_coordinates": 0,
        "without_coordinates": 0,
        "skipped": 0,
    }

    for building in service.client.get_building_lands(
        fetch_all=True
    ):
        result["seen"] += 1
        erp_id = _as_int(
            getattr(building, "id_", None)
        )

        if erp_id is None:
            result["skipped"] += 1
            continue

        if (
            _skip_building(building)
            or erp_id
            not in eligible_building_ids
        ):
            existing = (
                WowiBuilding.query.filter_by(
                    erp_id=erp_id
                ).one_or_none()
            )

            if existing is not None:
                existing.active = False
                existing.exit_date = (
                    parse_datetime(
                        getattr(
                            building,
                            "exit_date",
                            None,
                        )
                    )
                )
                existing.synced_at = synced_at

            result["skipped"] += 1
            continue

        item = WowiBuilding.query.filter_by(
            erp_id=erp_id
        ).one_or_none()

        if item is None:
            item = WowiBuilding(
                erp_id=erp_id
            )
            db.session.add(item)
            result["created"] += 1
        else:
            result["updated"] += 1

        seen_building_ids.add(erp_id)

        estate_address = getattr(building, "estate_address", None)
        address = _format_address(estate_address)

        item.id_num = getattr(building, "id_num", None)
        item.street = getattr(estate_address, "street_complete", None)
        item.zip_code = getattr(estate_address, "zip_", None)
        item.town = getattr(estate_address, "town", None)
        item.address = address
        item.exit_date = parse_datetime(getattr(building, "exit_date", None))
        item.active = item.exit_date is None
        item.synced_at = utc_now()

        if item.latitude is not None and item.longitude is not None:
            result["kept_existing_coordinates"] += 1
            continue

        if not address:
            result["without_coordinates"] += 1
            continue

        try:
            latitude, longitude = geocode_address(address)
        except Exception:
            current_app.logger.exception(
                "geocoding_failed building_erp_id=%s address=%s",
                erp_id,
                address,
            )
            latitude, longitude = None, None

        if latitude is not None and longitude is not None:
            item.latitude = latitude
            item.longitude = longitude
            item.coordinate_source = "geoapify"
            item.geocoded_at = utc_now()
            result["geocoded"] += 1
        else:
            result["without_coordinates"] += 1

    query = WowiBuilding.query.filter(
        WowiBuilding.active.is_(True)
    )

    if seen_building_ids:
        query = query.filter(
            WowiBuilding.erp_id.notin_(
                seen_building_ids
            )
        )

    for item in query.all():
        item.active = False
        item.synced_at = synced_at

    db.session.commit()
    return result


def sync_wowi_use_unit(use_unit_id: int) -> WowiUseUnit | None:
    service = WowiportService()
    use_unit = service.get_use_unit(use_unit_id)

    if use_unit is None:
        return None

    return upsert_wowi_use_unit(use_unit)


def upsert_wowi_use_unit(
    use_unit: Any,
) -> WowiUseUnit | None:
    erp_id = _as_int(
        getattr(use_unit, "id_", None)
    )

    if erp_id is None:
        return None

    synced_at = utc_now()
    exit_date = parse_datetime(
        getattr(
            use_unit,
            "exit_date",
            None,
        )
    )
    item = WowiUseUnit.query.filter_by(
        erp_id=erp_id
    ).one_or_none()

    if exit_date is not None:
        if item is not None:
            item.exit_date = exit_date
            item.active = False
            item.synced_at = synced_at

        return None

    building = getattr(
        use_unit,
        "building_land",
        None,
    )
    building_erp_id = _as_int(
        getattr(building, "id_", None)
    )
    building_item = (
        WowiBuilding.query.filter_by(
            erp_id=building_erp_id,
            active=True,
        ).one_or_none()
        if building_erp_id is not None
        else None
    )

    if building_item is None:
        if item is not None:
            item.active = False
            item.synced_at = synced_at

        return None

    if item is None:
        item = WowiUseUnit(
            erp_id=erp_id
        )
        db.session.add(item)

    item.id_num = getattr(
        use_unit,
        "id_num",
        None,
    )
    item.building_erp_id = building_erp_id
    item.building = building_item
    item.floor_name = getattr(
        getattr(use_unit, "floor", None),
        "name",
        None,
    )
    item.position_name = getattr(
        getattr(use_unit, "position", None),
        "name",
        None,
    )
    item.description_of_position = getattr(
        use_unit,
        "description_of_position",
        None,
    )
    item.address = _format_address(
        getattr(
            use_unit,
            "estate_address",
            None,
        )
    )
    item.exit_date = None
    item.active = True
    item.synced_at = synced_at

    return item


def _skip_building(building: Any) -> bool:
    if getattr(building, "exit_date", None):
        return True

    id_num = str(getattr(building, "id_num", "") or "")

    if not id_num.startswith("20") and not id_num.startswith("00"):
        return True

    try:
        building_number = int(id_num.split(".")[1])
    except (IndexError, ValueError):
        return False

    return building_number > 600


def _format_address(estate_address: Any) -> str | None:
    if estate_address is None:
        return None

    street = getattr(estate_address, "street_complete", None)
    zip_code = getattr(estate_address, "zip_", None)
    town = getattr(estate_address, "town", None)

    parts = [item for item in [street, zip_code, town] if item]
    return " ".join(parts) if parts else None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None

    try:
        return int(value)
    except ValueError:
        return None


def _eligible_building_ids(
    service: WowiportService,
) -> set[int]:
    rows = service.read_all_rows(
        "CommercialInventory/UseUnits",
        {
            "showNullValues": "true",
        },
    )

    if not rows:
        raise RuntimeError(
            "OpenWowi hat keine "
            "Nutzungseinheiten geliefert."
        )

    building_ids = set()

    for row in rows:
        exit_date = _dict_value(
            row,
            "ExitDate",
            "exitDate",
        )

        if exit_date not in (None, ""):
            continue

        building = _dict_object(
            row,
            "BuildingLand",
            "buildingLand",
            "Building",
            "building",
        )
        building_erp_id = _as_int(
            _dict_value(
                building,
                "Id",
                "id",
            )
            or _dict_value(
                row,
                "BuildingLandId",
                "buildingLandId",
            )
        )

        if building_erp_id is not None:
            building_ids.add(
                building_erp_id
            )

    return building_ids


def _dict_value(
    source: Any,
    *names: str,
) -> Any:
    if not isinstance(source, dict):
        return None

    for name in names:
        if name in source:
            return source[name]

    lowered = {
        str(key).lower(): value
        for key, value in source.items()
    }

    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]

    return None


def _dict_object(
    source: Any,
    *names: str,
) -> dict[str, Any]:
    value = _dict_value(
        source,
        *names,
    )
    return (
        value
        if isinstance(value, dict)
        else {}
    )
