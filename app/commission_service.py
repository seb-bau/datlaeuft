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
    TimeTaskTemplate,
    User,
    WowiCompanyCode,
    utc_now,
)
from .object_directory import (
    resolve_commission_assignment,
    resolve_commission_creator_official_id,
    resolve_commission_object,
)
from .external_craftsman_service import (
    resolve_external_craftsman,
)
from .settings import SettingKeys, settings
from .utils import parse_datetime
from .commission_priority import parse_commission_priority


def create_pending_commission(
    data: dict[str, Any],
    created_by_user: User,
    source: CommissionSource,
    allow_assignment_override: bool = False,
) -> Commission:
    assignment_type = str(
        data.get("assignment_type")
        or "internal"
    ).strip().casefold()

    if assignment_type not in {
        "internal",
        "external",
    }:
        raise ValueError(
            "Die Vergabeart ist ungültig."
        )
    is_high_priority = parse_commission_priority(
        data.get("priority", "normal")
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

    object_context = resolve_commission_object(
        data
    )
    position_data = data.get("positions")
    selected_craft_activity_id = optional_int(
        data.get("craft_activity_id"),
        "Gewerk",
    )

    if (
        source == CommissionSource.WEB
        and selected_craft_activity_id is None
    ):
        raise ValueError(
            "Gewerk muss angegeben werden."
        )

    if (
        not isinstance(position_data, list)
        or not position_data
    ):
        raise ValueError(
            "Mindestens eine Auftragsposition "
            "ist erforderlich."
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
            "Das Ausführungsende darf nicht "
            "vor dem Beginn liegen."
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
    use_unit = object_context.use_unit
    building = object_context.building
    agreement = (
        object_context.license_agreement
    )
    apartment_location = None

    if use_unit is not None:
        location_items = [
            use_unit.floor_name,
            use_unit.position_name,
            use_unit.description_of_position,
        ]
        apartment_location = (
            " · ".join(
                item
                for item in location_items
                if item
            )
            or None
        )

    company_code_erp_id = (
        object_context.economic_unit
        .company_code_erp_id
    )
    company_code_code = (
        object_context.economic_unit
        .company_code_code
    )

    requested_company_code_erp_id = (
        optional_int(
            data.get("company_code_erp_id"),
            "Buchwerk",
        )
        if allow_assignment_override
        else None
    )

    if requested_company_code_erp_id is not None:
        selected_company_code = (
            WowiCompanyCode.query
            .filter(
                WowiCompanyCode.erp_id
                == requested_company_code_erp_id,
                WowiCompanyCode.active.is_(True),
            )
            .one_or_none()
        )

        if selected_company_code is None:
            raise ValueError(
                "Das ausgewählte Buchwerk "
                "ist nicht verfügbar."
            )

        company_code_erp_id = (
            selected_company_code.erp_id
        )
        company_code_code = (
            selected_company_code.code
        )

    commission = Commission(
        company_code_erp_id=company_code_erp_id,
        company_code_code=company_code_code,
        economic_unit_erp_id=(
            object_context.economic_unit.erp_id
        ),
        economic_unit_idnum=(
            object_context.economic_unit.id_num
        ),
        crafts_process_type_code=(
            process_type_code
        ),
        crafts_process_status_code=(
            process_status_code
        ),
        sales_tax_code=sales_tax_code,
        public_id=public_id,
        source=source.value,
        assignment_type=assignment_type,
        is_high_priority=is_high_priority,
        priority_is_manual="priority" in data,
        erp_sync_state=(
            CommissionSyncState.PENDING.value
        ),
        local_status=(
            CommissionLocalStatus.RECORDED.value
        ),
        erp_external_identification_number=(
            public_id
        ),
        commission_type_erp_id=(
            commission_type_id
        ),
        crafts_process_erp_id=optional_int(
            data.get("crafts_process_id"),
            "Vorgang",
        ),
        creditor_erp_id=optional_int(
            data.get("creditor_id"),
            "Kreditor",
        ),
        use_unit_id=(
            use_unit.erp_id
            if use_unit
            else None
        ),
        use_unit_idnum=(
            use_unit.id_num
            if use_unit
            else None
        ),
        building_erp_id=(
            building.erp_id
            if building
            else None
        ),
        building_idnum=(
            building.id_num
            if building
            else None
        ),
        license_agreement_id=(
            agreement.erp_id
            if agreement
            else None
        ),
        license_agreement_idnum=(
            agreement.id_num
            if agreement
            else None
        ),
        apartment_address=(
            use_unit.address
            if use_unit is not None
            else (
                building.address
                if building is not None
                else None
            )
        ),
        apartment_location=apartment_location,
        tenant_name=object_context.tenant_name,
        user=None,
        craftsman_erp_id=None,
        created_by=created_by_user,
        title=clean_text(
            data.get("title")
        ),
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

    service_positions = []

    for position_index, item in enumerate(
        position_data
    ):
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
            (
                "Leistungsposition "
                f"{position_index + 1}"
            ),
        )

        service_position = (
            TimeTaskTemplate.query.filter(
                TimeTaskTemplate.active.is_(
                    True
                ),
                TimeTaskTemplate
                .erp_service_catalogue_id
                == service_catalog_erp_id,
            ).one_or_none()
        )

        if service_position is None:
            raise ValueError(
                "Die Leistungsposition "
                f"{position_index + 1} "
                "ist nicht verfügbar."
            )

        craft_activity = (
            service_position.craft_activity
        )

        if (
                craft_activity is None
                or not craft_activity.active
                or not craft_activity.app_enabled
                or craft_activity.erp_id is None
        ):
            raise ValueError(
                "Der Leistungsposition "
                f"{position_index + 1} "
                "ist kein freigegebenes Gewerk "
                "zugeordnet."
            )

        service_positions.append(
            service_position
        )
        commission.positions.append(
            CommissionPosition(
                position_index=position_index,
                code=(
                    clean_text(item.get("code"))
                    or service_position.code
                ),
                commission_text=(
                    commission_text
                ),
                internal_description=clean_text(
                    item.get(
                        "internal_description"
                    )
                ),
                description=clean_text(
                    item.get("description")
                ),
                units=units,
                service_catalog_erp_id=(
                    service_position
                    .erp_service_catalogue_id
                ),
                craft_activity_erp_id=(
                    craft_activity.erp_id
                ),
            )
        )
    for service_position in service_positions:
        mapped_type_ids = integer_set(
            service_position
            .commission_type_erp_ids
            or []
        )

        if (
            mapped_type_ids
            and commission_type_id
            not in mapped_type_ids
        ):
            raise ValueError(
                "Die ausgewählte Auftragsart "
                "ist für mindestens eine "
                "Leistungsposition nicht zulässig."
            )

    craft_activity_ids = {
        service_position.craft_activity_id
        for service_position in service_positions
    }

    if len(craft_activity_ids) != 1:
        raise ValueError(
            "Alle Positionen eines Auftrags müssen "
            "demselben Gewerk angehören."
        )

    craft_activity_id = next(
        iter(craft_activity_ids)
    )
    craft_activity = (
        service_positions[0].craft_activity
    )

    if craft_activity is None:
        raise ValueError(
            "Der Leistungsposition ist kein "
            "Gewerk zugeordnet."
        )

    if (
        selected_craft_activity_id is not None
        and selected_craft_activity_id
        != craft_activity_id
    ):
        raise ValueError(
            "Mindestens eine Leistungsposition "
            "gehört nicht zum ausgewählten Gewerk."
        )

    requested_user_id = (
        optional_int(
            data.get("craftsman_user_id"),
            "Handwerker",
        )
        if (
            allow_assignment_override
            and assignment_type == "internal"
        )
        else None
    )

    if assignment_type == "external":
        requested_external_craftsman_id = (
            optional_int(
                data.get("external_craftsman_id"),
                "Externer Handwerker",
            )
            if allow_assignment_override
            else None
        )
        external_craftsman = resolve_external_craftsman(
            craft_activity,
            requested_external_craftsman_id,
        )

        commission.user = None
        commission.craftsman_erp_id = external_craftsman.craftsman_erp_id
        commission.department_erp_id = None
    else:
        if requested_user_id is not None:
            assigned_user = (
                User.query
                .filter(
                    User.id == requested_user_id,
                    User.is_active_flag.is_(True),
                    User.craftsman_erp_id.isnot(None),
                )
                .one_or_none()
            )

            if assigned_user is None:
                raise ValueError(
                    "Der ausgewählte Handwerker ist nicht verfügbar."
                )

            department_mapping = craft_activity.department_mapping
            department = (
                department_mapping.department
                if department_mapping is not None
                else None
            )
            if department is not None and not department.active:
                department = None
        else:
            assignment = resolve_commission_assignment(
                service_positions,
                object_context,
            )
            assigned_user = assignment.user
            department = assignment.department

            if assigned_user is None:
                raise ValueError(
                    "Es konnte kein interner Handwerker ermittelt werden."
                )

        commission.user = assigned_user
        commission.craftsman_erp_id = assigned_user.craftsman_erp_id
        commission.department_erp_id = (
            department.erp_id if department is not None else None
        )

    commission.responsible_official_repair_erp_id = (
        resolve_commission_creator_official_id(created_by_user)
    )

    if len(commission.positions) > 1:
        commission.title = "Mehrere Positionen"
        commission.description = (
            "Mehrere Positionen"
        )
    else:
        first_position = (
            commission.positions[0]
        )
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
        operation_type=(
            ErpOperationType.CREATE_COMMISSION
        ),
        idempotency_key=(
            f"commission:create:"
            f"{commission.public_id}"
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
