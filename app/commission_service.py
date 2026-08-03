from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from .erp_sync import enqueue_erp_operation
from .extensions import db
from .models import (
    Commission,
    CommissionLocalStatus,
    CommissionPosition,
    CommissionSource,
    CommissionSyncState,
    ErpOperationType,
    User,
    WowiBuilding,
    WowiUseUnit,
    utc_now,
    TimeTaskTemplate,
    WowiCompanyCode,
)
from .settings import SettingKeys, settings
from .utils import parse_datetime


def create_pending_commission(
    data: dict[str, Any],
    assigned_user: User,
    created_by_user: User,
    source: CommissionSource,
) -> Commission:
    if assigned_user.craftsman_erp_id is None:
        raise ValueError(
            "Dem ausgewählten Handwerker fehlt "
            "die Wowiport-Zuordnung."
        )

    commission_type_id = required_int(
        data,
        "commission_type_id",
        "Auftragsart",
    )
    allowed_type_ids = integer_set(
        settings.get(
            SettingKeys.WOWI_ALLOWED_COMMISSION_TYPE_IDS
        )
    )

    if commission_type_id not in allowed_type_ids:
        raise ValueError(
            "Diese Auftragsart ist nicht freigegeben."
        )

    use_unit_idnum = clean_id_num(
        data.get("use_unit_idnum")
    )
    building_idnum = clean_id_num(
        data.get("building_idnum")
    )
    license_agreement_idnum = clean_id_num(
        data.get("license_agreement_idnum")
    )

    if not any(
        [
            use_unit_idnum,
            building_idnum,
            license_agreement_idnum,
        ]
    ):
        raise ValueError(
            "Nutzungseinheit, Gebäude oder Mietvertrag "
            "muss angegeben werden."
        )

    use_unit = resolve_local_reference(
        WowiUseUnit,
        use_unit_idnum,
        "Nutzungseinheit",
    )
    building = resolve_local_reference(
        WowiBuilding,
        building_idnum,
        "Gebäude",
    )

    use_unit_erp_id = (
        use_unit.erp_id
        if use_unit is not None
        else None
    )
    building_erp_id = (
        building.erp_id
        if building is not None
        else None
    )

    if use_unit is not None:
        if (
            building_erp_id is not None
            and use_unit.building_erp_id is not None
            and building_erp_id
            != use_unit.building_erp_id
        ):
            raise ValueError(
                "Die Nutzungseinheit gehört nicht "
                "zum angegebenen Gebäude."
            )

        if building_erp_id is None:
            building_erp_id = use_unit.building_erp_id

        if (
            not building_idnum
            and use_unit.building is not None
        ):
            building_idnum = use_unit.building.id_num

    position_data = data.get("positions")

    if not isinstance(position_data, list) or not position_data:
        raise ValueError(
            "Mindestens eine Auftragsposition ist erforderlich."
        )

    execution_from = parse_datetime(
        data.get("execution_from")
    )
    execution_to = parse_datetime(
        data.get("execution_to")
    )

    if (
        execution_from is not None
        and execution_to is not None
        and execution_from > execution_to
    ):
        raise ValueError(
            "Das Ausführungsende darf nicht vor dem Beginn liegen."
        )

    company_code_code = clean_text(
        data.get("company_code_code")
    )

    if not company_code_code:
        raise ValueError(
            "Das Buchwerk muss ausgewählt werden."
        )

    company_code = WowiCompanyCode.query.filter(
        WowiCompanyCode.active.is_(True),
        WowiCompanyCode.code == company_code_code,
    ).one_or_none()

    if company_code is None:
        raise ValueError(
            "Das ausgewählte Buchwerk ist nicht verfügbar. "
            "Bitte die Wowiport-Buchwerke synchronisieren."
        )

    process_type_code = required_setting_text(
        SettingKeys.WOWI_DEFAULT_CRAFTS_PROCESS_TYPE_CODE,
        "Standard-Vorgangsart",
    )
    process_status_code = required_setting_text(
        SettingKeys.WOWI_INITIAL_CRAFTS_PROCESS_STATUS_CODE,
        "Anfangsstatus des Vorgangs",
    )
    sales_tax_code = required_setting_text(
        SettingKeys.WOWI_INTERNAL_SALES_TAX_CODE,
        "Umsatzsteuercode",
    )

    public_id = normalize_public_id(
        data.get("client_request_id")
    )

    commission = Commission(
        company_code_erp_id=company_code.erp_id,
        company_code_code=company_code.code,
        crafts_process_type_code=process_type_code,
        crafts_process_status_code=process_status_code,
        sales_tax_code=sales_tax_code,
        public_id=public_id,
        source=source.value,
        erp_sync_state=CommissionSyncState.PENDING.value,
        local_status=CommissionLocalStatus.RECORDED.value,
        erp_external_identification_number=public_id,
        commission_type_erp_id=commission_type_id,
        crafts_process_erp_id=optional_int(
            data.get("crafts_process_id"),
            "Vorgang",
        ),
        creditor_erp_id=optional_int(
            data.get("creditor_id"),
            "Kreditor",
        ),
        department_erp_id=optional_int(
            data.get("department_id"),
            "Abteilung",
        ),
        responsible_official_repair_erp_id=optional_int(
            data.get("responsible_official_repair_id"),
            "Sachbearbeiter",
        ),
        use_unit_id=use_unit_erp_id,
        use_unit_idnum=use_unit_idnum,
        building_erp_id=building_erp_id,
        building_idnum=building_idnum,
        license_agreement_id=None,
        license_agreement_idnum=license_agreement_idnum,
        user=assigned_user,
        craftsman_erp_id=assigned_user.craftsman_erp_id,
        created_by=created_by_user,
        title=clean_text(data.get("title")),
        damage_location=clean_text(
            data.get("damage_location")
        ),
        contact_person=clean_text(
            data.get("contact_person")
        ),
        recording_date=utc_now(),
        execution_from=execution_from,
        execution_to=execution_to,
        synced_at=utc_now(),
    )

    db.session.add(commission)

    for position_index, item in enumerate(position_data):
        if not isinstance(item, dict):
            raise ValueError(
                "Eine Auftragsposition ist ungültig."
            )

        commission_text = clean_text(
            item.get("commission_text")
            or item.get("description")
        )

        if not commission_text:
            raise ValueError(
                f"Position {position_index + 1} "
                "benötigt einen Auftragstext."
            )

        units = optional_float(
            item.get("units"),
            "Menge",
        )

        if units is None:
            units = 1.0

        if units <= 0:
            raise ValueError(
                f"Position {position_index + 1} "
                "benötigt eine positive Menge."
            )

        service_catalog_erp_id = required_int(
            item,
            "service_catalog_id",
            f"Leistungsposition {position_index + 1}",
        )

        service_position = TimeTaskTemplate.query.filter(
            TimeTaskTemplate.active.is_(True),
            TimeTaskTemplate.erp_service_catalogue_id
            == service_catalog_erp_id,
        ).one_or_none()

        if service_position is None:
            raise ValueError(
                f"Die Leistungsposition {position_index + 1} "
                "ist nicht verfügbar."
            )

        craft_activity = service_position.craft_activity

        if (
            craft_activity is None
            or not craft_activity.active
            or craft_activity.erp_id is None
        ):
            raise ValueError(
                f"Der Leistungsposition {position_index + 1} "
                "ist kein gültiges Gewerk zugeordnet."
            )

        commission.positions.append(
            CommissionPosition(
                position_index=position_index,
                code=(
                        clean_text(item.get("code"))
                        or service_position.code
                ),
                commission_text=commission_text,
                internal_description=clean_text(
                    item.get("internal_description")
                ),
                description=clean_text(
                    item.get("description")
                ),
                units=units,
                service_catalog_erp_id=(
                    service_position.erp_service_catalogue_id
                ),
                craft_activity_erp_id=(
                    craft_activity.erp_id
                ),
            )
        )

    if len(commission.positions) > 1:
        commission.title = "Mehrere Positionen"
        commission.description = "Mehrere Positionen"
    else:
        first_position = commission.positions[0]
        position_text = (
            first_position.internal_description
            or first_position.commission_text
            or first_position.code
        )

        if not commission.title:
            commission.title = position_text

        commission.description = position_text

    db.session.flush()

    enqueue_erp_operation(
        commission=commission,
        operation_type=ErpOperationType.CREATE_COMMISSION,
        idempotency_key=(
            f"commission:create:{commission.public_id}"
        ),
    )

    return commission


def integer_set(value: Any) -> set[int]:
    if not isinstance(value, (list, tuple, set)):
        return set()

    result = set()

    for item in value:
        try:
            result.add(int(item))
        except (TypeError, ValueError):
            continue

    return result


def required_int(
    data: dict[str, Any],
    key: str,
    label: str,
) -> int:
    value = optional_int(data.get(key), label)

    if value is None:
        raise ValueError(
            f"{label} muss angegeben werden."
        )

    return value


def optional_int(
    value: Any,
    label: str,
) -> int | None:
    if value is None or value == "":
        return None

    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{label} ist ungültig."
        ) from exc


def optional_float(
    value: Any,
    label: str,
) -> float | None:
    if value is None or value == "":
        return None

    try:
        return float(
            str(value).replace(",", ".")
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{label} ist ungültig."
        ) from exc


def clean_text(value: Any) -> str | None:
    if value is None:
        return None

    text = str(value).strip()
    return text or None


def clean_id_num(value: Any) -> str | None:
    if value is None:
        return None

    normalized_value = str(value).strip()
    return normalized_value or None


def resolve_local_reference(
    model: Any,
    id_num: str | None,
    label: str,
) -> Any:
    if id_num is None:
        return None

    matches = model.query.filter(
        model.id_num == id_num
    ).limit(2).all()

    if len(matches) > 1:
        raise ValueError(
            f"{label} mit der Nummer {id_num} "
            "ist nicht eindeutig."
        )

    return matches[0] if matches else None


def required_setting_text(
    definition: Any,
    label: str,
) -> str:
    value = clean_text(settings.get(definition))

    if not value:
        raise ValueError(
            f"{label} ist nicht konfiguriert."
        )

    return value


def normalize_public_id(value: Any) -> str:
    if value is None or not str(value).strip():
        return str(uuid4())

    try:
        return str(UUID(str(value).strip()))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(
            "Die Client-Auftragskennung ist ungültig."
        ) from exc
