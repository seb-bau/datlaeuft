from __future__ import annotations

from datetime import date, timedelta
from decimal import (
    Decimal,
    InvalidOperation,
    ROUND_HALF_UP,
)
from typing import Any
import re

from flask import current_app
from sqlalchemy import select

from .audit import audit
from .extensions import db
from .models import (
    Commission,
    CommissionAssignmentType,
    CommissionLocalStatus,
    InvoiceStatus,
    Offer,
    OfferCraftFallback,
    OfferPosition,
    OfferPositionSource,
    OfferStatus,
    TimeTaskTemplate,
    User,
    WowiCompanyCode,
    utc_now,
)
from .offer_document import (
    OfferDocumentResult,
    create_offer_document,
)
from .settings import SettingKeys, settings


MONEY_QUANTIZER = Decimal("0.01")


class OfferFinalizationError(RuntimeError):
    pass


def decimal_value(
    value,
    label: str,
) -> Decimal:
    try:
        return Decimal(
            str(value).strip().replace(",", ".")
        )
    except (
        InvalidOperation,
        TypeError,
        ValueError,
    ) as exc:
        raise ValueError(
            f"{label} ist ungültig."
        ) from exc


def money_value(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(
        MONEY_QUANTIZER,
        rounding=ROUND_HALF_UP,
    )


def optional_int(value) -> int | None:
    normalized = str(value or "").strip()

    if not normalized:
        return None

    try:
        return int(normalized)
    except ValueError as exc:
        raise ValueError(
            "Die Leistungsposition ist ungültig."
        ) from exc


def create_offer_draft(
    company_code: WowiCompanyCode,
    created_by: User,
) -> Offer:
    if not company_code.active:
        raise ValueError(
            "Das ausgewählte Buchwerk "
            "ist nicht aktiv."
        )

    if company_code.document_creation_disabled:
        raise ValueError("Für dieses Buchwerk können keine neuen Angebote erstellt werden.")

    offer_date = date.today()
    validity_days = max(
        0,
        int(
            settings.get(
                SettingKeys.OFFER_VALIDITY_DAYS
            )
            or 0
        ),
    )

    offer = Offer(
        status=OfferStatus.DRAFT.value,
        company_code=company_code,
        company_code_erp_id=(
            company_code.erp_id
        ),
        company_code_code=company_code.code,
        offer_date=offer_date,
        valid_until=(
            offer_date
            + timedelta(days=validity_days)
        ),
        recipient_country_code="DE",
        seller_name=company_code.seller_name,
        seller_street=company_code.seller_street,
        seller_postal_code=(
            company_code.seller_postal_code
        ),
        seller_town=company_code.seller_town,
        seller_country_code=(
            company_code.seller_country_code
            or "DE"
        ),
        seller_vat_id=(
            company_code.seller_vat_id
        ),
        seller_tax_number=(
            company_code.seller_tax_number
        ),
        seller_email=(
            company_code.seller_email
        ),
        seller_phone=(
            company_code.seller_phone
        ),
        currency_code="EUR",
        introductory_text=settings.get(SettingKeys.OFFER_INTRODUCTORY_TEXT) or None,
        concluding_text=settings.get(SettingKeys.OFFER_CONCLUDING_TEXT) or None,
        created_by=created_by,
    )

    db.session.add(offer)
    return offer


def replace_offer_positions(
    offer: Offer,
    rows: list[dict],
) -> None:
    if offer.status != OfferStatus.DRAFT.value:
        raise ValueError(
            "Nur ein Angebotsentwurf "
            "kann geändert werden."
        )

    if not rows:
        raise ValueError(
            "Das Angebot benötigt mindestens "
            "eine Position."
        )

    offer.positions.clear()

    if offer.id is not None:
        db.session.flush()

    for position_index, row in enumerate(rows):
        source_type = str(
            row.get("source_type")
            or OfferPositionSource.FREE.value
        ).strip()

        if source_type not in {
            OfferPositionSource.CATALOG.value,
            OfferPositionSource.MATERIAL.value,
            OfferPositionSource.LOCAL_CATALOG.value,
            OfferPositionSource.FREE.value,
        }:
            raise ValueError(
                f"Die Art der Position "
                f"{position_index + 1} "
                "ist ungültig."
            )

        description = str(
            row.get("description") or ""
        ).strip()

        if not description:
            raise ValueError(
                f"Position {position_index + 1} "
                "benötigt eine Bezeichnung."
            )

        details = (
            str(
                row.get("details") or ""
            ).strip()
            or None
        )
        unit = str(
            row.get("unit") or "Stk"
        ).strip()

        if not unit:
            raise ValueError(
                f"Position {position_index + 1} "
                "benötigt eine Einheit."
            )

        quantity = decimal_value(
            row.get("quantity"),
            (
                "Menge der Position "
                f"{position_index + 1}"
            ),
        )
        unit_price_net = decimal_value(
            row.get("unit_price_net"),
            (
                "Einzelpreis der Position "
                f"{position_index + 1}"
            ),
        )
        vat_rate = decimal_value(
            row.get("vat_rate"),
            (
                "Umsatzsteuersatz der Position "
                f"{position_index + 1}"
            ),
        )

        if quantity <= 0:
            raise ValueError(
                f"Position {position_index + 1} "
                "benötigt eine positive Menge."
            )

        if unit_price_net < 0:
            raise ValueError(
                f"Der Einzelpreis der Position "
                f"{position_index + 1} "
                "darf nicht negativ sein."
            )

        if vat_rate < 0 or vat_rate > 100:
            raise ValueError(
                "Der Umsatzsteuersatz der "
                f"Position {position_index + 1} "
                "muss zwischen 0 und 100 liegen."
            )

        task_template = None
        task_template_id = optional_int(
            row.get("task_template_id")
        )

        if (
            source_type
            == OfferPositionSource.CATALOG.value
        ):
            if task_template_id is None:
                raise ValueError(
                    f"Position {position_index + 1} "
                    "benötigt eine "
                    "Leistungsposition."
                )

            task_template = db.session.get(
                TimeTaskTemplate,
                task_template_id,
            )

            if (
                task_template is None
                or not task_template.active
                or (
                    task_template
                    .erp_service_catalogue_id
                    is None
                )
                or (
                    task_template
                    .craft_activity_id
                    is None
                )
            ):
                raise ValueError(
                    f"Die Leistungsposition "
                    f"{position_index + 1} "
                    "ist nicht verfügbar."
                )

            if offer.craft_activity_id is None:
                offer.craft_activity_id = (
                    task_template
                    .craft_activity_id
                )
            elif (
                offer.craft_activity_id
                != task_template
                .craft_activity_id
            ):
                raise ValueError(
                    "Alle Angebotspositionen "
                    "müssen demselben Gewerk "
                    "zugeordnet sein."
                )

        net_amount = (
            quantity * unit_price_net
        ).quantize(
            MONEY_QUANTIZER,
            rounding=ROUND_HALF_UP,
        )
        vat_amount = (
            net_amount
            * vat_rate
            / Decimal("100")
        ).quantize(
            MONEY_QUANTIZER,
            rounding=ROUND_HALF_UP,
        )
        gross_amount = (
            net_amount + vat_amount
        ).quantize(
            MONEY_QUANTIZER,
            rounding=ROUND_HALF_UP,
        )

        offer.positions.append(
            OfferPosition(
                position_index=position_index,
                source_type=source_type,
                task_template=task_template,
                inventory_item_id=row.get("inventory_item_id"),
                local_catalog_item_id=row.get("local_catalog_item_id"),
                source_data=row.get("source_data"),
                description=description,
                details=details,
                quantity=quantity,
                unit=unit[:40],
                unit_price_net=unit_price_net,
                vat_rate=vat_rate,
                net_amount=net_amount,
                vat_amount=vat_amount,
                gross_amount=gross_amount,
            )
        )

    recalculate_offer(offer)


def recalculate_offer(
    offer: Offer,
) -> None:
    total_net = sum(
        (
            money_value(
                position.net_amount
            )
            for position in offer.positions
        ),
        Decimal("0"),
    )
    total_vat = sum(
        (
            money_value(
                position.vat_amount
            )
            for position in offer.positions
        ),
        Decimal("0"),
    )

    offer.total_net = money_value(total_net)
    offer.total_vat = money_value(total_vat)
    offer.total_gross = money_value(
        total_net + total_vat
    )


def render_offer_number(
    company_code: WowiCompanyCode,
    year: int,
    counter: int,
) -> str:
    raw_mask = str(
        company_code.offer_number_mask
        or (
            "<company_code>-ANG-"
            "<year>-<counter>"
        )
    ).strip()

    mask = (
        raw_mask
        .replace(
            "<company_code>",
            "{company_code}",
        )
        .replace(
            "<year>",
            "{year}",
        )
        .replace(
            "<counter>",
            "{counter}",
        )
    )

    if (
        "{year}" not in mask
        or "{counter}" not in mask
    ):
        raise OfferFinalizationError(
            "Die Angebotsnummernmaske muss "
            "<year> und <counter> enthalten."
        )

    allowed_fields = {
        "company_code",
        "year",
        "counter",
    }
    used_fields = set(
        re.findall(
            r"\{([^{}]+)\}",
            mask,
        )
    )
    unresolved_fields = set(
        re.findall(
            r"<([^<>]+)>",
            mask,
        )
    )
    unknown_fields = (
        used_fields - allowed_fields
    ) | unresolved_fields

    if unknown_fields:
        raise OfferFinalizationError(
            "Unbekannte Platzhalter in der "
            "Angebotsnummernmaske: "
            + ", ".join(
                sorted(unknown_fields)
            )
        )

    digits = max(
        1,
        min(
            12,
            int(
                company_code
                .offer_counter_digits
                or 5
            ),
        ),
    )

    number = (
        mask
        .replace(
            "{company_code}",
            company_code.code,
        )
        .replace(
            "{year}",
            str(year),
        )
        .replace(
            "{counter}",
            str(counter).zfill(digits),
        )
    ).strip()

    if not number:
        raise OfferFinalizationError(
            "Die Angebotsnummer ist leer."
        )

    return number


def allocate_offer_number(
    offer: Offer,
    year: int,
) -> str:
    company_code = db.session.execute(
        select(WowiCompanyCode)
        .where(
            WowiCompanyCode.id
            == offer.company_code_id
        )
        .with_for_update()
    ).scalar_one()

    if (
        company_code.offer_counter_year
        != year
    ):
        company_code.offer_counter_year = year
        company_code.offer_counter = 0

    for _ in range(100):
        company_code.offer_counter = (
            int(
                company_code.offer_counter
                or 0
            )
            + 1
        )

        offer_number = render_offer_number(
            company_code,
            year,
            company_code.offer_counter,
        )

        existing_offer = (
            Offer.query
            .filter(
                Offer.offer_number
                == offer_number,
                Offer.id != offer.id,
            )
            .first()
        )

        if existing_offer is None:
            offer.company_code = company_code
            return offer_number

    raise OfferFinalizationError(
        "Es konnte keine freie "
        "Angebotsnummer ermittelt werden."
    )


def validate_offer_for_release(
    offer: Offer,
) -> None:
    if offer.status != OfferStatus.DRAFT.value:
        raise OfferFinalizationError(
            "Nur ein Entwurf kann "
            "freigegeben werden."
        )

    if offer.craft_activity_id is None:
        raise OfferFinalizationError(
            "Das Gewerk fehlt."
        )

    if not offer.positions:
        raise OfferFinalizationError(
            "Das Angebot enthält keine Positionen."
        )

    for label, value in (
        (
            "Name des Empfängers",
            offer.recipient_name,
        ),
        (
            "Straße des Empfängers",
            offer.recipient_street,
        ),
        (
            "Postleitzahl des Empfängers",
            offer.recipient_postal_code,
        ),
        (
            "Ort des Empfängers",
            offer.recipient_town,
        ),
    ):
        if not str(value or "").strip():
            raise OfferFinalizationError(
                f"{label} fehlt."
            )

    if offer.offer_date is None:
        raise OfferFinalizationError(
            "Das Angebotsdatum fehlt."
        )

    if offer.valid_until is None:
        raise OfferFinalizationError(
            "Das Gültigkeitsdatum fehlt."
        )

    if offer.valid_until < offer.offer_date:
        raise OfferFinalizationError(
            "Das Gültigkeitsdatum darf nicht "
            "vor dem Angebotsdatum liegen."
        )

    recalculate_offer(offer)


def release_offer(
    offer: Offer,
    user: User,
) -> OfferDocumentResult:
    if offer.offer_date is None:
        offer.offer_date = date.today()

    if offer.valid_until is None:
        validity_days = max(
            0,
            int(
                settings.get(
                    SettingKeys
                    .OFFER_VALIDITY_DAYS
                )
                or 0
            ),
        )
        offer.valid_until = (
            offer.offer_date
            + timedelta(days=validity_days)
        )

    validate_offer_for_release(offer)

    offer.offer_number = (
        allocate_offer_number(
            offer,
            offer.offer_date.year,
        )
    )

    db.session.flush()

    offer.status = OfferStatus.AWAITING_RESPONSE.value
    document_result = (
        create_offer_document(offer)
    )

    offer.document_filename = (
        document_result.pdf_storage_name
    )
    offer.document_sha256 = (
        document_result.pdf_sha256
    )
    offer.released_at = utc_now()
    offer.released_by = user
    offer.status_note = None

    audit(
        "offer.release",
        "offer",
        offer.id,
        {
            "offer_number": (
                offer.offer_number
            ),
            "total_net": str(
                offer.total_net
            ),
            "total_vat": str(
                offer.total_vat
            ),
            "total_gross": str(
                offer.total_gross
            ),
            "document_sha256": (
                offer.document_sha256
            ),
        },
    )

    db.session.flush()
    return document_result


def copy_offer_to_draft(
    source_offer: Offer,
    user: User,
) -> Offer:
    item = create_offer_draft(
        source_offer.company_code,
        user,
    )
    item.source_offer_id = source_offer.id
    item.craft_activity_id = (
        source_offer.craft_activity_id
    )
    item.title = source_offer.title
    item.object_type = source_offer.object_type
    item.object_id = source_offer.object_id
    item.economic_unit_erp_id = (
        source_offer.economic_unit_erp_id
    )
    item.building_erp_id = (
        source_offer.building_erp_id
    )
    item.use_unit_erp_id = (
        source_offer.use_unit_erp_id
    )
    item.license_agreement_erp_id = (
        source_offer
        .license_agreement_erp_id
    )
    item.license_agreement_idnum = (
        source_offer
        .license_agreement_idnum
    )
    item.object_address = (
        source_offer.object_address
    )
    item.object_location = (
        source_offer.object_location
    )
    item.tenant_name = (
        source_offer.tenant_name
    )
    item.recipient_type = (
        source_offer.recipient_type
    )
    item.recipient_person_erp_id = (
        source_offer.recipient_person_erp_id
    )
    item.recipient_name = (
        source_offer.recipient_name
    )
    item.recipient_name_addition = (
        source_offer.recipient_name_addition
    )
    item.recipient_street = (
        source_offer.recipient_street
    )
    item.recipient_postal_code = (
        source_offer.recipient_postal_code
    )
    item.recipient_town = (
        source_offer.recipient_town
    )
    item.recipient_country_code = (
        source_offer.recipient_country_code
    )
    item.recipient_email = (
        source_offer.recipient_email
    )
    item.recipient_vat_id = (
        source_offer.recipient_vat_id
    )
    item.introductory_text = (
        source_offer.introductory_text
    )
    item.concluding_text = (
        source_offer.concluding_text
    )
    item.currency_code = (
        source_offer.currency_code
    )

    rows = [
        {
            "source_type": position.source_type,
            "task_template_id": (
                position.task_template_id
            ),
            "inventory_item_id": position.inventory_item_id,
            "local_catalog_item_id": position.local_catalog_item_id,
            "source_data": dict(position.source_data or {}),
            "description": (
                position.description
            ),
            "details": position.details,
            "quantity": position.quantity,
            "unit": position.unit,
            "unit_price_net": (
                position.unit_price_net
            ),
            "vat_rate": position.vat_rate,
        }
        for position in source_offer.positions
    ]

    replace_offer_positions(
        item,
        rows,
    )

    return item


def reject_offer(
    item: Offer,
    reason: str,
    user: User,
) -> None:
    normalized_reason = reason.strip()

    if item.status not in {
        OfferStatus.AWAITING_RESPONSE.value,
        OfferStatus.EXPIRED.value,
    }:
        raise ValueError(
            "Dieses Angebot kann nicht "
            "als abgelehnt markiert werden."
        )

    if not normalized_reason:
        raise ValueError(
            "Für die Ablehnung ist eine "
            "Begründung erforderlich."
        )

    if item.commission_id is not None:
        raise ValueError(
            "Für dieses Angebot wurde bereits "
            "ein Auftrag angelegt."
        )

    item.status = OfferStatus.REJECTED.value
    item.status_note = normalized_reason
    item.rejected_by_user_id = user.id
    item.rejected_at = utc_now()


def cancel_offer(
    item: Offer,
    reason: str,
    user: User,
) -> None:
    normalized_reason = reason.strip()

    if item.status not in {
        OfferStatus.AWAITING_RESPONSE.value,
        OfferStatus.EXPIRED.value,
    }:
        raise ValueError(
            "Dieses Angebot kann nicht "
            "storniert werden."
        )

    if item.commission_id is not None:
        raise ValueError(
            "Das Angebot besitzt bereits einen "
            "Auftrag. Der Auftrag muss separat "
            "bearbeitet werden."
        )

    if not normalized_reason:
        raise ValueError(
            "Für die Stornierung ist eine "
            "Begründung erforderlich."
        )

    item.status = OfferStatus.CANCELED.value
    item.status_note = normalized_reason
    item.canceled_by_user_id = user.id
    item.canceled_at = utc_now()


def offer_commission_draft_data(
    item: Offer,
) -> dict[str, Any]:
    if (
        item.status
        != OfferStatus.AWAITING_RESPONSE.value
    ):
        raise ValueError(
            "Nur ein Angebot mit dem Status "
            "„Warte auf Rückmeldung“ kann "
            "angenommen werden."
        )

    if (
        item.valid_until is not None
        and item.valid_until < date.today()
    ):
        raise ValueError(
            "Das Angebot ist abgelaufen. "
            "Bitte erstellen Sie zunächst "
            "eine neue Angebotsfassung."
        )

    if item.commission_id is not None:
        raise ValueError(
            "Für dieses Angebot wurde bereits "
            "ein Auftrag angelegt."
        )

    if (
        not item.object_type
        or item.object_id is None
    ):
        raise ValueError(
            "Vor der Annahme muss dem Angebot "
            "ein Objekt zugeordnet werden."
        )

    if item.craft_activity_id is None:
        raise ValueError(
            "Vor der Annahme muss ein Gewerk "
            "ausgewählt werden."
        )

    commission_type_id = int(
        settings.get(
            SettingKeys.OFFER_COMMISSION_TYPE_ID
        )
        or 0
    )

    if commission_type_id <= 0:
        raise ValueError(
            "Die Auftragsart für angenommene "
            "Angebote ist nicht konfiguriert."
        )

    fallback = (
        OfferCraftFallback.query
        .filter_by(
            craft_activity_id=(
                item.craft_activity_id
            )
        )
        .one_or_none()
    )
    position_rows = []

    for index, position in enumerate(
        item.positions
    ):
        if (
            position.source_type
            == OfferPositionSource.CATALOG.value
        ):
            task_template = (
                position.task_template
            )

            if task_template is None:
                raise ValueError(
                    f"Für Position {index + 1} "
                    "fehlt die Leistungsposition."
                )
        elif (
                position.source_type
                in {
                    OfferPositionSource.FREE.value,
                    OfferPositionSource.MATERIAL.value,
                    OfferPositionSource.LOCAL_CATALOG.value,
                }
        ):
            if (
                fallback is None
                or fallback.task_template is None
            ):
                raise ValueError(
                    "Für das ausgewählte Gewerk "
                    "ist keine Leistungsposition "
                    "„Arbeit nach Vorgabe“ "
                    "hinterlegt."
                )

            task_template = (
                fallback.task_template
            )

        else:
            raise ValueError(
                f"Die Quelle der Position "
                f"{index + 1} ist ungültig."
            )

        craft_activity = (
            task_template.craft_activity
        )

        if (
            not task_template.active
            or craft_activity is None
            or not craft_activity.active
            or not craft_activity.app_enabled
            or craft_activity.erp_id is None
            or (
                task_template.craft_activity_id
                != item.craft_activity_id
            )
            or (
                task_template
                .erp_service_catalogue_id
                is None
            )
        ):
            raise ValueError(
                f"Die Leistungsposition "
                f"{index + 1} ist für das "
                "gewählte Gewerk nicht verfügbar."
            )

        position_rows.append(
            {
                "article_number": position.article_number,
                "service_catalog_id": (
                    task_template
                    .erp_service_catalogue_id
                ),
                "commission_text": (
                    position.description
                ),
                "internal_description": (
                    position.details or ""
                ),
                "units": float(
                    position.quantity
                ),
            }
        )

    if not position_rows:
        raise ValueError(
            "Das Angebot besitzt keine Positionen."
        )

    assignment_type = (
        CommissionAssignmentType.EXTERNAL.value
        if (
            item.craft_activity is not None
            and item.craft_activity
            .default_assignment_type
            == CommissionAssignmentType.EXTERNAL.value
        )
        else CommissionAssignmentType.INTERNAL.value
    )

    return {
        "form_values": {
            "commission_type_id": (
                commission_type_id
            ),
            "craft_activity_id": (
                item.craft_activity_id
            ),
            "assignment_type": (
                assignment_type
            ),
            "company_code_erp_id": (
                item.company_code_erp_id
            ),
            "priority": "normal",
            "title": (
                item.title
                or (
                    "Auftrag aus Angebot "
                    f"{item.offer_number}"
                )
            ),
            "damage_location": (
                item.object_location or ""
            ),
            "contact_person": "",
        },
        "position_rows": position_rows,
    }


def accept_offer(
    item: Offer,
    commission: Commission,
    user: User,
) -> None:
    offer_commission_draft_data(item)

    item.commission = commission
    item.status = OfferStatus.IN_PROGRESS.value
    item.status_note = None
    item.accepted_by_user_id = user.id
    item.accepted_at = utc_now()


def sync_offer_from_commission(
    commission: Commission,
) -> Offer | None:
    item = (
        Offer.query
        .filter_by(
            commission_id=commission.id
        )
        .one_or_none()
    )

    if item is None:
        return None

    previous_status = item.status
    invoice_created = False

    if (
        item.invoice is not None
        and item.invoice.status
        == InvoiceStatus.FINALIZED.value
    ):
        item.status = OfferStatus.INVOICED.value
        item.status_note = None

        if item.invoiced_at is None:
            item.invoiced_at = (
                item.invoice.finalized_at
                or utc_now()
            )
    elif (
        item.invoice is not None
        and item.invoice.status
        == InvoiceStatus.CANCELED.value
    ):
        item.status = (
            OfferStatus
            .REVIEW_REQUIRED.value
        )

        if not item.status_note:
            item.status_note = (
                "Die zum Angebot gehörende "
                "Rechnung wurde storniert."
            )

        item.invoiced_at = None
    elif (
        item.invoice is None and commission.local_status == CommissionLocalStatus.TENANT_CHARGE_REVIEW.value
        and commission.tenant_charge_default()
    ):
        item.status = OfferStatus.REVIEW_REQUIRED.value
        item.status_note = "Die Mieterbelastung ist in der Auftragsprüfung offen."
    elif commission.local_status in {
        CommissionLocalStatus.DONE.value,
        (
            CommissionLocalStatus
            .TENANT_CHARGE_REVIEW.value
        ),
    }:
        if item.invoice is None:
            try:
                with db.session.begin_nested():
                    from .invoice_service import (
                        create_offer_invoice_draft,
                    )

                    invoice = (
                        create_offer_invoice_draft(
                            item
                        )
                    )
                    db.session.flush()
                    invoice_created = True

            except Exception as exc:
                current_app.logger.exception(
                    "offer_invoice_draft_failed "
                    "offer_id=%s commission_id=%s",
                    item.id,
                    commission.id,
                )

                item.status = (
                    OfferStatus
                    .REVIEW_REQUIRED.value
                )
                item.status_note = (
                    "Der Auftrag wurde erfolgreich "
                    "abgeschlossen, der Rechnungsentwurf "
                    "konnte jedoch nicht erzeugt werden: "
                    f"{str(exc)[:1000]}"
                )

                audit(
                    "offer.invoice.create_failed",
                    "offer",
                    item.id,
                    {
                        "commission_id": (
                            commission.id
                        ),
                        "error": str(exc)[:1000],
                    },
                )

                return item

        item.status = (
            OfferStatus
            .READY_FOR_INVOICE.value
        )
        item.status_note = None

    elif commission.local_status in {
        (
            CommissionLocalStatus
            .COMPLETION_REVIEW.value
        ),
        CommissionLocalStatus.CANCELED.value,
        CommissionLocalStatus.DELETED.value,
    }:
        item.status = (
            OfferStatus
            .REVIEW_REQUIRED.value
        )

        if commission.local_status == CommissionLocalStatus.CANCELED.value:
            item.status_note = "Der zugehörige Auftrag wurde storniert."
        elif commission.completion_reason:
            item.status_note = (
                commission.completion_reason
            )
        elif (
            commission.local_status
            == CommissionLocalStatus.DELETED.value
        ):
            item.status_note = (
                "Der zugehörige Auftrag wurde "
                "in der lokalen Synchronisierung "
                "als gelöscht erkannt."
            )
        else:
            item.status_note = (
                "Der Auftrag konnte nicht "
                "erfolgreich abgeschlossen werden."
            )

    elif commission.local_status in {
        CommissionLocalStatus.RECORDED.value,
        (
            CommissionLocalStatus
            .WAITING_FOR_ACCEPTANCE.value
        ),
        CommissionLocalStatus.OPEN.value,
        CommissionLocalStatus.IN_PROGRESS.value,
    }:
        if item.invoice is None:
            item.status = (
                OfferStatus.IN_PROGRESS.value
            )
            item.status_note = None

    if (
        item.status != previous_status
        or invoice_created
    ):
        audit(
            "offer.commission_status_sync",
            "offer",
            item.id,
            {
                "commission_id": commission.id,
                "commission_status": (
                    commission.local_status
                ),
                "previous_offer_status": (
                    previous_status
                ),
                "offer_status": item.status,
                "invoice_id": item.invoice_id,
                "invoice_created": (
                    invoice_created
                ),
            },
        )

    return item


def reconcile_offer_workflows(
    offer_id: int | None = None,
) -> dict[str, int]:
    result = {
        "seen": 0,
        "changed": 0,
        "unchanged": 0,
        "expired": 0,
        "invoices_created": 0,
        "errors": 0,
    }

    query = Offer.query.order_by(
        Offer.id.asc()
    )

    if offer_id is not None:
        query = query.filter(
            Offer.id == offer_id
        )

    for item in query.all():
        result["seen"] += 1
        previous_status = item.status
        previous_invoice_id = (
            item.invoice_id
        )

        try:
            with db.session.begin_nested():
                if (
                    item.status
                    == OfferStatus
                    .AWAITING_RESPONSE.value
                    and item.valid_until
                    is not None
                    and item.valid_until
                    < date.today()
                    and item.commission_id
                    is None
                ):
                    item.status = (
                        OfferStatus
                        .EXPIRED.value
                    )
                    item.status_note = (
                        "Die Angebotsgültigkeit "
                        "ist abgelaufen."
                    )

                    audit(
                        "offer.expire",
                        "offer",
                        item.id,
                    )

                    result["expired"] += 1

                elif (
                    item.commission
                    is not None
                ):
                    sync_offer_from_commission(
                        item.commission
                    )

                db.session.flush()

        except Exception:
            result["errors"] += 1

            current_app.logger.exception(
                "offer_reconciliation_failed "
                "offer_id=%s "
                "commission_id=%s",
                item.id,
                item.commission_id,
            )

            continue

        invoice_created = (
            previous_invoice_id is None
            and item.invoice_id is not None
        )
        status_changed = (
            previous_status != item.status
        )

        if invoice_created:
            result[
                "invoices_created"
            ] += 1

        if (
            status_changed
            or invoice_created
        ):
            result["changed"] += 1
        else:
            result["unchanged"] += 1

    return result

