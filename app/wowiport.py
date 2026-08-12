from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
import base64

from datetime import datetime
from typing import Any

from flask import current_app
from wowipy import wowipy
from wowipy.models import CommissioningCommission, Communication, Contractor, LicenseAgreement, UseUnit
from wowipy.wowipy import WowiPy
from wowipy.models import Result

from .extensions import db
from .models import (
    Commission,
    CommissionLocalStatus,
    CommissionPosition,
    CommissionSource,
    CommissionSyncState,
    User,
    WowiCommissionType,
    utc_now,
    WowiCompanyCode,
    CommissionPhoto,
    PushEventType,
)
from .settings import SettingKeys, settings
from .utils import parse_datetime
from .document_sync import sync_commission_documents
from .push_service import enqueue_commission_push
from .photo_service import commission_photo_path

ERP_LOCAL_STATUS = {
    12: CommissionLocalStatus.WAITING_FOR_ACCEPTANCE.value,
    7: CommissionLocalStatus.IN_PROGRESS.value,
    9: CommissionLocalStatus.DONE.value,
    10: CommissionLocalStatus.COMPLETION_REVIEW.value,
    11: CommissionLocalStatus.TENANT_CHARGE_REVIEW.value,
}


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

    def get_media_rows(
            self,
            entity_name: str,
            entity_id: int | None = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        limit = 100

        while True:
            parameters = {
                "showNullValues": "true",
                "limit": limit,
                "offset": offset,
            }

            if entity_id is not None:
                parameters["entityId"] = (
                    entity_id
                )

            result = (
                self.client
                ._rest_adapter
                .get(
                    endpoint=(
                        f"MediaRead/"
                        f"{entity_name}/"
                        f"MediaData"
                    ),
                    ep_params=parameters,
                    force_refresh=True,
                )
            )

            page_rows = self._result_rows(
                result
            )
            rows.extend(page_rows)

            if len(page_rows) < limit:
                break

            offset += limit

        return rows

    def download_media_content(
            self,
            entity_name: str,
            file_guid: str,
    ) -> bytes:
        result = self.client._rest_adapter.get(
            endpoint=(
                f"MediaRead/{entity_name}/"
                f"MediaContent/{file_guid}"
            ),
            force_refresh=True,
        )

        encoded_data = getattr(
            result,
            "data",
            None,
        )

        if not isinstance(encoded_data, str):
            raise RuntimeError(
                "OPENWOWI hat keinen gültigen Bildinhalt geliefert."
            )

        try:
            return base64.b64decode(
                encoded_data
            )
        except (ValueError, TypeError) as exc:
            raise RuntimeError(
                "Der OPENWOWI-Bildinhalt ist ungültig."
            ) from exc

    def get_document_rows(
            self,
            entity_name: str,
            entity_id: int,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        limit = 100

        while True:
            result = self.client._rest_adapter.get(
                endpoint=(
                    f"DocumentRead/{entity_name}/"
                    f"FileData"
                ),
                ep_params={
                    "entityId": entity_id,
                    "includeRevision": "false",
                    "showNullValues": "true",
                    "limit": limit,
                    "offset": offset,
                },
                force_refresh=True,
            )

            page_rows = self._result_rows(
                result
            )
            rows.extend(page_rows)

            if len(page_rows) < limit:
                break

            offset += limit

        return rows

    def upload_commission_photo(
            self,
            commission: Commission,
            photo: CommissionPhoto,
    ) -> dict[str, Any]:
        if commission.erp_id is None:
            raise RuntimeError(
                "Der Auftrag besitzt noch keine OPENWOWI-ID."
            )

        external_document_id = str(
            photo.external_document_id or ""
        ).strip()

        if not external_document_id:
            raise RuntimeError(
                "Das Foto besitzt keine externe Dokument-ID."
            )

        def row_value(
                row: dict[str, Any],
                *keys: str,
        ) -> Any:
            for key in keys:
                if key in row:
                    return row[key]

            return None

        def find_existing() -> dict[str, Any] | None:
            rows = self.get_document_rows(
                "Commission",
                commission.erp_id,
            )

            for row in rows:
                row_external_id = row_value(
                    row,
                    "ExternalDocumentId",
                    "externalDocumentId",
                    "external_document_id",
                )

                if (
                        str(row_external_id or "").strip()
                        == external_document_id
                ):
                    return row

            return None

        existing_row = find_existing()

        if existing_row is not None:
            return existing_row

        path = commission_photo_path(
            photo.storage_name
        )

        if not path.is_file():
            raise RuntimeError(
                "Die lokale Fotodatei wurde nicht gefunden."
            )

        content = path.read_bytes()

        if not content:
            raise RuntimeError(
                "Die lokale Fotodatei ist leer."
            )

        file_type_id = settings.get(
            SettingKeys.WOWI_DOCUMENT_FILE_TYPE_ID
        )
        privacy_category_id = settings.get(
            SettingKeys.WOWI_DOCUMENT_PRIVACY_CATEGORY_ID
        )

        if file_type_id <= 0:
            raise RuntimeError(
                "Es ist keine gültige Wowiport-Dateiart konfiguriert."
            )

        if privacy_category_id <= 0:
            raise RuntimeError(
                "Es ist keine gültige Datenschutzkategorie konfiguriert."
            )

        creation_date = photo.created_at or utc_now()

        if creation_date.tzinfo is None:
            creation_date = creation_date.replace(
                tzinfo=timezone.utc
            )
        else:
            creation_date = creation_date.astimezone(
                timezone.utc
            )

        payload = {
            "Filename": photo.original_filename,
            "CreationDate": creation_date.strftime(
                "%Y-%m-%dT%H:%M:%S"
            ),
            "ExternalDocumentId": external_document_id,
            "FileTypeId": file_type_id,
            "DataPrivacyCategoryId": privacy_category_id,
            "EntityId": commission.erp_id,
            "Contents": base64.b64encode(
                content
            ).decode("ascii"),
            "Sha1Hash": hashlib.sha1(
                content
            ).hexdigest(),
        }

        result = self.client._rest_adapter.post(
            endpoint="DocumentEdit/Commission/File",
            data=payload,
        )

        result_data = getattr(
            result,
            "data",
            None,
        )

        if (
                isinstance(result_data, list)
                and result_data
                and isinstance(result_data[0], dict)
        ):
            return result_data[0]

        if isinstance(result_data, dict):
            return result_data

        existing_row = find_existing()

        if existing_row is not None:
            return existing_row

        raise RuntimeError(
            "OPENWOWI hat das Foto nicht mit Dokumentdaten bestätigt."
        )

    def download_document_content(
            self,
            entity_name: str,
            file_guid: str,
    ) -> bytes:
        result = self.client._rest_adapter.get(
            endpoint=(
                f"DocumentRead/{entity_name}/"
                f"FileContent/{file_guid}"
            ),
            force_refresh=True,
        )

        encoded_data = getattr(
            result,
            "data",
            None,
        )

        if not isinstance(encoded_data, str):
            raise RuntimeError(
                "OPENWOWI hat keinen gültigen Dateiinhalt geliefert."
            )

        try:
            return base64.b64decode(
                encoded_data
            )
        except (ValueError, TypeError) as exc:
            raise RuntimeError(
                "Der OPENWOWI-Dateiinhalt ist ungültig."
            ) from exc

    def get_commission_ticket_rows(
            self,
            commission_erp_id: int,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        limit = 100

        while True:
            result = self.client._rest_adapter.get(
                endpoint=(
                    "CommunicationRead/Ticket"
                ),
                ep_params={
                    "ticketAssignmentEntityId": 7,
                    "ticketAssignmentEntityValueEntityId": (
                        commission_erp_id
                    ),
                    "includeAssignmentEntity": "true",
                    "includeComments": "false",
                    "showNullValues": "true",
                    "limit": limit,
                    "offset": offset,
                },
                force_refresh=True,
            )

            page_rows = self._result_rows(
                result
            )
            rows.extend(page_rows)

            if len(page_rows) < limit:
                break

            offset += limit

        return rows

    def mark_commission_done(self, erp_id: int) -> None:
        self.client.set_commissioning_commission_to_done(commission_id=erp_id)

    def mark_commission_refused(
            self,
            erp_id: int,
            reason: str,
    ) -> None:
        self.client.set_commissioning_commission_to_refused(
            commission_id=erp_id,
            reason_for_refusal=reason,
        )

    def mark_commission_canceled(
            self,
            erp_id: int,
    ) -> None:
        self.client.set_commissioning_commission_to_canceled(
            commission_id=erp_id,
        )

    @staticmethod
    def _row_value(row: dict[str, Any], *names: str) -> Any:
        normalized = {
            str(key).replace("_", "").lower(): value
            for key, value in row.items()
        }

        for name in names:
            value = normalized.get(name.replace("_", "").lower())
            if value is not None:
                return value

        return None

    @staticmethod
    def _result_rows(result: Any) -> list[dict[str, Any]]:
        data = getattr(result, "data", result)

        if isinstance(data, dict):
            rows = [data]

            for key in ("CraftsProcess", "Commission", "Data"):
                nested = data.get(key)
                if isinstance(nested, dict):
                    rows.append(nested)

            return rows

        if isinstance(data, list):
            return [
                row
                for row in data
                if isinstance(row, dict)
            ]

        if isinstance(data, int):
            return [{"Id": data}]

        return []

    def _identity_from_rows(
            self,
            rows: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        for row in rows:
            raw_id = self._row_value(
                row,
                "Id",
                "CraftsProcessId",
                "CommissionId",
            )

            try:
                erp_id = int(raw_id)
            except (TypeError, ValueError):
                continue

            if erp_id <= 0:
                continue

            raw_idnum = self._row_value(
                row,
                "IdNum",
                "CraftsProcessIdNum",
                "CommissionIdNum",
            )

            return {
                "id": erp_id,
                "id_num": str(raw_idnum) if raw_idnum else None,
            }

        return None

    def _force_read_rows(
            self,
            endpoint: str,
            params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        result = self.client._rest_adapter.get(
            endpoint=endpoint,
            ep_params=params,
            force_refresh=True,
        )

        if isinstance(result.data, dict):
            return [result.data]

        if isinstance(result.data, list):
            return [
                row
                for row in result.data
                if isinstance(row, dict)
            ]

        return []

    def read_all_rows(
            self,
            endpoint: str,
            params: dict[str, Any] | None = None,
            limit: int = 100,
            paginate: bool = True,
    ) -> list[dict[str, Any]]:
        rows = []
        offset = 0
        base_params = dict(params or {})
        previous_page = None

        if not paginate:
            return self._force_read_rows(
                endpoint,
                base_params,
            )

        while True:
            page_params = {
                **base_params,
                "limit": limit,
                "offset": offset,
            }
            page = self._force_read_rows(
                endpoint,
                page_params,
            )

            if previous_page == page:
                raise RuntimeError(
                    "OpenWowi-Paginierung wiederholt "
                    f"Seite für {endpoint}"
                )

            rows.extend(page)

            if len(page) < limit:
                break

            offset += len(page)
            previous_page = page

        return rows

    def _resolve_sales_tax_id(
            self,
            company_code_id: int,
            sales_tax_code: str,
    ) -> int:
        normalized_code = sales_tax_code.strip()

        commissions = self._force_read_rows(
            "CommissioningRead/Commissions",
            {
                "companyCodeId": company_code_id,
                "limit": 100,
                "offset": 0,
                "includeCommissionItems": "true",
                "showNullValues": "true",
            },
        )

        matching_ids = set()

        for commission in commissions:
            positions = (
                    commission.get("CommissionItems")
                    or []
            )

            for position in positions:
                sales_tax = position.get("SalesTax")

                if not isinstance(sales_tax, dict):
                    continue

                code = str(
                    self._row_value(
                        sales_tax,
                        "Code",
                    )
                    or ""
                ).strip()

                if code.casefold() != normalized_code.casefold():
                    continue

                raw_id = self._row_value(
                    sales_tax,
                    "Id",
                )

                try:
                    matching_ids.add(int(raw_id))
                except (TypeError, ValueError):
                    continue

        if not matching_ids:
            raise RuntimeError(
                f"Der Umsatzsteuercode {normalized_code} wurde im Buchwerk nicht gefunden"
            )

        if len(matching_ids) > 1:
            raise RuntimeError(
                f"Der Umsatzsteuercode {normalized_code} ist in Wowiport nicht eindeutig"
            )

        return matching_ids.pop()

    def _resolve_creditor_id(
            self,
            commission: Commission,
            company_code_id: int,
    ) -> int:
        craftsman_id = getattr(
            commission,
            "craftsman_erp_id",
            None,
        )

        if not craftsman_id:
            assigned_user = getattr(
                commission,
                "user",
                None,
            )
            craftsman_id = getattr(
                assigned_user,
                "craftsman_erp_id",
                None,
            )

        if not craftsman_id:
            raise RuntimeError(
                "Dem Auftrag ist kein Wowiport-Handwerker zugeordnet"
            )

        craftsmen = (
            self.client.get_commissioning_craftsmen(
                company_code_id=company_code_id,
                craftsman_id=craftsman_id,
                limit=10,
                show_null_values=True,
            )
        )

        matches = [
            craftsman
            for craftsman in craftsmen
            if int(craftsman.id_) == int(craftsman_id)
        ]

        if not matches:
            raise RuntimeError(
                f"Der Wowiport-Handwerker {craftsman_id} wurde im ausgewählten Buchwerk nicht gefunden"
            )

        if len(matches) > 1:
            raise RuntimeError(
                f"Der Wowiport-Handwerker {craftsman_id} ist nicht eindeutig"
            )

        raw_creditor_id = getattr(
            matches[0],
            "creditor_id",
            None,
        )

        try:
            creditor_id = int(raw_creditor_id)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Dem Wowiport-Handwerker {craftsman_id} ist kein Kreditor zugeordnet"
            ) from exc

        if creditor_id <= 0:
            raise RuntimeError(
                f"Dem Wowiport-Handwerker {craftsman_id} ist kein gültiger Kreditor zugeordnet"
            )

        commission.creditor_erp_id = creditor_id

        return creditor_id

    def _resolve_company_code_id(
            self,
            company_code_code: str,
    ) -> int:
        normalized_code = company_code_code.strip()

        rows = self._force_read_rows(
            "AccountancyMasterData/CompanyCodes",
            {
                "companyCodeCode": normalized_code,
                "limit": 10,
                "offset": 0,
                "showNullValues": "true",
            },
        )

        matches = [
            row
            for row in rows
            if str(
                self._row_value(row, "Code")
                or ""
            ).strip()
               == normalized_code
        ]

        if not matches:
            raise RuntimeError(
                f"Das Buchwerk {normalized_code} ist für diesen OPENWOWI-Zugang nicht freigegeben"
            )

        if len(matches) > 1:
            raise RuntimeError(
                f"Das Buchwerk {normalized_code} ist in Wowiport nicht eindeutig"
            )

        raw_id = self._row_value(
            matches[0],
            "Id",
        )

        try:
            company_code_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Wowiport hat für das Buchwerk {normalized_code} keine gültige ID geliefert"
            ) from exc

        return company_code_id

    def _resolve_craft_process_type_id(
            self,
            process_type_code: str,
    ) -> int:
        normalized_code = process_type_code.strip()

        process_types = (
            self.client.get_commissioning_craft_process_types(
                show_null_values=True
            )
        )

        matches = [
            process_type
            for process_type in process_types
            if str(process_type.code or "").strip().casefold()
               == normalized_code.casefold()
        ]

        if not matches:
            raise RuntimeError(
                f"Die Vorgangsart {normalized_code} wurde in Wowiport nicht gefunden"
            )

        if len(matches) > 1:
            raise RuntimeError(
                f"Die Vorgangsart {normalized_code} ist in Wowiport nicht eindeutig"
            )

        return int(matches[0].id_)

    def _resolve_craft_process_status_id(
            self,
            company_code_id: int,
            process_status_code: str,
    ) -> int:
        normalized_code = process_status_code.strip()

        rows = self._force_read_rows(
            "CommissioningRead/CraftProcesses",
            {
                "companyCodeId": company_code_id,
                "limit": 100,
                "offset": 0,
                "showNullValues": "true",
            },
        )

        matching_ids = set()

        for row in rows:
            status = self._row_value(
                row,
                "CraftProcessStatus",
            )

            if not isinstance(status, dict):
                continue

            status_code = str(
                self._row_value(status, "Code")
                or ""
            ).strip()

            if status_code.casefold() != normalized_code.casefold():
                continue

            raw_id = self._row_value(
                status,
                "Id",
            )

            try:
                matching_ids.add(int(raw_id))
            except (TypeError, ValueError):
                continue

        if not matching_ids:
            raise RuntimeError(
                f"Der Vorgangsstatus {normalized_code} wurde für das Buchwerk nicht gefunden"
            )

        if len(matching_ids) > 1:
            raise RuntimeError(
                f"Der Vorgangsstatus {normalized_code} ist in Wowiport nicht eindeutig"
            )

        return matching_ids.pop()

    def _commission_company_code_id(
            self,
            commission: Commission,
    ) -> int:
        raw_payload = commission.raw_payload or {}

        company_code_code = str(
            commission.company_code_code
            or raw_payload.get("company_code_code")
            or ""
        ).strip()

        if not company_code_code:
            raise RuntimeError(
                "Für den Auftrag wurde kein Buchwerk ausgewählt"
            )

        company_code_id = self._resolve_company_code_id(
            company_code_code
        )

        commission.company_code_code = company_code_code
        commission.company_code_erp_id = company_code_id

        return company_code_id

    def _find_crafts_process_by_idnum(
            self,
            id_num: str,
    ) -> dict[str, Any] | None:
        rows = self._force_read_rows(
            "CommissioningRead/CraftProcesses",
            {
                "idNum": id_num,
                "limit": 10,
                "offset": 0,
                "showNullValues": "true",
            },
        )

        matches = [
            row
            for row in rows
            if str(self._row_value(row, "IdNum") or "") == id_num
        ]

        if len(matches) > 1:
            raise RuntimeError(
                f"Mehrere Wowiport-Vorgänge mit IdNum {id_num} gefunden"
            )

        return self._identity_from_rows(matches)

    def _find_crafts_process_by_id(
            self,
            erp_id: int,
    ) -> dict[str, Any] | None:
        rows = self._force_read_rows(
            "CommissioningRead/CraftProcesses",
            {
                "id": erp_id,
                "limit": 10,
                "offset": 0,
                "showNullValues": "true",
            },
        )

        return self._identity_from_rows(rows)

    def _ensure_crafts_process(
            self,
            commission: Commission,
            resolved_company_code_id: int,
            resolved_process_type_id: int,
            resolved_process_status_id: int,
    ) -> int:
        if commission.crafts_process_erp_id:
            existing = self._find_crafts_process_by_id(
                commission.crafts_process_erp_id
            )

            if existing is not None:
                return existing["id"]

            commission.crafts_process_erp_id = None

        resolved_reference_ids = self.resolve_commission_reference_ids(
            commission
        )

        reference_ids = (
            resolved_reference_ids.copy()
            if isinstance(resolved_reference_ids, dict)
            else {}
        )

        reference_ids.setdefault(
            "company_code_id",
            getattr(commission, "company_code_erp_id", None),
        )
        reference_ids.setdefault(
            "economic_unit_id",
            getattr(commission, "economic_unit_erp_id", None),
        )
        reference_ids.setdefault(
            "building_id",
            getattr(commission, "building_erp_id", None)
            or getattr(commission, "building_id", None),
        )
        reference_ids.setdefault(
            "use_unit_id",
            getattr(commission, "use_unit_erp_id", None)
            or getattr(commission, "use_unit_id", None),
        )
        reference_ids.setdefault(
            "license_agreement_id",
            getattr(commission, "license_agreement_erp_id", None)
            or getattr(commission, "license_agreement_id", None),
        )
        short_description = (
                commission.title
                or commission.description
                or "Auftrag aus DatLäuft"
        )[:255]

        process_from = (
                commission.recording_date
                or utc_now()
        )

        process_from_value = (
            process_from.date()
            if isinstance(process_from, datetime)
            else process_from
        )

        result = self.client.create_commissioning_crafts_process(
            short_description_crafts_process=short_description,
            crafts_process_type_id=resolved_process_type_id,
            company_code_id=resolved_company_code_id,
            crafts_process_status_id=resolved_process_status_id,
            economic_unit_id=reference_ids.get("economic_unit_id"),
            building_id=reference_ids.get("building_id"),
            use_unit_id=reference_ids.get("use_unit_id"),
            license_agreement_id=reference_ids.get(
                "license_agreement_id"
            ),
            crafts_process_from=process_from_value,
            description=commission.description,
        )

        identity = self._identity_from_rows(
            self._result_rows(result)
        )

        if identity is None:
            raise RuntimeError(
                "Wowiport hat den Vorgang angelegt, aber keine Vorgangs-ID zurückgegeben"
            )

        commission.crafts_process_erp_id = (
            identity["id"]
        )
        db.session.commit()

        return identity["id"]

    def _find_existing_commission(
            self,
            crafts_process_id: int,
            external_identification_number: str,
    ) -> dict[str, Any] | None:
        rows = self._force_read_rows(
            "CommissioningRead/Commissions",
            {
                "craftsProcessId": crafts_process_id,
                "limit": 100,
                "offset": 0,
                "showNullValues": "true",
                "includeCommissionDetails": "true",
            },
        )

        matches = [
            row
            for row in rows
            if str(
                self._row_value(
                    row,
                    "ExternalIdentificationNumber",
                )
                or ""
            )
               == external_identification_number
        ]

        if len(matches) > 1:
            raise RuntimeError(
                "Mehrere Wowiport-Aufträge mit derselben externen Kennung gefunden"
            )

        return self._identity_from_rows(matches)

    def _commission_positions_payload(
            self,
            commission: Commission,
            resolved_sales_tax_id: int,
    ) -> list[dict[str, Any]]:
        if not commission.positions:
            raise RuntimeError(
                "Der Auftrag enthält keine Auftragsposition"
            )

        position_payloads = []

        for position_index, position in enumerate(
                commission.positions,
                start=1,
        ):
            payload = dict(
                position.to_erp_payload()
            )

            commission_text = str(
                payload.get("CommissionText")
                or getattr(
                    position,
                    "commission_text",
                    None,
                )
                or getattr(
                    position,
                    "description",
                    None,
                )
                or ""
            ).strip()

            if not commission_text:
                raise RuntimeError(
                    f"Auftragsposition {position_index} enthält keinen Auftragstext"
                )

            raw_units = (
                payload.get("Units")
                if payload.get("Units") is not None
                else getattr(position, "units", 1)
            )

            try:
                units = float(raw_units)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"Auftragsposition {position_index} enthält keine gültige Menge"
                ) from exc

            if units <= 0:
                raise RuntimeError(
                    f"Auftragsposition {position_index} muss eine positive Menge besitzen"
                )

            raw_unit_price = payload.get(
                "UnitPrice"
            )
            raw_net_amount = payload.get(
                "NetAmount"
            )
            raw_gross_amount = payload.get(
                "GrossAmount"
            )

            payload["GrossAmount"] = float(
                raw_gross_amount
                if raw_gross_amount is not None
                else 0.0
            )
            payload["SalesTaxId"] = (
                resolved_sales_tax_id
            )
            payload["TransferToCraftsmanPortalWithoutCosts"] = True

            payload["CommissionText"] = commission_text
            payload["Units"] = units
            payload["PositionNumber"] = (
                    payload.get("PositionNumber")
                    or position_index
            )
            payload["UnitPrice"] = float(
                raw_unit_price
                if raw_unit_price is not None
                else 0.0
            )
            payload["NetAmount"] = float(
                raw_net_amount
                if raw_net_amount is not None
                else 0.0
            )

            service_catalogue_id = (
                    payload.get("ServiceCatalogueId")
                    or payload.get("ServiceCatalogId")
                    or getattr(
                position,
                "service_catalog_erp_id",
                None,
            )
            )
            craft_activity_id = (
                    payload.get("CraftActivityId")
                    or getattr(
                position,
                "craft_activity_erp_id",
                None,
            )
            )

            try:
                service_catalogue_id = int(
                    service_catalogue_id
                )
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"Auftragsposition {position_index} enthält keine Leistungskatalogposition"
                ) from exc

            try:
                craft_activity_id = int(
                    craft_activity_id
                )
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"Auftragsposition {position_index} enthält kein Gewerk"
                ) from exc

            if service_catalogue_id <= 0:
                raise RuntimeError(
                    f"Auftragsposition {position_index} enthält keine gültige Leistungskatalogposition"
                )

            if craft_activity_id <= 0:
                raise RuntimeError(
                    f"Auftragsposition {position_index} enthält kein gültiges Gewerk"
                )

            payload.pop(
                "ServiceCatalogueId",
                None,
            )
            payload["ServiceCatalogId"] = (
                service_catalogue_id
            )
            payload["CraftActivityId"] = (
                craft_activity_id
            )

            position_payloads.append(payload)

        return position_payloads

    def create_commission_waiting_for_acceptance(
            self,
            commission: Commission,
    ) -> Any:
        raw_payload = commission.raw_payload or {}

        process_type_code = str(
            commission.crafts_process_type_code
            or raw_payload.get("crafts_process_type_code")
            or settings.get(
                SettingKeys.WOWI_DEFAULT_CRAFTS_PROCESS_TYPE_CODE
            )
            or ""
        ).strip()

        process_status_code = str(
            commission.crafts_process_status_code
            or raw_payload.get("crafts_process_status_code")
            or settings.get(
                SettingKeys.WOWI_INITIAL_CRAFTS_PROCESS_STATUS_CODE
            )
            or ""
        ).strip()

        sales_tax_code = str(
            commission.sales_tax_code
            or raw_payload.get("sales_tax_code")
            or settings.get(
                SettingKeys.WOWI_INTERNAL_SALES_TAX_CODE
            )
            or ""
        ).strip()

        if not sales_tax_code:
            raise RuntimeError(
                "Für den Auftrag wurde kein Umsatzsteuercode ausgewählt"
            )

        if not process_type_code:
            raise RuntimeError(
                "Für den Auftrag wurde keine Vorgangsart ausgewählt"
            )

        if not process_status_code:
            raise RuntimeError(
                "Für den Auftrag wurde kein Vorgangsstatus ausgewählt"
            )

        resolved_company_code_id = (
            self._commission_company_code_id(
                commission
            )
        )

        resolved_sales_tax_id = (
            self._resolve_sales_tax_id(
                resolved_company_code_id,
                sales_tax_code,
            )
        )
        resolved_creditor_id = self._resolve_creditor_id(
            commission,
            resolved_company_code_id,
        )
        resolved_process_type_id = (
            self._resolve_craft_process_type_id(
                process_type_code
            )
        )
        resolved_process_status_id = (
            self._resolve_craft_process_status_id(
                resolved_company_code_id,
                process_status_code,
            )
        )

        crafts_process_id = self._ensure_crafts_process(
            commission,
            resolved_company_code_id,
            resolved_process_type_id,
            resolved_process_status_id,
        )

        external_identification_number = str(
            commission.erp_external_identification_number
            or commission.public_id
        )

        commission.erp_external_identification_number = (
            external_identification_number
        )

        existing_commission = self._find_existing_commission(
            crafts_process_id,
            external_identification_number,
        )
        commission.sales_tax_code = sales_tax_code
        commission.sales_tax_erp_id = resolved_sales_tax_id
        commission.crafts_process_type_code = process_type_code
        commission.crafts_process_type_erp_id = (
            resolved_process_type_id
        )
        commission.crafts_process_status_code = (
            process_status_code
        )
        commission.crafts_process_status_erp_id = (
            resolved_process_status_id
        )

        if existing_commission is not None:
            return Result(
                200,
                message="Auftrag bereits vorhanden",
                data={
                    "Id": existing_commission["id"],
                    "IdNum": existing_commission["id_num"],
                },
            )

        recording_date = (
            commission.recording_date.date()
            if commission.recording_date
            else utc_now().date()
        )
        execution_from = (
            commission.execution_from.date()
            if commission.execution_from
            else None
        )
        execution_to = (
            commission.execution_to.date()
            if commission.execution_to
            else None
        )

        position_payloads = (
            self._commission_positions_payload(
                commission,
                resolved_sales_tax_id,
            )
        )

        result = self.client.create_commissioning_commission_wait_for_craftsman_feedback(
            crafts_process_id=crafts_process_id,
            external_identification_number=external_identification_number,
            commission_type_id=commission.commission_type_erp_id,
            creditor_id=resolved_creditor_id,
            economic_unit_id=commission.economic_unit_erp_id,
            use_unit_id=commission.use_unit_id,
            building_id=commission.building_erp_id,
            license_agreement_id=commission.license_agreement_id,
            responsible_official_repair_id=commission.responsible_official_repair_erp_id,
            department_id=commission.department_erp_id,
            recording_date=recording_date,
            execution_from=execution_from,
            execution_to=execution_to,
            positions=position_payloads,
            short_description_crafts_process=commission.title,
        )

        identity = self._identity_from_rows(
            self._result_rows(result)
        )

        if identity is None:
            identity = self._find_existing_commission(
                crafts_process_id,
                external_identification_number,
            )

        if identity is None:
            raise RuntimeError(
                "Wowiport hat den Auftrag angelegt, aber keine Auftrags-ID zurückgegeben"
            )

        return Result(
            getattr(result, "status_code", 200),
            message=getattr(
                result,
                "message",
                "Auftrag angelegt",
            ),
            data={
                "Id": identity["id"],
                "IdNum": identity["id_num"],
            },
        )

    def mark_commission_accepted(self, erp_id: int) -> None:
        self.client.set_commissioning_commission_to_accepted(commission_id=erp_id)

    def mark_commission_await_invoice(self, erp_id: int) -> None:
        self.client.set_commissioning_commission_to_await_invoice(commission_id=erp_id)

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

    def resolve_commission_reference_ids(
            self,
            commission: Commission,
    ) -> None:
        license_agreement = None
        use_unit = None
        building = None

        if commission.license_agreement_idnum:
            license_agreements = (
                self.client.get_license_agreements(
                    license_agreement_idnum=(
                        commission.license_agreement_idnum
                    ),
                    limit=2,
                )
            )
            license_agreement = _single_idnum_match(
                license_agreements,
                commission.license_agreement_idnum,
                "Mietvertrag",
            )
            license_agreement_id = _as_int(
                _attr(license_agreement, "id_")
            )
            commission.license_agreement_id = (
                _merge_reference_id(
                    commission.license_agreement_id,
                    license_agreement_id,
                    "Mietvertrag",
                )
            )

            agreement_use_unit = _attr(
                license_agreement,
                "use_unit",
            )
            agreement_use_unit_id = _as_int(
                _attr(agreement_use_unit, "id_")
            )
            commission.use_unit_id = _merge_reference_id(
                commission.use_unit_id,
                agreement_use_unit_id,
                "Nutzungseinheit des Mietvertrags",
            )

            if not commission.use_unit_idnum:
                commission.use_unit_idnum = _clean_id_num(
                    _attr(agreement_use_unit, "id_num")
                )

        if commission.use_unit_idnum:
            use_units = self.client.get_use_units(
                use_unit_idnum=commission.use_unit_idnum,
                limit=2,
            )
            use_unit = _single_idnum_match(
                use_units,
                commission.use_unit_idnum,
                "Nutzungseinheit",
            )
            use_unit_id = _as_int(
                _attr(use_unit, "id_")
            )
            commission.use_unit_id = _merge_reference_id(
                commission.use_unit_id,
                use_unit_id,
                "Nutzungseinheit",
            )

            use_unit_building = _attr(
                use_unit,
                "building_land",
            )
            use_unit_building_id = _as_int(
                _attr(use_unit_building, "id_")
            )
            commission.building_erp_id = (
                _merge_reference_id(
                    commission.building_erp_id,
                    use_unit_building_id,
                    "Gebäude der Nutzungseinheit",
                )
            )

            if not commission.building_idnum:
                commission.building_idnum = _clean_id_num(
                    _attr(use_unit_building, "id_num")
                )

        if commission.building_idnum:
            buildings = self.client.get_building_lands(
                building_land_idnum=(
                    commission.building_idnum
                ),
                limit=2,
            )
            building = _single_idnum_match(
                buildings,
                commission.building_idnum,
                "Gebäude",
            )
            building_id = _as_int(
                _attr(building, "id_")
            )
            commission.building_erp_id = (
                _merge_reference_id(
                    commission.building_erp_id,
                    building_id,
                    "Gebäude",
                )
            )

        if not any(
                [
                    commission.use_unit_id,
                    commission.building_erp_id,
                    commission.license_agreement_id,
                ]
        ):
            raise RuntimeError(
                "Die Objektangabe konnte nicht in eine "
                "OPENWOWI-ID aufgelöst werden."
            )

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

    def get_craftsmen_for_user_sync(
            self,
    ) -> list[dict[str, Any]]:
        rows = []
        limit = 100
        offset = 0

        while True:
            page = self._force_read_rows(
                "CommissioningRead/Craftsman",
                {
                    "limit": limit,
                    "offset": offset,
                    "includeMainCommunication": "true",
                    "includePersonCommunications": "true",
                    "includeCraftsmanAccessibility": "true",
                    "showNullValues": "true",
                },
            )

            if not page:
                break

            rows.extend(page)

            if len(page) < limit:
                break

            offset += limit

        return rows

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
            include_commission_items=True,
            include_commission_details=True,
            show_null_values=True,
            add_args=add_args,
        )


def configured_commission_type_ids() -> set[int]:
    return _integer_set(
        settings.get(SettingKeys.WOWI_ALLOWED_COMMISSION_TYPE_IDS)
    )


def configured_commission_status_ids() -> set[int]:
    return _integer_set(
        settings.get(SettingKeys.WOWI_VISIBLE_COMMISSION_STATUS_IDS)
    )


def sync_company_codes_from_wowiport() -> dict[str, int]:
    service = WowiportService()

    rows = service._force_read_rows(
        "AccountancyMasterData/CompanyCodes",
        {
            "limit": 100,
            "offset": 0,
            "showNullValues": "true",
        },
    )

    existing_items = {
        item.erp_id: item
        for item in WowiCompanyCode.query.all()
    }

    seen_ids = set()
    created = 0
    updated = 0

    for row in rows:
        erp_id = _as_int(
            _read_first(row, "Id", "id", "id_")
        )
        code = _first_text(
            _read_first(row, "Code", "code")
        )

        if erp_id is None or not code:
            continue

        name = _first_text(
            _read_first(row, "Name", "name"),
            _read_first(row, "Description", "description"),
            code,
        )

        seen_ids.add(erp_id)
        item = existing_items.get(erp_id)

        if item is None:
            item = WowiCompanyCode(erp_id=erp_id)
            db.session.add(item)
            created += 1
        else:
            updated += 1

        item.code = code
        item.name = name or code
        item.active = True
        item.synced_at = utc_now()

    for erp_id, item in existing_items.items():
        if erp_id not in seen_ids:
            item.active = False
            item.synced_at = utc_now()

    return {
        "seen": len(seen_ids),
        "created": created,
        "updated": updated,
    }


def sync_commission_types_from_wowiport() -> dict[str, int]:
    service = WowiportService()
    payloads = service.client.get_commissioning_commission_types(
        include_commission_control=True,
        show_null_values=True,
    ) or []

    existing_items = {
        item.erp_id: item
        for item in WowiCommissionType.query.all()
    }
    seen_ids = set()
    created = 0
    updated = 0

    for payload in payloads:
        erp_id = int(_read_first(payload, "id_", "id", "Id"))
        seen_ids.add(erp_id)

        item = existing_items.get(erp_id)
        if item is None:
            item = WowiCommissionType(erp_id=erp_id)
            db.session.add(item)
            created += 1
        else:
            updated += 1

        item.code = _first_text(
            _read_first(payload, "code", "Code"),
            _read_first(payload, "name", "Name"),
            str(erp_id),
        )
        item.active = True
        item.synced_at = utc_now()

    for erp_id, item in existing_items.items():
        if erp_id not in seen_ids:
            item.active = False
            item.synced_at = utc_now()

    return {
        "seen": len(seen_ids),
        "created": created,
        "updated": updated,
    }


def refresh_commission_context_safely(
        commission: Commission,
        service: WowiportService,
        erp_id: int,
        payload: Any,
) -> bool:
    previous_context = (
        commission.building_erp_id,
        commission.use_unit_id,
        commission.license_agreement_id,
        commission.apartment_address,
        commission.apartment_location,
        commission.tenant_name,
    )

    try:
        context = service.get_commission_context(
            erp_id=erp_id,
            commission_object=payload,
        )

        apply_commission_context(
            commission,
            context,
        )
    except Exception as exc:
        current_app.logger.exception(
            "commission_context_sync_failed "
            "erp_id=%s exc=%s",
            erp_id,
            str(exc),
        )

        return False

    current_context = (
        commission.building_erp_id,
        commission.use_unit_id,
        commission.license_agreement_id,
        commission.apartment_address,
        commission.apartment_location,
        commission.tenant_name,
    )

    return current_context != previous_context


def sync_commission_documents_safely(
        commission: Commission,
        service: WowiportService,
        force: bool = False,
) -> None:
    try:
        result = sync_commission_documents(
            commission=commission,
            service=service,
            force=force,
        )

        current_app.logger.info(
            "commission_document_sync_completed "
            "commission_id=%s erp_id=%s result=%s",
            commission.id,
            commission.erp_id,
            result,
        )
    except Exception as exc:
        commission.document_sync_error = (
            str(exc)[:4000]
        )

        current_app.logger.exception(
            "commission_document_sync_failed "
            "commission_id=%s erp_id=%s",
            commission.id,
            commission.erp_id,
        )


def enqueue_synced_commission_push(
        commission: Commission,
        existed_before_sync: bool,
        previous_user_id: int | None,
        was_deleted_before_sync: bool,
        had_sync_signature_before_sync: bool,
) -> None:
    if commission.user is None:
        return

    current_user_id = commission.user.id

    if (
            not existed_before_sync
            or was_deleted_before_sync
            or previous_user_id != current_user_id
    ):
        event_type = PushEventType.NEW_COMMISSION
    elif not had_sync_signature_before_sync:
        return
    else:
        event_type = PushEventType.CHANGED_COMMISSION

    enqueue_commission_push(
        commission=commission,
        event_type=event_type,
        user=commission.user,
    )


def sync_commission_from_erp(
        erp_id: int,
) -> Commission | None:
    service = WowiportService()
    payload = service.get_commission(erp_id)

    if payload is None:
        return mark_commission_deleted(erp_id)

    existing = Commission.query.filter_by(
        erp_id=erp_id
    ).one_or_none()
    existed_before_sync = existing is not None
    previous_user_id = existing.user_id if existing is not None else None
    was_deleted_before_sync = bool(
        existing is not None
        and (
                existing.local_status == CommissionLocalStatus.DELETED.value
                or existing.deleted_at is not None
        )
    )
    had_sync_signature_before_sync = bool(
        existing is not None
        and isinstance(existing.raw_payload, dict)
        and existing.raw_payload.get("sync_signature")
    )
    allowed_type_ids = configured_commission_type_ids()
    visible_status_ids = configured_commission_status_ids()

    if not allowed_type_ids or not visible_status_ids:
        current_app.logger.warning(
            "commission_sync_configuration_missing erp_id=%s",
            erp_id,
        )
        return existing

    status_id = _commission_status_id(payload)

    if (
            existing is None
            and status_id == 11
    ):
        current_app.logger.info(
            "commission_sync_await_invoice_not_local "
            "erp_id=%s",
            erp_id,
        )
        return None

    commission_type_id = _commission_type_id(payload)

    if status_id not in visible_status_ids:
        return (
            mark_commission_deleted(erp_id)
            if existing is not None
            else None
        )

    if commission_type_id not in allowed_type_ids:
        return (
            mark_commission_deleted(erp_id)
            if existing is not None
            else None
        )

    sync_signature = _commission_sync_signature(
        payload
    )

    if _commission_is_unchanged(
            existing,
            sync_signature,
    ):
        if existing is not None:
            sync_commission_documents_safely(
                commission=existing,
                service=service,
            )

        return existing

    commission = upsert_commission(
        payload,
        sync_signature=sync_signature,
    )

    refresh_commission_context_safely(
        commission=commission,
        service=service,
        erp_id=erp_id,
        payload=payload,
    )

    sync_commission_documents_safely(
        commission=commission,
        service=service,
        force=True,
    )
    enqueue_synced_commission_push(
        commission=commission,
        existed_before_sync=existed_before_sync,
        previous_user_id=previous_user_id,
        was_deleted_before_sync=was_deleted_before_sync,
        had_sync_signature_before_sync=had_sync_signature_before_sync,
    )

    return commission


def sync_commissions_from_wowiport_regular(
        full: bool = False,
) -> dict[str, int]:
    service = WowiportService()
    from .catalog import (
        sync_craft_activities_from_wowiport,
        sync_service_positions_from_wowiport,
    )
    from .user_sync import (
        sync_users_from_wowiport,
    )

    craft_result = (
        sync_craft_activities_from_wowiport(
            service
        )
    )
    db.session.flush()

    user_result = (
        sync_users_from_wowiport(
            service
        )
    )
    db.session.flush()

    service_position_result = (
        sync_service_positions_from_wowiport(
            service
        )
    )
    db.session.commit()
    visible_status_ids = configured_commission_status_ids()
    allowed_type_ids = configured_commission_type_ids()
    date_field = settings.get(
        SettingKeys.SYNC_COMMISSION_DATE_FIELD
    )

    if full:
        date_from, date_to = (
            current_business_year_range()
        )
    else:
        date_from, date_to = (
            quick_reconciliation_range()
        )

    craftsman_erp_ids = {
        int(row[0])
        for row in db.session.query(User.craftsman_erp_id)
        .filter(
            User.is_active_flag.is_(True),
            User.craftsman_erp_id.isnot(None),
        )
        .distinct()
        .all()
    }

    result = {
        "craftsmen": len(craftsman_erp_ids),
        "statuses": len(visible_status_ids),
        "seen": 0,
        "synced": 0,
        "ignored_status": 0,
        "ignored_type": 0,
        "ignored_business_year": 0,
        "ignored_unknown_craftsman": 0,
        "deleted_marked": 0,
        "errors": 0,
        "configuration_missing": 0,
        "reconciliation_skipped": 0,
        "unchanged": 0,
        "ignored_await_invoice_not_local": 0,
        "user_sync_seen": user_result.seen,
        "users_created": user_result.created,
        "users_updated": user_result.updated,
        "users_unchanged": user_result.unchanged,
        "users_skipped_domain": user_result.skipped_domain,
        "users_without_explicit_craft_activity": (
            user_result
            .without_explicit_craft_activity
        ),
        "users_skipped_protected": (
            user_result.skipped_protected
        ),
        "users_skipped_excluded": (
            user_result.skipped_excluded
        ),
        "users_skipped_conflict": (
            user_result.skipped_conflict
        ),
        "user_sync_configuration_missing": (
            user_result.configuration_missing
        ),
        "craft_activities_created": (
            craft_result
            .craft_activities_created
        ),
        "craft_activities_updated": (
            craft_result
            .craft_activities_updated
        ),
        "service_positions_created": (
            service_position_result
            .service_positions_created
        ),
        "service_positions_updated": (
            service_position_result
            .service_positions_updated
        ),
        (
            "service_positions_skipped_"
            "disabled_craft_activity"
        ): (
            service_position_result
            .service_positions_skipped_disabled_craft_activity
        ),
        "service_positions_deactivated": (
            service_position_result
            .service_positions_deactivated
        ),
    }

    if not visible_status_ids or not allowed_type_ids:
        result["configuration_missing"] = 1
        current_app.logger.error(
            "regular_commission_sync_configuration_missing "
            "visible_status_ids=%s allowed_type_ids=%s",
            sorted(visible_status_ids),
            sorted(allowed_type_ids),
        )
        return result

    synced_erp_ids = set()
    reconciliation_complete = True
    limit = 100

    for craftsman_erp_id in sorted(craftsman_erp_ids):
        for status_id in sorted(visible_status_ids):
            offset = 0

            while True:
                current_app.logger.info(
                    "regular_commission_sync_page "
                    "full=%s craftsman_erp_id=%s "
                    "offset=%s limit=%s status_id=%s "
                    "date_from=%s date_to=%s",
                    full,
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
                except Exception as exc:
                    current_app.logger.exception(
                        "regular_commission_sync_page_failed "
                        "craftsman_erp_id=%s status_id=%s offset=%s exc=%s",
                        craftsman_erp_id,
                        status_id,
                        offset,
                        str(exc),
                    )
                    result["errors"] += 1
                    reconciliation_complete = False
                    break

                if not payloads:
                    break

                for payload in payloads:
                    result["seen"] += 1

                    try:
                        with db.session.begin_nested():
                            sync_result = sync_commission_payload_from_erp(
                                payload=payload,
                                visible_status_ids=visible_status_ids,
                                allowed_type_ids=allowed_type_ids,
                                date_from=date_from,
                                date_to=date_to,
                                date_field=date_field,
                                allowed_craftsman_erp_ids=craftsman_erp_ids,
                                service=service,
                            )

                        erp_id = _as_int(
                            _read_first(payload, "id_", "id", "Id")
                        )

                        if (
                                sync_result
                                in {"synced", "unchanged"}
                                and erp_id is not None
                        ):
                            synced_erp_ids.add(erp_id)

                        if sync_result in result:
                            result[sync_result] += 1
                    except Exception as exc:
                        current_app.logger.exception(
                            "regular_commission_sync_item_failed "
                            "erp_id=%s exc=%s",
                            _read_first(payload, "id_", "id", "Id"),
                            str(exc),
                        )
                        result["errors"] += 1
                        reconciliation_complete = False

                db.session.commit()

                if len(payloads) < limit:
                    break

                offset += limit

    if reconciliation_complete:
        mark_missing_regular_sync_commissions_deleted(
            date_from=date_from,
            date_to=date_to,
            date_field=date_field,
            allowed_craftsman_erp_ids=craftsman_erp_ids,
            visible_status_ids=visible_status_ids,
            synced_erp_ids=synced_erp_ids,
            result=result,
        )
    else:
        result["reconciliation_skipped"] = 1

    db.session.commit()
    return result


def sync_commission_payload_from_erp(
        payload: wowipy.CommissioningCommission,
        visible_status_ids: set[int],
        allowed_type_ids: set[int],
        date_from: datetime,
        date_to: datetime,
        date_field: str,
        allowed_craftsman_erp_ids: set[int],
        service: WowiportService,
) -> str:
    erp_id = int(
        _read_first(payload, "id_", "id", "Id")
    )
    existing = Commission.query.filter_by(
        erp_id=erp_id
    ).one_or_none()
    existed_before_sync = existing is not None
    previous_user_id = existing.user_id if existing is not None else None
    was_deleted_before_sync = bool(
        existing is not None
        and (
                existing.local_status == CommissionLocalStatus.DELETED.value
                or existing.deleted_at is not None
        )
    )
    had_sync_signature_before_sync = bool(
        existing is not None
        and isinstance(existing.raw_payload, dict)
        and existing.raw_payload.get("sync_signature")
    )
    status_id = _commission_status_id(payload)
    commission_type_id = _commission_type_id(
        payload
    )

    if (
            existing is None
            and status_id == 11
    ):
        return (
            "ignored_await_invoice_not_local"
        )

    craftsman = _read_first(
        payload,
        "craftsman",
        "Craftsman",
    )
    craftsman_erp_id = _as_int(
        _read_first(craftsman, "id_", "id", "Id")
    )

    if craftsman_erp_id not in allowed_craftsman_erp_ids:
        if existing is not None:
            mark_commission_deleted(erp_id)
            return "deleted_marked"

        return "ignored_unknown_craftsman"

    if status_id not in visible_status_ids:
        if existing is not None:
            mark_commission_deleted(erp_id)
            return "deleted_marked"

        return "ignored_status"

    if commission_type_id not in allowed_type_ids:
        if existing is not None:
            mark_commission_deleted(erp_id)
            return "deleted_marked"

        return "ignored_type"

    commission_date = get_commission_business_year_date(
        payload,
        date_field,
    )

    if (
            commission_date is None
            or not date_from <= commission_date < date_to
    ):
        if existing is not None:
            mark_commission_deleted(erp_id)
            return "deleted_marked"

        return "ignored_business_year"

    sync_signature = _commission_sync_signature(
        payload
    )

    if _commission_is_unchanged(
            existing,
            sync_signature,
    ):
        return "unchanged"

    commission = upsert_commission(
        payload,
        sync_signature=sync_signature,
    )

    refresh_commission_context_safely(
        commission=commission,
        service=service,
        erp_id=erp_id,
        payload=payload,
    )

    sync_commission_documents_safely(
        commission=commission,
        service=service,
        force=True,
    )
    enqueue_synced_commission_push(
        commission=commission,
        existed_before_sync=existed_before_sync,
        previous_user_id=previous_user_id,
        was_deleted_before_sync=was_deleted_before_sync,
        had_sync_signature_before_sync=had_sync_signature_before_sync,
    )

    return "synced"


def quick_reconciliation_range() -> tuple[
    datetime,
    datetime,
]:
    lookback_days = max(
        settings.get(
            SettingKeys
            .SYNC_QUICK_RECONCILIATION_LOOKBACK_DAYS
        ),
        1,
    )
    now = datetime.now()
    start = now - timedelta(days=lookback_days)
    end = now + timedelta(days=1)

    return (
        _as_naive_datetime(start),
        _as_naive_datetime(end),
    )


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
        visible_status_ids: set[int],
        synced_erp_ids: set[int],
        result: dict[str, int],
) -> None:
    if not allowed_craftsman_erp_ids or not visible_status_ids:
        return

    query = Commission.query.filter(
        Commission.erp_id.isnot(None),
        Commission.local_status != CommissionLocalStatus.DELETED.value,
        Commission.craftsman_erp_id.in_(allowed_craftsman_erp_ids),
        Commission.status_id.in_(visible_status_ids),
    )

    if synced_erp_ids:
        query = query.filter(
            Commission.erp_id.notin_(synced_erp_ids)
        )

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

    for commission in query.all():
        commission.local_status = CommissionLocalStatus.DELETED.value
        commission.deleted_at = utc_now()
        commission.synced_at = utc_now()
        result["deleted_marked"] += 1


def upsert_commission(
        payload: Any,
        sync_signature: str | None = None,
) -> Commission:
    erp_id = int(_read_first(payload, "id_", "id", "Id"))
    commission = Commission.query.filter_by(erp_id=erp_id).one_or_none()

    if commission is None:
        commission = Commission(
            erp_id=erp_id,
            source=CommissionSource.WOWIPORT.value,
            erp_sync_state=CommissionSyncState.SYNCED.value,
        )
        db.session.add(commission)

    commission_status = _read_first(
        payload,
        "commission_status",
        "CommissionStatus",
    )
    commission_type = _read_first(
        payload,
        "commission_type",
        "CommissionType",
    )
    crafts_process = _read_first(
        payload,
        "crafts_process",
        "CraftsProcess",
    )
    creditor = _read_first(payload, "creditor", "Creditor")
    department = _read_first(payload, "department", "Department")
    responsible_official = _read_first(
        payload,
        "responsible_official_repair",
        "responsible_official",
        "ResponsibleOfficialRepair",
        "ResponsibleOfficial",
    )
    craftsman = _read_first(payload, "craftsman", "Craftsman")
    commission_items = (
            _read_first(
                payload,
                "commission_items",
                "CommissionItems",
            )
            or []
    )

    craftsman_erp_id = _as_int(
        _read_first(craftsman, "id_", "id", "Id")
    )

    user = None
    if craftsman_erp_id is not None:
        user = User.query.filter_by(
            craftsman_erp_id=craftsman_erp_id,
            is_active_flag=True,
        ).one_or_none()

    commission.erp_id = erp_id
    commission.erp_idnum = _read_first(
        payload,
        "id_num",
        "idNum",
        "IdNum",
    )
    commission.code = _read_first(payload, "code", "Code")
    commission.commission_type_erp_id = _as_int(
        _read_first(commission_type, "id_", "id", "Id")
    )
    commission.crafts_process_erp_id = _as_int(
        _read_first(crafts_process, "id_", "id", "Id")
    )
    commission.creditor_erp_id = _as_int(
        _read_first(creditor, "id_", "id", "Id")
    )
    commission.department_erp_id = _as_int(
        _read_first(department, "id_", "id", "Id")
    )
    commission.responsible_official_repair_erp_id = _as_int(
        _read_first(
            responsible_official,
            "id_",
            "id",
            "Id",
        )
    )
    commission.status_id = _as_int(
        _read_first(commission_status, "id_", "id", "Id")
    )
    commission.status_code = _read_first(
        commission_status,
        "code",
        "Code",
    )
    commission.craftsman_erp_id = craftsman_erp_id
    commission.user = user
    commission.damage_location = _read_first(
        payload,
        "damage_location",
        "DamageLocation",
    )
    commission.contact_person = _first_text(
        _read_first(
            payload,
            "contact_person_on_site",
            "ContactPersonOnSite",
        ),
        _read_first(
            payload,
            "contact_person_engineering",
            "ContactPersonEngineering",
        ),
    )
    commission.recording_date = parse_datetime(
        _read_first(payload, "recording_date", "RecordingDate")
    )
    commission.release_date = parse_datetime(
        _read_first(payload, "release_date", "ReleaseDate")
    )
    commission.execution_from = parse_datetime(
        _read_first(payload, "execution_from", "ExecutionFrom")
    )
    commission.execution_to = parse_datetime(
        _read_first(payload, "execution_to", "ExecutionTo")
    )
    commission.deleted_at = None
    commission.synced_at = utc_now()

    if not commission.source:
        commission.source = CommissionSource.WOWIPORT.value

    if commission.erp_sync_state not in {
        CommissionSyncState.PENDING.value,
        CommissionSyncState.FAILED.value,
    }:
        commission.erp_sync_state = CommissionSyncState.SYNCED.value
        commission.erp_sync_message = None
        commission.local_status = ERP_LOCAL_STATUS.get(
            commission.status_id,
            CommissionLocalStatus.OPEN.value,
        )

    replace_commission_positions(
        commission,
        commission_items,
    )

    if len(commission.positions) > 1:
        commission.title = "Mehrere Positionen"
        commission.description = "Mehrere Positionen"
    elif commission.positions:
        first_position = commission.positions[0]
        commission.title = _first_text(
            first_position.internal_description,
            first_position.commission_text,
            first_position.code,
            commission.erp_idnum,
            f"Auftrag {erp_id}",
        )
        commission.description = _first_text(
            first_position.commission_text,
            first_position.internal_description,
            first_position.description,
        )
    else:
        commission.title = _first_text(
            _read_first(payload, "description", "Description"),
            commission.erp_idnum,
            f"Auftrag {erp_id}",
        )
        commission.description = _read_first(
            payload,
            "description",
            "Description",
        )

    if sync_signature:
        raw_payload = (
            dict(commission.raw_payload)
            if isinstance(
                commission.raw_payload,
                dict,
            )
            else {}
        )
        raw_payload["sync_signature"] = (
            sync_signature
        )
        commission.raw_payload = raw_payload

    return commission


def replace_commission_positions(
        commission: Commission,
        payload_items: list[Any],
) -> None:
    commission.positions.clear()

    if commission.id is not None:
        db.session.flush()

    for position_index, payload in enumerate(payload_items):
        service_catalogue = _read_first(
            payload,
            "service_catalogue",
            "service_catalog",
            "ServiceCatalogue",
            "ServiceCatalog",
        )
        craft_activity = _read_first(
            payload,
            "craft_activity",
            "CraftActivity",
        )
        units = _as_float(
            _read_first(payload, "units", "Units")
        )

        commission.positions.append(
            CommissionPosition(
                erp_id=_as_int(
                    _read_first(payload, "id_", "id", "Id")
                ),
                position_index=position_index,
                position_number=_as_float(
                    _read_first(
                        payload,
                        "position_number",
                        "PositionNumber",
                    )
                ),
                code=_read_first(payload, "code", "Code"),
                commission_text=_read_first(
                    payload,
                    "commission_text",
                    "CommissionText",
                ),
                internal_description=_read_first(
                    payload,
                    "internal_description",
                    "InternalDescription",
                ),
                description=_read_first(
                    payload,
                    "description",
                    "Description",
                ),
                units=1.0 if units is None else units,
                unit_price=_as_float(
                    _read_first(
                        payload,
                        "unit_price",
                        "UnitPrice",
                    )
                ),
                net_amount=_as_float(
                    _read_first(
                        payload,
                        "net_amount",
                        "NetAmount",
                    )
                ),
                material_costs=_as_float(
                    _read_first(
                        payload,
                        "material_costs",
                        "MaterialCosts",
                    )
                ),
                labour_costs=_as_float(
                    _read_first(
                        payload,
                        "labour_costs",
                        "LabourCosts",
                    )
                ),
                service_catalog_erp_id=_as_int(
                    _read_first(
                        service_catalogue,
                        "id_",
                        "id",
                        "Id",
                    )
                ),
                craft_activity_erp_id=_as_int(
                    _read_first(
                        craft_activity,
                        "id_",
                        "id",
                        "Id",
                    )
                ),
            )
        )


def _commission_status_id(payload: Any) -> int | None:
    commission_status = _read_first(
        payload,
        "commission_status",
        "CommissionStatus",
    )
    return _as_int(
        _read_first(commission_status, "id_", "id", "Id")
    )


def _commission_type_id(payload: Any) -> int | None:
    commission_type = _read_first(
        payload,
        "commission_type",
        "CommissionType",
    )
    return _as_int(
        _read_first(commission_type, "id_", "id", "Id")
    )


def _integer_set(value: Any) -> set[int]:
    if not isinstance(value, (list, tuple, set)):
        return set()

    result = set()

    for item in value:
        parsed_item = _as_int(item)
        if parsed_item is not None:
            result.add(parsed_item)

    return result


def apply_commission_context(commission: Commission, context: dict[str, Any]) -> None:
    if context.get("use_unit_id"):
        commission.use_unit_id = int(context["use_unit_id"])

        try:
            from .location_sync import sync_wowi_use_unit

            use_unit_location = sync_wowi_use_unit(commission.use_unit_id)

            if use_unit_location and use_unit_location.building:
                commission.building_erp_id = use_unit_location.building.erp_id
                commission.latitude = use_unit_location.building.latitude
                commission.longitude = use_unit_location.building.longitude
        except Exception as ex:
            current_app.logger.exception(
                "commission_location_sync_failed commission_id=%s use_unit_id=%s ex %s",
                commission.id,
                commission.use_unit_id,
                str(ex)
            )

    if context.get("license_agreement_id"):
        commission.license_agreement_id = int(context["license_agreement_id"])

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


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None

    try:
        return float(value)
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


def _read_first(source: Any, *names: str) -> Any:
    if source is None:
        return None

    for name in names:
        if isinstance(source, dict):
            if name in source:
                return source.get(name)
        elif hasattr(source, name):
            return getattr(source, name)

    return None


def _clean_id_num(value: Any) -> str | None:
    if value is None:
        return None

    normalized_value = str(value).strip()
    return normalized_value or None


def _single_idnum_match(
        items: list[Any],
        id_num: str,
        label: str,
) -> Any:
    matches = [
        item
        for item in items
        if _clean_id_num(
            _attr(item, "id_num")
        )
           == id_num
    ]

    if not matches:
        raise RuntimeError(
            f"{label} mit der Nummer {id_num} "
            "wurde in OPENWOWI nicht gefunden."
        )

    if len(matches) > 1:
        raise RuntimeError(
            f"{label} mit der Nummer {id_num} "
            "ist in OPENWOWI nicht eindeutig."
        )

    return matches[0]


def _merge_reference_id(
        current_id: int | None,
        resolved_id: int | None,
        label: str,
) -> int | None:
    if resolved_id is None:
        raise RuntimeError(
            f"Für {label} wurde keine interne "
            "OPENWOWI-ID zurückgegeben."
        )

    if (
            current_id is not None
            and current_id != resolved_id
    ):
        raise RuntimeError(
            f"Die angegebene {label}-Nummer passt "
            "nicht zur bereits gespeicherten Zuordnung."
        )

    return resolved_id


def _commission_is_unchanged(
        commission: Commission | None,
        sync_signature: str,
) -> bool:
    if commission is None:
        return False

    if (
            commission.local_status
            == CommissionLocalStatus.DELETED.value
    ):
        return False

    if (
            commission.erp_sync_state
            != CommissionSyncState.SYNCED.value
    ):
        return False

    if not isinstance(
            commission.raw_payload,
            dict,
    ):
        return False

    return (
            commission.raw_payload.get("sync_signature")
            == sync_signature
    )


def _commission_sync_signature(
        payload: Any,
) -> str:
    commission_status = _read_first(
        payload,
        "commission_status",
        "CommissionStatus",
    )
    commission_type = _read_first(
        payload,
        "commission_type",
        "CommissionType",
    )
    crafts_process = _read_first(
        payload,
        "crafts_process",
        "CraftsProcess",
    )
    craftsman = _read_first(
        payload,
        "craftsman",
        "Craftsman",
    )
    commission_items = (
            _read_first(
                payload,
                "commission_items",
                "CommissionItems",
            )
            or []
    )

    signature_data = {
        "id_num": _read_first(
            payload,
            "id_num",
            "idNum",
            "IdNum",
        ),
        "code": _read_first(
            payload,
            "code",
            "Code",
        ),
        "status_id": _as_int(
            _read_first(
                commission_status,
                "id_",
                "id",
                "Id",
            )
        ),
        "status_code": _read_first(
            commission_status,
            "code",
            "Code",
        ),
        "commission_type_id": _as_int(
            _read_first(
                commission_type,
                "id_",
                "id",
                "Id",
            )
        ),
        "crafts_process_id": _as_int(
            _read_first(
                crafts_process,
                "id_",
                "id",
                "Id",
            )
        ),
        "craftsman_id": _as_int(
            _read_first(
                craftsman,
                "id_",
                "id",
                "Id",
            )
        ),
        "damage_location": _read_first(
            payload,
            "damage_location",
            "DamageLocation",
        ),
        "contact_person_on_site": _read_first(
            payload,
            "contact_person_on_site",
            "ContactPersonOnSite",
        ),
        "contact_person_engineering": _read_first(
            payload,
            "contact_person_engineering",
            "ContactPersonEngineering",
        ),
        "recording_date": _signature_value(
            _read_first(
                payload,
                "recording_date",
                "RecordingDate",
            )
        ),
        "release_date": _signature_value(
            _read_first(
                payload,
                "release_date",
                "ReleaseDate",
            )
        ),
        "execution_from": _signature_value(
            _read_first(
                payload,
                "execution_from",
                "ExecutionFrom",
            )
        ),
        "execution_to": _signature_value(
            _read_first(
                payload,
                "execution_to",
                "ExecutionTo",
            )
        ),
        "positions": [
            _position_signature(
                item,
                position_index,
            )
            for position_index, item
            in enumerate(commission_items)
        ],
    }

    serialized_data = json.dumps(
        signature_data,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    return hashlib.sha256(
        serialized_data.encode("utf-8")
    ).hexdigest()


def _position_signature(
        payload: Any,
        position_index: int,
) -> dict[str, Any]:
    service_catalogue = _read_first(
        payload,
        "service_catalogue",
        "service_catalog",
        "ServiceCatalogue",
        "ServiceCatalog",
    )
    craft_activity = _read_first(
        payload,
        "craft_activity",
        "CraftActivity",
    )

    return {
        "position_index": position_index,
        "erp_id": _as_int(
            _read_first(payload, "id_", "id", "Id")
        ),
        "position_number": _as_float(
            _read_first(
                payload,
                "position_number",
                "PositionNumber",
            )
        ),
        "code": _read_first(
            payload,
            "code",
            "Code",
        ),
        "commission_text": _read_first(
            payload,
            "commission_text",
            "CommissionText",
        ),
        "internal_description": _read_first(
            payload,
            "internal_description",
            "InternalDescription",
        ),
        "description": _read_first(
            payload,
            "description",
            "Description",
        ),
        "units": _as_float(
            _read_first(payload, "units", "Units")
        ),
        "unit_price": _as_float(
            _read_first(
                payload,
                "unit_price",
                "UnitPrice",
            )
        ),
        "net_amount": _as_float(
            _read_first(
                payload,
                "net_amount",
                "NetAmount",
            )
        ),
        "material_costs": _as_float(
            _read_first(
                payload,
                "material_costs",
                "MaterialCosts",
            )
        ),
        "labour_costs": _as_float(
            _read_first(
                payload,
                "labour_costs",
                "LabourCosts",
            )
        ),
        "service_catalogue_id": _as_int(
            _read_first(
                service_catalogue,
                "id_",
                "id",
                "Id",
            )
        ),
        "craft_activity_id": _as_int(
            _read_first(
                craft_activity,
                "id_",
                "id",
                "Id",
            )
        ),
    }


def _signature_value(value: Any) -> str | None:
    if value is None:
        return None

    if isinstance(value, datetime):
        return value.isoformat()

    return str(value)
