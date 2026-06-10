from __future__ import annotations

from datetime import datetime
from typing import Any

from flask import current_app
from wowipy import wowipy
from wowipy.models import CommissioningCommission, Communication, Contractor, LicenseAgreement, UseUnit
from wowipy.wowipy import WowiPy

from .extensions import db
from .models import Commission, CommissionLocalStatus, User, utc_now
from .settings import SettingKeys, settings
from .utils import parse_datetime


def _format_use_unit_location(use_unit: UseUnit | None) -> str | None:
    if use_unit is None:
        return None

    floor = _attr(use_unit, "floor")
    position = _attr(use_unit, "position")

    return _join_unique_text(
        _attr(floor, "name"),
        _attr(position, "name"),
        _attr(use_unit, "description_of_position"),
    )


def _format_use_unit_address(use_unit: UseUnit | None) -> str | None:
    if use_unit is None:
        return None

    estate_address = _attr(use_unit, "estate_address")
    if estate_address is None:
        return None

    street = _first_text(
        _attr(estate_address, "street_complete"),
        _join_text(
            _attr(estate_address, "street"),
            _attr(estate_address, "house_number"),
        ),
    )

    zip_code = _first_text(
        _attr(estate_address, "zip_"),
        _attr(estate_address, "zip"),
        _attr(estate_address, "postal_code"),
    )

    town = _first_text(
        _attr(estate_address, "town"),
        _attr(estate_address, "city"),
    )

    line1 = _join_text(street)
    line2 = _join_text(zip_code, town)

    return _join_text(line1, line2, separator=", ")


def _format_birth_date(value: Any) -> str | None:
    if value is None:
        return None

    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")

    return str(value)


def _format_natural_person_name(
        title: Any,
        first_name: Any,
        last_name: Any,
) -> str | None:
    title_text = f"{title} " if title else ""

    if last_name and first_name:
        return f"{title_text}{last_name}, {first_name}"

    if last_name:
        return f"{title_text}{last_name}"

    if first_name:
        return f"{title_text}{first_name}"

    return None


def _first_communication(
        preferred: Communication | None,
        fallback_items: list[Communication],
) -> Communication | None:
    if preferred is not None and _attr(preferred, "content"):
        return preferred

    if fallback_items:
        return fallback_items[0]

    return None


def _get_comm_items_of_type(
        type_ids: list[int],
        communication_items: list[Communication],
) -> list[Communication]:
    items = []

    for communication in communication_items:
        communication_type = _attr(communication, "communication_type")
        communication_type_id = _as_int(_attr(communication_type, "id_"))

        if communication_type_id in type_ids:
            items.append(communication)

    return items


def _build_contact_item(contractor: Contractor) -> dict[str, Any] | None:
    person = _attr(contractor, "person")
    if person is None:
        return None

    natural_person = _attr(person, "natural_person")
    legal_person = _attr(person, "legal_person")
    is_natural_person = bool(_attr(person, "is_natural_person"))

    if is_natural_person and natural_person is not None:
        person_name = _format_natural_person_name(
            title=_attr(natural_person, "title"),
            first_name=_attr(natural_person, "first_name"),
            last_name=_attr(natural_person, "last_name"),
        )
        gender = _attr(_attr(natural_person, "gender"), "name")
        birth_date = _format_birth_date(_attr(natural_person, "birth_date"))
    else:
        person_name = _first_text(
            _attr(legal_person, "long_name1"),
            _attr(legal_person, "name"),
            _attr(person, "name"),
        )
        gender = None
        birth_date = None

    communications = list(_attr(person, "communications") or [])

    email_entry = _first_communication(
        preferred=_attr(person, "first_email_communication"),
        fallback_items=_get_comm_items_of_type([5], communications),
    )

    phone_entry = _first_communication(
        preferred=_attr(person, "first_landline_phone_communication"),
        fallback_items=_get_comm_items_of_type([1], communications),
    )

    mobile_entry = _first_communication(
        preferred=_attr(person, "first_mobile_phone_communication"),
        fallback_items=_get_comm_items_of_type([3], communications),
    )

    phone = _attr(phone_entry, "content")
    mobile = _attr(mobile_entry, "content")

    additional_numbers = []

    for communication in _get_comm_items_of_type([1, 3], communications):
        content = _attr(communication, "content")
        if content and content not in {phone, mobile}:
            additional_numbers.append(content)

    contractor_type = _attr(contractor, "contractor_type")

    return {
        "person_id": _attr(person, "id_"),
        "role": _attr(contractor_type, "name"),
        "name": person_name,
        "gender": gender,
        "email": _attr(email_entry, "content"),
        "email_id": _attr(email_entry, "id_"),
        "email_comment": _prepare_comment_for_display(_attr(email_entry, "explanation")),
        "phone": phone,
        "phone_id": _attr(phone_entry, "id_"),
        "phone_comment": _prepare_comment_for_display(_attr(phone_entry, "explanation")),
        "mobile": mobile,
        "mobile_id": _attr(mobile_entry, "id_"),
        "mobile_comment": _prepare_comment_for_display(_attr(mobile_entry, "explanation")),
        "birth_date": birth_date,
        "additional_numbers": additional_numbers,
    }


def _is_vacancy(license_agreement: LicenseAgreement) -> bool:
    restriction_of_use = _attr(license_agreement, "restriction_of_use")
    return bool(_attr(restriction_of_use, "is_vacancy"))


class WowiportService:
    def __init__(self) -> None:
        self.client = WowiPy(
            hostname=settings.get(SettingKeys.WOWI_HOSTNAME),
            user=settings.get(SettingKeys.WOWI_USERNAME),
            password=settings.get(SettingKeys.WOWI_PASSWORD),
            api_key=settings.get(SettingKeys.WOWI_API_KEY),
            version=settings.get(SettingKeys.WOWI_API_VERSION),
            logger=current_app.logger,
            user_agent="datlaeuft/1.0",
        )

    def get_commission(self, erp_id: int) -> wowipy.CommissioningCommission | None:
        try:
            commission_list = self.client.get_commissioning_commissions(
                id_=erp_id,
                include_commission_items=True,
                include_commission_details=True,
                show_null_values=True,
                limit=1,
            )
        except TypeError as exc:
            current_app.logger.warning(
                "get_commission with items failed for commission %s, retrying without items: %s",
                erp_id,
                exc,
            )
            commission_list = self.client.get_commissioning_commissions(
                id_=erp_id,
                include_commission_items=False,
                include_commission_details=True,
                show_null_values=True,
                limit=1,
            )

        if not commission_list:
            current_app.logger.error("get_commission: Cannot find commission %s in erp", erp_id)
            return None

        return commission_list[0]

    def mark_commission_done(self, erp_id: int) -> None:
        self.client.set_commissioning_commission_to_done(commission_id=erp_id)

    def get_use_unit(self, use_unit_id: int | None) -> UseUnit | None:
        if not use_unit_id:
            return None

        use_unit_list = self.client.get_use_units(
            use_unit_id=use_unit_id,
            limit=1,
        )

        if not use_unit_list:
            current_app.logger.warning("get_use_unit failed. use unit id %s not found", use_unit_id)
            return None

        return use_unit_list[0]

    def get_commission_context(
            self,
            erp_id: int,
            commission_object: CommissioningCommission | None = None,
    ) -> dict[str, Any]:
        if commission_object is None:
            commission_object = self.get_commission(erp_id)

        if commission_object is None:
            return {}

        use_unit_id_object = commission_object.use_unit
        if use_unit_id_object is not None:
            use_unit_id = use_unit_id_object.get("id")
        else:
            use_unit_id = None
        license_agreement_id_object = commission_object.license_agreement_id
        if license_agreement_id_object is not None:
            license_agreement_id = license_agreement_id_object.get("id")
        else:
            license_agreement_id = None

        use_unit = self.get_use_unit(use_unit_id)
        contact_items = self.get_commission_contact_items(
            erp_id=erp_id,
            commission_object=commission_object,
        )

        return {
            "use_unit_id": use_unit_id,
            "license_agreement_id": license_agreement_id,
            "apartment_address": _format_use_unit_address(use_unit),
            "apartment_location": _format_use_unit_location(use_unit),
            "tenant_name": contact_items[0].get("name") if contact_items else None,
        }

    def get_contractors(self, erp_id: int) -> list[dict[str, Any]] | None:
        contact_items = self.get_commission_contact_items(erp_id=erp_id)
        return contact_items if contact_items else None

    def get_commission_contact_items(
            self,
            erp_id: int,
            commission_object: CommissioningCommission | None = None,
    ) -> list[dict[str, Any]]:
        if commission_object is None:
            commission_object = self.get_commission(erp_id)

        if commission_object is None:
            current_app.logger.error("get_commission_contact_items failed. commission id %s not found", erp_id)
            return []

        use_unit_id = _as_int(_attr(commission_object, "use_unit_id"))
        license_agreement_object = commission_object.license_agreement
        if license_agreement_object:
            license_agreement_id = license_agreement_object.get("id")
        else:
            license_agreement_id = None

        license_agreement = self._get_license_agreement(
            license_agreement_id=license_agreement_id,
            use_unit_id=use_unit_id,
        )

        if license_agreement is not None and _is_vacancy(license_agreement):
            return []

        contractors = self._get_contractors_from_license_agreement(
            license_agreement=license_agreement,
            license_agreement_id=license_agreement_id,
        )

        if not contractors:
            current_app.logger.warning(
                "get_commission_contact_items failed. no contractors found for commission id %s, "
                "license agreement id %s",
                erp_id,
                license_agreement_id,
            )
            return []

        contact_items = []

        for contractor in contractors:
            contact_item = _build_contact_item(contractor)
            if contact_item:
                contact_items.append(contact_item)

        return contact_items

    def _get_license_agreement(
            self,
            license_agreement_id: int | None,
            use_unit_id: int | None,
    ) -> LicenseAgreement | None:
        if use_unit_id:
            agreement_list = self.client.get_license_agreements(
                license_agreement_active_on=datetime.now(),
                add_args={"useUnitId": use_unit_id},
                add_contractors=True,
                limit=1,
            )

            if agreement_list:
                return agreement_list[0]

        if license_agreement_id:
            agreement_list = self.client.get_license_agreements(
                add_args={"licenseAgreementId": license_agreement_id},
                add_contractors=True,
                limit=1,
            )

            if agreement_list:
                return agreement_list[0]

        return None

    def _get_contractors_from_license_agreement(
            self,
            license_agreement: LicenseAgreement | None,
            license_agreement_id: int | None,
    ) -> list[Contractor]:
        if license_agreement is not None:
            contractors = license_agreement.contractors
            if contractors:
                return list(contractors)

        if license_agreement_id:
            return self.client.get_contractors(
                license_agreement_id=license_agreement_id,
            )

        return []

    def get_craft_activities(self) -> list[Any]:
        return self.client.get_commissioning_craft_activities(show_null_values=True)

    def get_service_catalogues(self) -> list[Any]:
        return self.client.get_commissioning_service_catalogues(
            include_invalid=False,
            include_craftsman_agreements=True,
            include_commission_types=True,
            show_null_values=True,
            fetch_all=True,
        )

    def get_commissions_for_regular_sync(
        self,
        status_id: int,
        craftsman_erp_id: int,
        date_from: datetime,
        date_to: datetime,
        limit: int = 100,
        offset: int = 0,
    ) -> list[wowipy.CommissioningCommission]:
        add_args = {
            "commissionStatusId": status_id,
            "recordingDateFrom": date_from.strftime("%Y-%m-%d"),
            "recordingDateTo": date_to.strftime("%Y-%m-%d"),
        }

        return self.client.get_commissioning_commissions(
            limit=limit,
            offset=offset,
            craftsman_id=craftsman_erp_id,
            include_commission_items=False,
            include_commission_details=True,
            show_null_values=True,
            add_args=add_args,
        )


def sync_commission_from_erp(erp_id: int) -> Commission | None:
    service = WowiportService()
    payload = service.get_commission(erp_id)

    if payload is None:
        return mark_commission_deleted(erp_id)

    commission_status = _attr(payload, "commission_status")
    status_id = _as_int(_attr(commission_status, "id_"))
    approved_status_id = settings.get(SettingKeys.WOWI_APPROVED_STATUS_ID)
    existing = db.session.query(Commission).filter_by(erp_id=erp_id).one_or_none()

    if status_id != approved_status_id and existing is None:
        current_app.logger.info("commission_ignored erp_id=%s status_id=%s", erp_id, status_id)
        return None

    commission = upsert_commission(payload)

    try:
        context = service.get_commission_context(erp_id=erp_id, commission_object=payload)
        apply_commission_context(commission, context)
    except Exception as ex:
        current_app.logger.exception("commission_context_sync_failed erp_id=%s exc:%s", erp_id, str(ex))

    return commission


def sync_commissions_from_wowiport_regular() -> dict[str, int]:
    service = WowiportService()

    status_id = settings.get(SettingKeys.WOWI_APPROVED_STATUS_ID)
    date_from, date_to = current_business_year_range()
    date_field = settings.get(SettingKeys.SYNC_COMMISSION_DATE_FIELD)

    craftsman_erp_ids = [
        row[0]
        for row in db.session.query(User.craftsman_erp_id)
        .filter(
            User.is_active_flag.is_(True),
            User.craftsman_erp_id.isnot(None),
        )
        .distinct()
        .all()
    ]

    result = {
        "craftsmen": len(craftsman_erp_ids),
        "seen": 0,
        "synced": 0,
        "ignored_status": 0,
        "ignored_business_year": 0,
        "ignored_unknown_craftsman": 0,
        "deleted_marked": 0,
        "errors": 0,
    }

    synced_erp_ids = set()
    limit = 100

    for craftsman_erp_id in craftsman_erp_ids:
        offset = 0

        while True:
            current_app.logger.info(
                "regular_commission_sync_page craftsman_erp_id=%s offset=%s limit=%s status_id=%s date_from=%s date_to=%s",
                craftsman_erp_id,
                offset,
                limit,
                status_id,
                date_from.date(),
                date_to.date(),
            )

            try:
                payloads = service.get_commissions_for_regular_sync(
                    status_id=status_id,
                    craftsman_erp_id=craftsman_erp_id,
                    date_from=date_from,
                    date_to=date_to,
                    limit=limit,
                    offset=offset,
                )
            except Exception:
                current_app.logger.exception(
                    "regular_commission_sync_page_failed craftsman_erp_id=%s offset=%s",
                    craftsman_erp_id,
                    offset,
                )
                result["errors"] += 1
                break

            if not payloads:
                break

            for payload in payloads:
                result["seen"] += 1

                try:
                    sync_result = sync_commission_payload_from_erp(
                        payload=payload,
                        approved_status_id=status_id,
                        date_from=date_from,
                        date_to=date_to,
                        date_field=date_field,
                        allowed_craftsman_erp_ids=set(craftsman_erp_ids),
                        service=service,
                    )

                    erp_id = getattr(payload, "id_", None)
                    if sync_result == "synced" and erp_id is not None:
                        synced_erp_ids.add(int(erp_id))

                    if sync_result == "synced":
                        result["synced"] += 1
                    elif sync_result == "ignored_status":
                        result["ignored_status"] += 1
                    elif sync_result == "ignored_business_year":
                        result["ignored_business_year"] += 1
                    elif sync_result == "ignored_unknown_craftsman":
                        result["ignored_unknown_craftsman"] += 1
                    elif sync_result == "deleted_marked":
                        result["deleted_marked"] += 1

                except Exception:
                    current_app.logger.exception(
                        "regular_commission_sync_item_failed erp_id=%s",
                        getattr(payload, "id_", None),
                    )
                    result["errors"] += 1

            db.session.commit()

            if len(payloads) < limit:
                break

            offset += limit

    mark_missing_regular_sync_commissions_deleted(
        date_from=date_from,
        date_to=date_to,
        date_field=date_field,
        allowed_craftsman_erp_ids=set(craftsman_erp_ids),
        synced_erp_ids=synced_erp_ids,
        result=result,
    )

    db.session.commit()
    return result


def sync_commission_payload_from_erp(
    payload: wowipy.CommissioningCommission,
    approved_status_id: int,
    date_from: datetime,
    date_to: datetime,
    date_field: str,
    allowed_craftsman_erp_ids: set[int],
    service: WowiportService,
) -> str:
    erp_id = int(payload.id_)

    status_id = payload.commission_status.id_ if payload.commission_status else None
    existing = db.session.query(Commission).filter_by(erp_id=erp_id).one_or_none()

    craftsman_erp_id = None
    if payload.craftsman is not None:
        craftsman_erp_id = payload.craftsman.id_

    if craftsman_erp_id not in allowed_craftsman_erp_ids:
        if existing is not None:
            mark_commission_deleted(erp_id)
            return "deleted_marked"
        return "ignored_unknown_craftsman"

    if status_id != approved_status_id:
        if existing is not None:
            mark_commission_deleted(erp_id)
            return "deleted_marked"
        return "ignored_status"

    commission_date = get_commission_business_year_date(payload, date_field)

    if commission_date is None or not (date_from <= commission_date < date_to):
        if existing is not None:
            mark_commission_deleted(erp_id)
            return "deleted_marked"
        return "ignored_business_year"

    commission = upsert_commission(payload)

    try:
        context = service.get_commission_context(erp_id, payload)
        apply_commission_context(commission, context)
    except Exception:
        current_app.logger.exception("commission_context_sync_failed erp_id=%s", erp_id)

    return "synced"


def current_business_year_range() -> tuple[datetime, datetime]:
    start_month = settings.get(SettingKeys.SYNC_BUSINESS_YEAR_START_MONTH)

    now = datetime.now()

    if now.month >= start_month:
        start = datetime(now.year, start_month, 1)
        end = datetime(now.year + 1, start_month, 1)
    else:
        start = datetime(now.year - 1, start_month, 1)
        end = datetime(now.year, start_month, 1)

    return _as_naive_datetime(start), _as_naive_datetime(end)


def get_commission_business_year_date(
    payload: wowipy.CommissioningCommission,
    date_field: str,
) -> datetime | None:
    if date_field == "release_date":
        return _as_naive_datetime(parse_datetime(payload.release_date))

    if date_field == "execution_from":
        return _as_naive_datetime(parse_datetime(payload.execution_from))

    return _as_naive_datetime(parse_datetime(payload.recording_date))


def mark_missing_regular_sync_commissions_deleted(
    date_from: datetime,
    date_to: datetime,
    date_field: str,
    allowed_craftsman_erp_ids: set[int],
    synced_erp_ids: set[int],
    result: dict[str, int],
) -> None:
    if not allowed_craftsman_erp_ids:
        return

    query = Commission.query.filter(
        Commission.local_status != CommissionLocalStatus.DELETED.value,
        Commission.craftsman_erp_id.in_(allowed_craftsman_erp_ids),
    )

    if synced_erp_ids:
        query = query.filter(Commission.erp_id.notin_(synced_erp_ids))

    if date_field == "release_date":
        query = query.filter(
            Commission.release_date >= date_from,
            Commission.release_date < date_to,
        )
    elif date_field == "execution_from":
        query = query.filter(
            Commission.execution_from >= date_from,
            Commission.execution_from < date_to,
        )
    else:
        query = query.filter(
            Commission.recording_date >= date_from,
            Commission.recording_date < date_to,
        )

    approved_status_id = settings.get(SettingKeys.WOWI_APPROVED_STATUS_ID)

    for commission in query.all():
        if commission.status_id == approved_status_id:
            commission.local_status = CommissionLocalStatus.DELETED.value
            commission.deleted_at = utc_now()
            commission.synced_at = utc_now()
            result["deleted_marked"] += 1


def upsert_commission(payload: wowipy.models.CommissioningCommission) -> Commission:
    erp_id = int(payload.id_)
    commission = db.session.query(Commission).filter_by(erp_id=erp_id).one_or_none()

    if commission is None:
        commission = Commission(erp_id=erp_id)
        db.session.add(commission)

    craftsman = getattr(payload, "craftsman", None)
    craftsman_erp_id = getattr(craftsman, "id_", None)

    user = (
        db.session.query(User)
        .filter_by(craftsman_erp_id=craftsman_erp_id, is_active_flag=True)
        .one_or_none()
        if craftsman_erp_id is not None
        else None
    )

    commission_items = list(getattr(payload, "commission_items", None) or [])
    first_item = commission_items[0] if commission_items else None
    commission_status = getattr(payload, "commission_status", None)

    commission.erp_idnum = getattr(payload, "id_num", None)
    commission.code = getattr(payload, "code", None)

    commission.title = _first_text(
        getattr(first_item, "internal_description", None),
        getattr(first_item, "code", None),
        getattr(payload, "short_description_crafts_process", None),
        getattr(payload, "description", None),
        getattr(payload, "id_num", None),
        f"Auftrag {erp_id}",
    )

    commission.description = _first_text(
        getattr(first_item, "commission_text", None),
        getattr(first_item, "internal_description", None),
        getattr(payload, "description", None),
        getattr(payload, "short_description_crafts_process", None),
    )

    commission.damage_location = getattr(payload, "damage_location", None)

    commission.contact_person = _first_text(
        getattr(payload, "contact_person_on_site", None),
        getattr(payload, "contact_person_engineering", None),
    )

    commission.status_id = getattr(commission_status, "id_", None)
    commission.status_code = getattr(commission_status, "code", None)
    commission.craftsman_erp_id = craftsman_erp_id
    commission.user = user

    commission.recording_date = parse_datetime(getattr(payload, "recording_date", None))
    commission.release_date = parse_datetime(getattr(payload, "release_date", None))
    commission.execution_from = parse_datetime(getattr(payload, "execution_from", None))
    commission.execution_to = parse_datetime(getattr(payload, "execution_to", None))

    commission.deleted_at = None
    commission.synced_at = utc_now()

    if not commission.local_status:
        commission.local_status = CommissionLocalStatus.OPEN.value

    if commission.local_status == CommissionLocalStatus.DELETED.value:
        commission.local_status = CommissionLocalStatus.OPEN.value

    return commission


def apply_commission_context(commission: Commission, context: dict[str, Any]) -> None:
    use_unit_id = _as_int(context.get("use_unit_id"))
    license_agreement_id = _as_int(context.get("license_agreement_id"))

    if use_unit_id:
        commission.use_unit_id = use_unit_id

    if license_agreement_id:
        commission.license_agreement_id = license_agreement_id

    if context.get("apartment_address"):
        commission.apartment_address = context["apartment_address"]

    if context.get("apartment_location"):
        commission.apartment_location = context["apartment_location"]

    if context.get("tenant_name"):
        commission.tenant_name = context["tenant_name"]


def mark_commission_deleted(erp_id: int) -> Commission | None:
    commission = db.session.query(Commission).filter_by(erp_id=erp_id).one_or_none()

    if commission is None:
        return None

    commission.local_status = CommissionLocalStatus.DELETED.value
    commission.deleted_at = utc_now()
    commission.synced_at = utc_now()
    return commission


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


def _first_text(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue

        text = str(value).strip()

        if text:
            return text

    return None


def _join_text(*values: Any, separator: str = " ") -> str | None:
    items = []

    for value in values:
        if value is None:
            continue

        text = str(value).strip()

        if text:
            items.append(text)

    if not items:
        return None

    return separator.join(items)


def _join_unique_text(*values: Any, separator: str = " · ") -> str | None:
    items = []

    for value in values:
        if value is None:
            continue

        text = str(value).strip()

        if text and text not in items:
            items.append(text)

    if not items:
        return None

    return separator.join(items)


def _prepare_comment_for_display(value: Any) -> str | None:
    if not value:
        return None

    return " ".join(str(value).split())


def _as_naive_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None

    if value.tzinfo is None:
        return value

    return value.astimezone().replace(tzinfo=None)
