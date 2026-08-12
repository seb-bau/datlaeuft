from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable

from sqlalchemy import or_

from .extensions import db
from .models import (
    CraftActivity,
    CraftActivityDepartmentMapping,
    TimeTaskTemplate,
    User,
    WowiBuilding,
    WowiCompanyCode,
    WowiContractor,
    WowiDepartment,
    WowiEconomicUnit,
    WowiEconomicUnitJurisdiction,
    WowiLicenseAgreement,
    WowiUseUnit,
    utc_now,
)
from .wowiport import WowiportService


@dataclass(frozen=True)
class CommissionObjectContext:
    economic_unit: WowiEconomicUnit
    building: WowiBuilding | None
    use_unit: WowiUseUnit | None
    license_agreement: WowiLicenseAgreement | None
    tenant_name: str | None


@dataclass(frozen=True)
class CommissionAssignment:
    user: User
    craft_activity: CraftActivity
    department: WowiDepartment
    jurisdiction: WowiEconomicUnitJurisdiction
    responsible_official_erp_id: int


def sync_object_directory() -> dict[str, int]:
    service = WowiportService()
    synced_at = utc_now()
    active_on = date.today().isoformat()
    result = {
        "departments": 0,
        "economic_units": 0,
        "jurisdictions": 0,
        "buildings": 0,
        "use_units": 0,
        "license_agreements": 0,
        "contractors": 0,
    }

    economic_rows = service.read_all_rows(
        "CommercialInventory/EconomicUnits",
        {
            "includeCompanyCode": "true",
            "showNullValues": "true",
        },
    )
    _require_rows(
        economic_rows,
        "Wirtschaftseinheiten",
    )

    seen_economic_ids = set()

    for row in economic_rows:
        erp_id = _as_int(
            _value(row, "Id", "id")
        )

        if erp_id is None:
            continue

        company_code = _object(
            row,
            "CompanyCode",
            "companyCode",
        )
        universal_value = _value(
            row,
            "UniversalResponsibility",
            "universalResponsibility",
        )
        universal_object = (
            universal_value
            if isinstance(universal_value, dict)
            else {}
        )
        universal_official = _object(
            universal_object,
            "ResponsibleOfficial",
            "responsibleOfficial",
        ) or _object(
            row,
            "UniversalResponsibleOfficial",
            "UniversalResponsibilityOfficial",
            "ResponsibleOfficial",
        )
        universal_person = _object(
            universal_official,
            "Person",
            "person",
        )

        item = WowiEconomicUnit.query.filter_by(
            erp_id=erp_id
        ).one_or_none()

        if item is None:
            item = WowiEconomicUnit(
                erp_id=erp_id
            )
            db.session.add(item)

        item.id_num = _text(
            _value(row, "IdNum", "idNum")
        )
        item.name = _text(
            _value(row, "Name", "name")
        )
        item.location = _text(
            _value(row, "Location", "location")
        )
        item.company_code_erp_id = _as_int(
            _value(company_code, "Id", "id")
            or _value(
                row,
                "CompanyCodeId",
                "companyCodeId",
            )
        )

        company_code_code = _text(
            _value(
                company_code,
                "Code",
                "code",
                "IdNum",
                "idNum",
            )
            or _value(
                row,
                "CompanyCodeCode",
                "companyCodeCode",
            )
        )

        if (
            company_code_code is None
            and item.company_code_erp_id is not None
        ):
            local_company_code = (
                WowiCompanyCode.query.filter_by(
                    erp_id=item.company_code_erp_id
                ).one_or_none()
            )
            company_code_code = (
                local_company_code.code
                if local_company_code is not None
                else None
            )

        item.company_code_code = company_code_code
        item.universal_responsibility = (
            bool(universal_object)
            or _as_bool(universal_value)
        )
        item.universal_responsible_official_erp_id = (
            _as_int(
                _value(
                    universal_official,
                    "Id",
                    "id",
                )
                or _value(
                    row,
                    "UniversalResponsibleOfficialId",
                )
            )
        )
        item.universal_responsible_person_erp_id = (
            _as_int(
                _value(
                    universal_official,
                    "PersonId",
                    "personId",
                )
                or _value(
                    universal_person,
                    "Id",
                    "id",
                )
                or _value(
                    row,
                    "UniversalResponsiblePersonId",
                )
            )
        )
        item.active = True
        item.synced_at = synced_at

        seen_economic_ids.add(erp_id)
        result["economic_units"] += 1

    _deactivate_missing(
        WowiEconomicUnit,
        seen_economic_ids,
        synced_at,
    )
    db.session.flush()

    building_rows = service.read_all_rows(
        "CommercialInventory/BuildingLands",
        {
            "showNullValues": "true",
        },
    )
    _require_rows(
        building_rows,
        "Gebäude",
    )

    seen_building_ids = set()

    for row in building_rows:
        erp_id = _as_int(
            _value(row, "Id", "id")
        )

        if erp_id is None:
            continue

        economic = _object(
            row,
            "EconomicUnit",
            "economicUnit",
        )
        economic_erp_id = _as_int(
            _value(economic, "Id", "id")
            or _value(
                row,
                "EconomicUnitId",
                "economicUnitId",
            )
        )
        economic_unit = _economic_unit(
            economic_erp_id
        )
        address = _object(
            row,
            "EstateAddress",
            "estateAddress",
            "Address",
            "address",
        )
        street = _text(
            _value(
                address,
                "StreetComplete",
                "streetComplete",
            )
            or _join(
                _value(
                    address,
                    "Street",
                    "street",
                ),
                _value(
                    address,
                    "HouseNumber",
                    "houseNumber",
                ),
            )
        )
        zip_code = _text(
            _value(
                address,
                "Zip",
                "zip",
                "PostalCode",
                "postalCode",
            )
        )
        town = _text(
            _value(
                address,
                "Town",
                "town",
                "City",
                "city",
            )
        )

        item = WowiBuilding.query.filter_by(
            erp_id=erp_id
        ).one_or_none()

        if item is None:
            item = WowiBuilding(
                erp_id=erp_id
            )
            db.session.add(item)

        item.id_num = _text(
            _value(row, "IdNum", "idNum")
        )
        item.economic_unit_erp_id = (
            economic_erp_id
        )
        item.economic_unit = economic_unit
        item.street = street
        item.zip_code = zip_code
        item.town = town
        item.address = _join(
            street,
            _join(zip_code, town),
            separator=", ",
        )
        item.exit_date = _as_datetime(
            _value(
                row,
                "ExitDate",
                "exitDate",
            )
        )
        item.active = (
            item.exit_date is None
            or item.exit_date.date() >= date.today()
        )
        item.synced_at = synced_at

        seen_building_ids.add(erp_id)
        result["buildings"] += 1

    _deactivate_missing(
        WowiBuilding,
        seen_building_ids,
        synced_at,
    )
    db.session.flush()

    use_unit_rows = service.read_all_rows(
        "CommercialInventory/UseUnits",
        {
            "showNullValues": "true",
        },
    )
    _require_rows(
        use_unit_rows,
        "Nutzungseinheiten",
    )

    seen_use_unit_ids = set()

    for row in use_unit_rows:
        erp_id = _as_int(
            _value(row, "Id", "id")
        )

        if erp_id is None:
            continue

        building_object = _object(
            row,
            "BuildingLand",
            "buildingLand",
            "Building",
            "building",
        )
        building_erp_id = _as_int(
            _value(
                building_object,
                "Id",
                "id",
            )
            or _value(
                row,
                "BuildingLandId",
                "buildingLandId",
            )
        )
        building = (
            WowiBuilding.query.filter_by(
                erp_id=building_erp_id
            ).one_or_none()
            if building_erp_id is not None
            else None
        )
        economic = _object(
            row,
            "EconomicUnit",
            "economicUnit",
        )
        economic_erp_id = _as_int(
            _value(economic, "Id", "id")
            or _value(
                row,
                "EconomicUnitId",
                "economicUnitId",
            )
            or (
                building.economic_unit_erp_id
                if building
                else None
            )
        )
        floor = _object(
            row,
            "Floor",
            "floor",
        )
        position = _object(
            row,
            "Position",
            "position",
        )
        address_object = _object(
            row,
            "EstateAddress",
            "estateAddress",
            "Address",
            "address",
        )
        address = (
            _format_address(address_object)
            or (
                building.address
                if building
                else None
            )
        )

        item = WowiUseUnit.query.filter_by(
            erp_id=erp_id
        ).one_or_none()

        if item is None:
            item = WowiUseUnit(
                erp_id=erp_id
            )
            db.session.add(item)

        item.id_num = _text(
            _value(row, "IdNum", "idNum")
        )
        item.economic_unit_erp_id = (
            economic_erp_id
        )
        item.economic_unit = _economic_unit(
            economic_erp_id
        )
        item.building_erp_id = building_erp_id
        item.building = building
        item.floor_name = _text(
            _value(floor, "Name", "name")
            or _value(row, "FloorName")
        )
        item.position_name = _text(
            _value(position, "Name", "name")
            or _value(row, "PositionName")
        )
        item.description_of_position = _text(
            _value(
                row,
                "DescriptionOfPosition",
                "descriptionOfPosition",
            )
        )
        item.address = address

        exit_date = _as_datetime(
            _value(
                row,
                "ExitDate",
                "exitDate",
            )
        )
        item.active = (
            exit_date is None
            or exit_date.date() >= date.today()
        )
        item.synced_at = synced_at

        seen_use_unit_ids.add(erp_id)
        result["use_units"] += 1

    _deactivate_missing(
        WowiUseUnit,
        seen_use_unit_ids,
        synced_at,
    )
    db.session.flush()

    agreement_rows = service.read_all_rows(
        "RentAccounting/LicenseAgreements",
        {
            "licenseAgreementActiveOn": active_on,
            "showNullValues": "true",
        },
    )

    seen_agreement_ids = set()

    for row in agreement_rows:
        erp_id = _as_int(
            _value(row, "Id", "id")
        )

        if erp_id is None:
            continue

        use_unit_object = _object(
            row,
            "UseUnit",
            "useUnit",
        )
        use_unit_erp_id = _as_int(
            _value(
                use_unit_object,
                "Id",
                "id",
            )
            or _value(
                row,
                "UseUnitId",
                "useUnitId",
            )
        )
        use_unit = (
            WowiUseUnit.query.filter_by(
                erp_id=use_unit_erp_id
            ).one_or_none()
            if use_unit_erp_id is not None
            else None
        )
        restriction = _object(
            row,
            "RestrictionOfUse",
            "restrictionOfUse",
        )

        item = WowiLicenseAgreement.query.filter_by(
            erp_id=erp_id
        ).one_or_none()

        if item is None:
            item = WowiLicenseAgreement(
                erp_id=erp_id
            )
            db.session.add(item)

        item.id_num = _text(
            _value(row, "IdNum", "idNum")
        )
        item.use_unit_erp_id = use_unit_erp_id
        item.use_unit = use_unit
        item.start_date = _as_date(
            _value(
                row,
                "StartContract",
                "startContract",
                "StartDate",
                "startDate",
            )
        )
        item.end_date = _as_date(
            _value(
                row,
                "EndOfContract",
                "endOfContract",
                "EndDate",
                "endDate",
            )
        )
        item.is_vacancy = _as_bool(
            _value(
                restriction,
                "IsVacancy",
                "isVacancy",
            )
            or _value(
                row,
                "IsVacancy",
                "isVacancy",
            )
        )
        item.active = True
        item.synced_at = synced_at

        seen_agreement_ids.add(erp_id)
        result["license_agreements"] += 1

    _deactivate_missing(
        WowiLicenseAgreement,
        seen_agreement_ids,
        synced_at,
    )
    db.session.flush()

    contractor_rows = service.read_all_rows(
        "RentAccountingPersonDetails/Contractors",
        {
            "licenseAgreementActiveOn": active_on,
            "contractualUseActiveOn": active_on,
            "showNullValues": "true",
        },
    )

    seen_contractor_keys = set()

    for row in contractor_rows:
        erp_id = _as_int(
            _value(row, "Id", "id")
        )
        agreement_object = _object(
            row,
            "LicenseAgreement",
            "licenseAgreement",
        )
        agreement_erp_id = _as_int(
            _value(
                agreement_object,
                "Id",
                "id",
            )
            or _value(
                row,
                "LicenseAgreementId",
                "licenseAgreementId",
            )
        )

        if (
                erp_id is None
                or agreement_erp_id is None
        ):
            continue

        agreement = (
            WowiLicenseAgreement.query
            .filter_by(
                erp_id=agreement_erp_id
            )
            .one_or_none()
        )
        person = _object(
            row,
            "Person",
            "person",
        )
        name = (
                _person_name(person)
                or _text(
            _value(row, "Name", "name")
        )
        )

        if name is None:
            continue

        item = (
            WowiContractor.query
            .filter_by(
                erp_id=erp_id,
                license_agreement_erp_id=(
                    agreement_erp_id
                ),
            )
            .one_or_none()
        )

        if item is None:
            item = WowiContractor(
                erp_id=erp_id,
                license_agreement_erp_id=(
                    agreement_erp_id
                ),
                display_name=name,
            )
            db.session.add(item)

        item.license_agreement_erp_id = (
            agreement_erp_id
        )
        item.license_agreement = agreement
        item.person_erp_id = _as_int(
            _value(person, "Id", "id")
            or _value(
                row,
                "PersonId",
                "personId",
            )
        )
        item.display_name = name
        item.contractual_use_from = _as_date(
            _value(
                row,
                "ContractualUseValidFrom",
                "contractualUseValidFrom",
                "ContractualUseFrom",
                "contractualUseFrom",
            )
        )
        item.contractual_use_to = _as_date(
            _value(
                row,
                "ContractualUseValidTo",
                "contractualUseValidTo",
                "ContractualUseTo",
                "contractualUseTo",
            )
        )
        item.active = agreement is not None
        item.synced_at = synced_at

        seen_contractor_keys.add(
            (
                erp_id,
                agreement_erp_id,
            )
        )
        result["contractors"] += 1

    for item in WowiContractor.query.filter(
            WowiContractor.active.is_(True)
    ).all():
        key = (
            item.erp_id,
            item.license_agreement_erp_id,
        )

        if key not in seen_contractor_keys:
            item.active = False
            item.synced_at = synced_at

    seen_department_ids = set()
    seen_jurisdiction_ids = set()

    for economic_unit in (
        WowiEconomicUnit.query.filter_by(
            active=True
        ).all()
    ):
        response_rows = service.read_all_rows(
            "CommercialInventory/EconomicUnit/Jurisdiction",
            {
                "economicUnitId": (
                    economic_unit.erp_id
                ),
                "showNullValues": "true",
            },
        )
        rows = []

        for response_row in response_rows:
            universal_official = _object(
                response_row,
                "EconomicUnitUniversalResponsibleOfficial",
                "economicUnitUniversalResponsibleOfficial",
            )
            economic_unit.universal_responsibility = (
                _as_bool(
                    _value(
                        response_row,
                        "EconomicUnitUniversalResponsibility",
                        "economicUnitUniversalResponsibility",
                    )
                )
            )
            economic_unit.universal_responsible_official_erp_id = (
                _as_int(
                    _value(
                        universal_official,
                        "Id",
                        "id",
                    )
                )
            )
            economic_unit.universal_responsible_person_erp_id = (
                _as_int(
                    _value(
                        universal_official,
                        "PersonId",
                        "personId",
                    )
                )
            )

            jurisdiction_list = _value(
                response_row,
                "EconomicUnitJurisdictionList",
                "economicUnitJurisdictionList",
            )

            if isinstance(
                jurisdiction_list,
                list,
            ):
                rows.extend(
                    row
                    for row in jurisdiction_list
                    if isinstance(row, dict)
                )
            elif (
                _value(
                    response_row,
                    "Id",
                    "id",
                )
                is not None
            ):
                rows.append(response_row)

        for row in rows:
            erp_id = _as_int(
                _value(row, "Id", "id")
            )
            department = _object(
                row,
                "Department",
                "department",
            )
            department_type = _object(
                row,
                "DepartmentType",
                "departmentType",
            ) or _object(
                department,
                "DepartmentType",
                "departmentType",
            )
            official = _object(
                row,
                "ResponsibleOfficial",
                "responsibleOfficial",
            )
            person = _object(
                official,
                "Person",
                "person",
            )

            department_erp_id = _as_int(
                _value(
                    department,
                    "Id",
                    "id",
                )
                or _value(
                    row,
                    "DepartmentId",
                    "departmentId",
                )
            )
            department_type_erp_id = _as_int(
                _value(
                    department_type,
                    "Id",
                    "id",
                )
                or _value(
                    row,
                    "DepartmentTypeId",
                    "departmentTypeId",
                )
            )

            if (
                    erp_id is None
                    or department_erp_id is None
                    or department_type_erp_id is None
            ):
                continue

            department_name = _text(
                _value(
                    department,
                    "Name",
                    "name",
                )
            )

            cached_department = (
                WowiDepartment.query.filter_by(
                    erp_id=department_erp_id
                ).one_or_none()
            )

            if cached_department is None:
                cached_department = WowiDepartment(
                    erp_id=department_erp_id,
                    name=(
                            department_name
                            or str(department_erp_id)
                    ),
                )
                db.session.add(cached_department)

            cached_department.name = (
                    department_name
                    or cached_department.name
            )
            cached_department.active = True
            cached_department.synced_at = synced_at

            if (
                    department_erp_id
                    not in seen_department_ids
            ):
                result["departments"] += 1

            seen_department_ids.add(
                department_erp_id
            )

            item = (
                WowiEconomicUnitJurisdiction.query
                .filter_by(
                    erp_id=erp_id
                )
                .one_or_none()
            )

            if item is None:
                item = WowiEconomicUnitJurisdiction(
                    erp_id=erp_id,
                    economic_unit=economic_unit,
                    department_type_erp_id=(
                        department_type_erp_id
                    ),
                )
                db.session.add(item)

            item.economic_unit = economic_unit
            item.department_erp_id = department_erp_id
            item.department_name = department_name
            item.department_type_erp_id = (
                department_type_erp_id
            )
            item.department_type_name = _text(
                _value(
                    department_type,
                    "Name",
                    "name",
                )
            )
            item.responsible_official_erp_id = (
                _as_int(
                    _value(
                        official,
                        "Id",
                        "id",
                    )
                )
            )
            item.responsible_person_erp_id = (
                _as_int(
                    _value(
                        official,
                        "PersonId",
                        "personId",
                    )
                    or _value(
                        person,
                        "Id",
                        "id",
                    )
                )
            )
            item.responsible_name = (
                    _person_name(person)
                    or _text(
                _value(
                    official,
                    "PersonName",
                    "personName",
                    "Name",
                    "name",
                )
            )
            )
            item.main_jurisdiction = _as_bool(
                _value(
                    row,
                    "MainJurisdiction",
                    "mainJurisdiction",
                )
            )
            item.active = True
            item.synced_at = synced_at

            seen_jurisdiction_ids.add(erp_id)
            result["jurisdictions"] += 1

    _deactivate_missing(
        WowiEconomicUnitJurisdiction,
        seen_jurisdiction_ids,
        synced_at,
    )
    _deactivate_missing(
        WowiDepartment,
        seen_department_ids,
        synced_at,
    )

    return result


def resolve_commission_object(
    data: dict[str, Any],
) -> CommissionObjectContext:
    object_type = _text(
        data.get("object_type")
    )
    object_id = _as_int(
        data.get("object_id")
    )
    agreement = None
    use_unit = None
    building = None

    if object_type and object_id:
        if object_type == "license_agreement":
            agreement = db.session.get(
                WowiLicenseAgreement,
                object_id,
            )

            if (
                agreement is None
                or not agreement.active
            ):
                raise ValueError(
                    "Der ausgewählte Mietvertrag "
                    "ist nicht mehr verfügbar."
                )

            use_unit = agreement.use_unit

        elif object_type == "use_unit":
            use_unit = db.session.get(
                WowiUseUnit,
                object_id,
            )

            if (
                use_unit is None
                or not use_unit.active
            ):
                raise ValueError(
                    "Die ausgewählte Nutzungseinheit "
                    "ist nicht mehr verfügbar."
                )

        elif object_type == "building":
            building = db.session.get(
                WowiBuilding,
                object_id,
            )

            if (
                building is None
                or not building.active
            ):
                raise ValueError(
                    "Das ausgewählte Gebäude "
                    "ist nicht mehr verfügbar."
                )

        else:
            raise ValueError(
                "Die ausgewählte Objektart ist ungültig."
            )

    else:
        agreement_idnum = _text(
            data.get("license_agreement_idnum")
        )
        use_unit_idnum = _text(
            data.get("use_unit_idnum")
        )
        building_idnum = _text(
            data.get("building_idnum")
        )

        if agreement_idnum:
            agreement = _single_by_idnum(
                WowiLicenseAgreement,
                agreement_idnum,
                "Mietvertrag",
            )
            use_unit = agreement.use_unit

        if use_unit_idnum:
            selected_use_unit = _single_by_idnum(
                WowiUseUnit,
                use_unit_idnum,
                "Nutzungseinheit",
            )

            if (
                use_unit is not None
                and use_unit.id
                != selected_use_unit.id
            ):
                raise ValueError(
                    "Mietvertrag und Nutzungseinheit "
                    "gehören nicht zusammen."
                )

            use_unit = selected_use_unit

        if building_idnum:
            building = _single_by_idnum(
                WowiBuilding,
                building_idnum,
                "Gebäude",
            )

    if use_unit is not None:
        if (
            building is not None
            and use_unit.building_id
            != building.id
        ):
            raise ValueError(
                "Nutzungseinheit und Gebäude "
                "gehören nicht zusammen."
            )

        building = use_unit.building

    economic_unit = (
        use_unit.economic_unit
        if (
            use_unit is not None
            and use_unit.economic_unit is not None
        )
        else (
            building.economic_unit
            if building is not None
            else None
        )
    )

    if (
        economic_unit is None
        or not economic_unit.active
    ):
        raise ValueError(
            "Für das ausgewählte Objekt ist keine "
            "aktive Wirtschaftseinheit hinterlegt."
        )

    if (
        economic_unit.company_code_erp_id is None
        or not economic_unit.company_code_code
    ):
        raise ValueError(
            "Für die Wirtschaftseinheit ist "
            "kein Buchwerk hinterlegt."
        )

    requested_company_code = _text(
        data.get("company_code_code")
    )

    if (
        requested_company_code
        and requested_company_code
        != economic_unit.company_code_code
    ):
        raise ValueError(
            "Das übergebene Buchwerk gehört "
            "nicht zum ausgewählten Objekt."
        )

    tenant_names = []

    if (
        agreement is not None
        and not agreement.is_vacancy
    ):
        tenant_names = [
            contractor.display_name
            for contractor in agreement.contractors
            if contractor.active
        ]

    return CommissionObjectContext(
        economic_unit=economic_unit,
        building=building,
        use_unit=use_unit,
        license_agreement=agreement,
        tenant_name=(
            ", ".join(
                sorted(set(tenant_names))
            )
            or None
        ),
    )


def resolve_commission_assignment(
    service_positions: Iterable[
        TimeTaskTemplate
    ],
    object_context: CommissionObjectContext,
) -> CommissionAssignment:
    positions = list(service_positions)

    if not positions:
        raise ValueError(
            "Mindestens eine Leistung ist erforderlich."
        )

    if any(
        position.craft_activity_id is None
        for position in positions
    ):
        raise ValueError(
            "Mindestens einer Leistung ist "
            "kein Gewerk zugeordnet."
        )

    craft_activity_ids = {
        position.craft_activity_id
        for position in positions
    }

    if len(craft_activity_ids) != 1:
        raise ValueError(
            "Alle Positionen eines Auftrags müssen "
            "demselben Gewerk angehören."
        )

    craft_activity_id = next(
        iter(craft_activity_ids)
    )
    mapping = (
        CraftActivityDepartmentMapping.query
        .filter_by(
            craft_activity_id=craft_activity_id
        )
        .one_or_none()
    )

    if (
            mapping is None
            or not mapping.department.active
    ):
        craft_activity = db.session.get(
            CraftActivity,
            craft_activity_id,
        )
        craft_name = (
            craft_activity.name
            if craft_activity is not None
            else str(craft_activity_id)
        )

        raise ValueError(
            f"Für das Gewerk „{craft_name}“ "
            "ist keine aktive Abteilung hinterlegt."
        )

    jurisdictions = (
        WowiEconomicUnitJurisdiction.query
        .filter_by(
            economic_unit_id=(
                object_context.economic_unit.id
            ),
            department_erp_id=(
                mapping.department.erp_id
            ),
            active=True,
        )
        .all()
    )

    main_jurisdictions = [
        jurisdiction
        for jurisdiction in jurisdictions
        if jurisdiction.main_jurisdiction
    ]
    candidates = (
            main_jurisdictions
            or jurisdictions
    )

    if len(candidates) != 1:
        raise ValueError(
            "Für Wirtschaftseinheit und Gewerk "
            "ist keine eindeutige Zuständigkeit "
            "hinterlegt."
        )

    jurisdiction = candidates[0]

    if (
        object_context.economic_unit
        .universal_responsibility
    ):
        responsible_official_erp_id = (
            object_context.economic_unit
            .universal_responsible_official_erp_id
        )
        responsible_person_erp_id = (
            object_context.economic_unit
            .universal_responsible_person_erp_id
        )
    else:
        responsible_official_erp_id = (
            jurisdiction
            .responsible_official_erp_id
        )
        responsible_person_erp_id = (
            jurisdiction
            .responsible_person_erp_id
        )

    if (
        responsible_official_erp_id is None
        or responsible_person_erp_id is None
    ):
        raise ValueError(
            "In der Zuständigkeit fehlt "
            "die verantwortliche Person."
        )

    users = User.query.filter_by(
        person_erp_id=responsible_person_erp_id,
        is_active_flag=True,
    ).all()
    users = [
        user
        for user in users
        if user.craftsman_erp_id is not None
    ]

    if len(users) != 1:
        raise ValueError(
            "Die zuständige Wowiport-Person ist "
            "keinem eindeutigen aktiven "
            "Handwerker zugeordnet."
        )

    return CommissionAssignment(
        user=users[0],
        craft_activity=mapping.craft_activity,
        department=mapping.department,
        jurisdiction=jurisdiction,
        responsible_official_erp_id=(
            responsible_official_erp_id
        ),
    )


def commission_creation_tasks():
    return (
        TimeTaskTemplate.query
        .join(
            CraftActivityDepartmentMapping,
            (
                CraftActivityDepartmentMapping
                .craft_activity_id
                == TimeTaskTemplate.craft_activity_id
            ),
        )
        .join(
            WowiDepartment,
            (
                WowiDepartment.id
                == CraftActivityDepartmentMapping
                .department_id
            ),
        )
        .filter(
            TimeTaskTemplate.active.is_(True),
            (
                TimeTaskTemplate
                .erp_service_catalogue_id
                .isnot(None)
            ),
            TimeTaskTemplate.craft_activity.has(
                CraftActivity.active.is_(True)
                & CraftActivity.app_enabled.is_(
                    True
                )
                & CraftActivity.erp_id.isnot(None)
            ),
            WowiDepartment.active.is_(True),
        )
    )


def search_commission_objects(
    search_text: str,
    limit: int = 30,
) -> list[dict[str, Any]]:
    normalized = " ".join(
        search_text.split()
    )

    if len(normalized) < 2:
        return []

    pattern = f"%{normalized}%"
    choices = []
    seen = set()

    contractors = (
        WowiContractor.query
        .join(WowiLicenseAgreement)
        .filter(
            WowiContractor.active.is_(True),
            WowiLicenseAgreement.active.is_(True),
            or_(
                WowiContractor.display_name.ilike(
                    pattern
                ),
                WowiLicenseAgreement.id_num.ilike(
                    pattern
                ),
            ),
        )
        .order_by(
            WowiContractor.display_name.asc()
        )
        .limit(limit)
        .all()
    )

    for contractor in contractors:
        agreement = contractor.license_agreement

        if agreement is None:
            continue

        key = (
            "license_agreement",
            agreement.id,
        )
        _append_choice(
            choices,
            seen,
            key,
        )

    agreements = (
        WowiLicenseAgreement.query
        .filter(
            WowiLicenseAgreement.active.is_(True),
            WowiLicenseAgreement.id_num.ilike(
                pattern
            ),
        )
        .order_by(
            WowiLicenseAgreement.id_num.asc()
        )
        .limit(limit)
        .all()
    )

    for agreement in agreements:
        key = (
            "license_agreement",
            agreement.id,
        )
        _append_choice(
            choices,
            seen,
            key,
        )

    use_units = (
        WowiUseUnit.query
        .filter(
            WowiUseUnit.active.is_(True),
            or_(
                WowiUseUnit.id_num.ilike(
                    pattern
                ),
                WowiUseUnit.address.ilike(
                    pattern
                ),
                WowiUseUnit
                .description_of_position
                .ilike(pattern),
            ),
        )
        .order_by(
            WowiUseUnit.id_num.asc()
        )
        .limit(limit)
        .all()
    )

    for use_unit in use_units:
        agreements = [
            agreement
            for agreement
            in use_unit.license_agreements
            if agreement.active
        ]
        keys = (
            [
                (
                    "license_agreement",
                    agreement.id,
                )
                for agreement in agreements
            ]
            if agreements
            else [
                (
                    "use_unit",
                    use_unit.id,
                )
            ]
        )

        for key in keys:
            _append_choice(
                choices,
                seen,
                key,
            )

    buildings = (
        WowiBuilding.query
        .filter(
            WowiBuilding.active.is_(True),
            or_(
                WowiBuilding.id_num.ilike(
                    pattern
                ),
                WowiBuilding.address.ilike(
                    pattern
                ),
            ),
        )
        .order_by(
            WowiBuilding.id_num.asc()
        )
        .limit(limit)
        .all()
    )

    for building in buildings:
        key = (
            "building",
            building.id,
        )
        _append_choice(
            choices,
            seen,
            key,
        )

    return choices[:limit]


def get_commission_object_choice(
    object_type: str,
    object_id: int,
) -> dict[str, Any]:
    return _serialize_choice(
        object_type,
        object_id,
    )


def _append_choice(
    choices: list[dict[str, Any]],
    seen: set[tuple[str, int]],
    key: tuple[str, int],
) -> None:
    if key in seen:
        return

    try:
        choice = _serialize_choice(*key)
    except ValueError:
        return

    choices.append(choice)
    seen.add(key)


def _serialize_choice(
    object_type: str,
    object_id: int,
) -> dict[str, Any]:
    if object_type == "license_agreement":
        agreement = db.session.get(
            WowiLicenseAgreement,
            object_id,
        )

        if (
            agreement is None
            or not agreement.active
        ):
            raise ValueError(
                "Der Mietvertrag ist nicht verfügbar."
            )

        use_unit = agreement.use_unit
        building = (
            use_unit.building
            if use_unit
            else None
        )
        tenant_names = (
            [
                contractor.display_name
                for contractor
                in agreement.contractors
                if contractor.active
            ]
            if not agreement.is_vacancy
            else []
        )
        tenant_name = (
            ", ".join(
                sorted(set(tenant_names))
            )
            or None
        )
        is_vacancy = agreement.is_vacancy

    elif object_type == "use_unit":
        agreement = None
        use_unit = db.session.get(
            WowiUseUnit,
            object_id,
        )

        if (
            use_unit is None
            or not use_unit.active
        ):
            raise ValueError(
                "Die Nutzungseinheit "
                "ist nicht verfügbar."
            )

        building = use_unit.building
        tenant_name = None
        is_vacancy = True

    elif object_type == "building":
        agreement = None
        use_unit = None
        building = db.session.get(
            WowiBuilding,
            object_id,
        )

        if (
            building is None
            or not building.active
        ):
            raise ValueError(
                "Das Gebäude ist nicht verfügbar."
            )

        tenant_name = None
        is_vacancy = False

    else:
        raise ValueError(
            "Die Objektart ist ungültig."
        )

    economic_unit = (
        use_unit.economic_unit
        if (
            use_unit is not None
            and use_unit.economic_unit is not None
        )
        else (
            building.economic_unit
            if building is not None
            else None
        )
    )

    if (
        economic_unit is None
        or not economic_unit.active
    ):
        raise ValueError(
            "Die Wirtschaftseinheit "
            "ist nicht verfügbar."
        )

    if agreement is not None:
        label = (
            tenant_name
            or (
                "Leerstand"
                if is_vacancy
                else "Mietvertrag"
            )
        )
    elif use_unit is not None:
        label = "Nutzungseinheit / Leerstand"
    else:
        label = "Gebäude"

    return {
        "object_type": object_type,
        "object_id": object_id,
        "label": label,
        "tenant_name": tenant_name,
        "is_vacancy": is_vacancy,
        "company_code": {
            "erp_id": (
                economic_unit.company_code_erp_id
            ),
            "code": (
                economic_unit.company_code_code
            ),
        },
        "economic_unit": {
            "erp_id": economic_unit.erp_id,
            "id_num": economic_unit.id_num,
            "name": economic_unit.name,
        },
        "building": (
            {
                "erp_id": building.erp_id,
                "id_num": building.id_num,
                "address": building.address,
            }
            if building is not None
            else None
        ),
        "use_unit": (
            {
                "erp_id": use_unit.erp_id,
                "id_num": use_unit.id_num,
                "floor": use_unit.floor_name,
                "position": (
                    use_unit.position_name
                ),
                "description": (
                    use_unit
                    .description_of_position
                ),
                "address": use_unit.address,
            }
            if use_unit is not None
            else None
        ),
        "license_agreement": (
            {
                "erp_id": agreement.erp_id,
                "id_num": agreement.id_num,
            }
            if agreement is not None
            else None
        ),
    }


def _single_by_idnum(
    model,
    id_num: str,
    label: str,
):
    items = (
        model.query
        .filter_by(
            id_num=id_num,
            active=True,
        )
        .limit(2)
        .all()
    )

    if len(items) != 1:
        raise ValueError(
            f"{label} wurde nicht "
            "eindeutig gefunden."
        )

    return items[0]


def _economic_unit(
    erp_id: int | None,
) -> WowiEconomicUnit | None:
    if erp_id is None:
        return None

    return WowiEconomicUnit.query.filter_by(
        erp_id=erp_id
    ).one_or_none()


def _deactivate_missing(
    model,
    seen_ids: set[int],
    synced_at: datetime,
) -> None:
    query = model.query.filter(
        model.active.is_(True)
    )

    if seen_ids:
        query = query.filter(
            model.erp_id.notin_(seen_ids)
        )

    for item in query.all():
        item.active = False
        item.synced_at = synced_at


def _require_rows(
    rows: list[dict[str, Any]],
    label: str,
) -> None:
    if not rows:
        raise RuntimeError(
            f"OpenWowi hat keine {label} geliefert."
        )


def _value(
    source: Any,
    *names: str,
) -> Any:
    if not isinstance(source, dict):
        return None

    for name in names:
        if name in source:
            return source[name]

    lower_items = {
        str(key).lower(): value
        for key, value in source.items()
    }

    for name in names:
        if name.lower() in lower_items:
            return lower_items[name.lower()]

    return None


def _object(
    source: Any,
    *names: str,
) -> dict[str, Any]:
    value = _value(
        source,
        *names,
    )

    return (
        value
        if isinstance(value, dict)
        else {}
    )


def _as_int(
    value: Any,
) -> int | None:
    try:
        return (
            int(value)
            if value not in (None, "")
            else None
        )
    except (TypeError, ValueError):
        return None


def _as_bool(
    value: Any,
) -> bool:
    if isinstance(value, bool):
        return value

    if isinstance(value, str):
        return (
            value.strip().lower()
            in {
                "1",
                "true",
                "yes",
                "ja",
            }
        )

    return bool(value)


def _as_date(
    value: Any,
) -> date | None:
    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    if not value:
        return None

    try:
        return datetime.fromisoformat(
            str(value).replace(
                "Z",
                "+00:00",
            )
        ).date()
    except ValueError:
        return None


def _as_datetime(
    value: Any,
) -> datetime | None:
    if isinstance(value, datetime):
        return value

    if not value:
        return None

    try:
        return datetime.fromisoformat(
            str(value).replace(
                "Z",
                "+00:00",
            )
        )
    except ValueError:
        return None


def _text(
    value: Any,
) -> str | None:
    if value is None:
        return None

    result = str(value).strip()

    return result or None


def _join(
    *values: Any,
    separator: str = " ",
) -> str | None:
    items = [
        _text(value)
        for value in values
    ]

    return (
        separator.join(
            item
            for item in items
            if item
        )
        or None
    )


def _format_address(
    address: dict[str, Any],
) -> str | None:
    street = (
        _value(
            address,
            "StreetComplete",
            "streetComplete",
        )
        or _join(
            _value(
                address,
                "Street",
                "street",
            ),
            _value(
                address,
                "HouseNumber",
                "houseNumber",
            ),
        )
    )
    zip_code = _value(
        address,
        "Zip",
        "zip",
        "PostalCode",
        "postalCode",
    )
    town = _value(
        address,
        "Town",
        "town",
        "City",
        "city",
    )

    return _join(
        street,
        _join(zip_code, town),
        separator=", ",
    )


def _person_name(
    person: dict[str, Any],
) -> str | None:
    natural = _object(
        person,
        "NaturalPerson",
        "naturalPerson",
    )
    legal = _object(
        person,
        "LegalPerson",
        "legalPerson",
    )
    first_name = _text(
        _value(
            natural,
            "FirstName",
            "firstName",
        )
    )
    last_name = _text(
        _value(
            natural,
            "LastName",
            "lastName",
        )
    )

    if first_name or last_name:
        return _join(
            last_name,
            first_name,
            separator=", ",
        )

    return _text(
        _value(
            legal,
            "LongName1",
            "longName1",
            "Name",
            "name",
        )
        or _value(
            person,
            "Name",
            "name",
        )
    )
