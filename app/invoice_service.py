from __future__ import annotations

from collections import defaultdict
from datetime import (
    date,
    datetime,
    timedelta,
)
from decimal import (
    Decimal,
    ROUND_HALF_UP,
)
import re
from typing import Any

from sqlalchemy import (
    func,
    inspect as sqlalchemy_inspect,
    or_,
    select,
)

from sqlalchemy.orm import aliased

from .audit import audit
from .erp_sync import enqueue_erp_operation
from .extensions import db
from .invoice_document import (
    InvoiceDocumentError,
    InvoiceDocumentResult,
    create_invoice_document,
    remove_invoice_documents,
)
from .models import (
    Commission,
    CommissionLocalStatus,
    ErpOperationType,
    InventoryMovementType,
    Invoice,
    InvoiceBillingRule,
    InvoiceBillingRuleType,
    InvoiceCommission,
    InvoiceDocumentType,
    InvoiceKind,
    InvoicePosition,
    InvoicePositionSource,
    InvoiceStatus,
    MaterialUsageStatus,
    TimeReviewStatus,
    User,
    WowiCompanyCode,
    WowiLicenseAgreement,
    utc_now,
    Offer,
    OfferPositionSource,
    OfferStatus,
)
from .settings import SettingKeys, settings
from .invoice_payments import freeze_invoice_payment_information, invoice_payment_context
from .tenant_billing import (
    agreement_company_code,
    agreement_uses_tenant_billing,
    apply_contract_recipient,
    apply_tenant_billing,
    tenant_billing_agreement,
    tenant_billing_issuer,
)


INVOICE_TIME_TYPES = (
    (
        "work_minutes",
        "Arbeitszeit",
    ),
    (
        "waiting_minutes",
        "Wartezeit",
    ),
    (
        "material_minutes",
        "Materialbeschaffung",
    ),
    (
        "travel_minutes",
        "Fahrtzeit",
    ),
    (
        "documentation_minutes",
        "Dokumentationszeit",
    ),
    (
        "extra_minutes",
        "Zusatzzeit",
    ),
)


def available_invoice_commissions(
) -> list[Commission]:
    reserved_commission_ids = (
        db.session.query(
            InvoiceCommission.commission_id
        )
        .join(
            Invoice,
            Invoice.id
            == InvoiceCommission.invoice_id,
        )
        .filter(
            Invoice.status
            != InvoiceStatus.CANCELED.value
        )
    )

    issuer = aliased(WowiCompanyCode)
    return (
        Commission.query
        .join(WowiCompanyCode, WowiCompanyCode.erp_id == Commission.company_code_erp_id)
        .join(issuer, issuer.id == func.coalesce(WowiCompanyCode.tenant_billing_issuer_id, WowiCompanyCode.id))
        .filter(
            WowiCompanyCode.active.is_(True),
            issuer.active.is_(True),
            issuer.document_creation_disabled.is_(False),
            Commission.deleted_at.is_(None),
            Commission.local_status
            == CommissionLocalStatus
            .TENANT_CHARGE_REVIEW.value,
            Commission.id.notin_(
                reserved_commission_ids
            ),
        )
        .order_by(
            Commission.tenant_name.asc(),
            Commission.erp_idnum.asc(),
            Commission.id.asc(),
        )
        .all()
    )


def create_free_invoice_draft(
        company_code: WowiCompanyCode,
        created_by: User,
) -> Invoice:
    return create_invoice_draft(
        company_code=company_code,
        created_by=created_by,
        commissions=[],
    )


def create_agreement_invoice_draft(
    agreement: WowiLicenseAgreement, recipient: dict, created_by: User,
) -> Invoice:
    company, issuer = invoice_agreement_companies(agreement)
    invoice = create_free_invoice_draft(issuer, created_by)
    _apply_agreement_invoice_recipient(invoice, agreement, company, recipient)
    return invoice


def invoice_agreement_companies(
    agreement: WowiLicenseAgreement, invoice: Invoice | None = None,
) -> tuple[WowiCompanyCode, WowiCompanyCode]:
    company = agreement_company_code(agreement)
    existing_issuer_id = invoice.company_code_id if invoice is not None else None
    issuer = tenant_billing_issuer(company, existing_issuer_id=existing_issuer_id)
    if invoice is not None:
        for link in invoice.commission_links:
            commission = link.commission
            if (
                commission.company_code_erp_id != company.erp_id
                or commission.license_agreement_id not in (None, agreement.erp_id)
                or commission.license_agreement_idnum not in (None, "", agreement.id_num)
            ):
                raise ValueError("Der Mietvertrag passt nicht zum verknüpften Auftrag.")

        offer = Offer.query.filter_by(invoice_id=invoice.id).one_or_none()
        if offer is not None and (
            offer.company_code_id != company.id
            or offer.license_agreement_erp_id not in (None, agreement.erp_id)
            or offer.license_agreement_idnum not in (None, "", agreement.id_num)
        ):
            raise ValueError("Der Mietvertrag passt nicht zum verknüpften Angebot.")
    return company, issuer


def assign_invoice_agreement(invoice: Invoice, agreement: WowiLicenseAgreement, recipient: dict) -> None:
    if not invoice.can_assign_agreement:
        raise ValueError("Ein Mietvertrag kann nur einem Rechnungsentwurf ohne Vertrag zugeordnet werden.")
    company, issuer = invoice_agreement_companies(agreement, invoice)
    _apply_agreement_invoice_recipient(invoice, agreement, company, recipient)
    _set_invoice_issuer(invoice, issuer)
    invoice.payment_information = None
    invoice.payment_reference = None


def _apply_agreement_invoice_recipient(
    invoice: Invoice, agreement: WowiLicenseAgreement, company: WowiCompanyCode, recipient: dict,
) -> None:
    if company.tenant_billing_issuer_id is not None:
        apply_tenant_billing(invoice, agreement, company, recipient)
    else:
        apply_contract_recipient(invoice, agreement, recipient)


def create_offer_invoice_draft(
    offer: Offer,
) -> Invoice:
    if offer.commission is None:
        raise ValueError(
            "Dem Angebot ist kein Auftrag "
            "zugeordnet."
        )

    if offer.invoice is not None:
        return offer.invoice

    created_by = (
        offer.accepted_by
        or offer.created_by
    )

    if created_by is None:
        raise ValueError(
            "Für den Rechnungsentwurf fehlt "
            "der zuständige Benutzer."
        )

    agreement_id = offer.license_agreement_erp_id or offer.commission.license_agreement_id
    if offer.company_code.tenant_billing_issuer_id is not None and agreement_id is not None:
        agreement = tenant_billing_agreement(agreement_id)
        company = agreement_company_code(agreement)
        if company.id != offer.company_code_id:
            raise ValueError("Mietvertrag und Buchwerk des Angebots stimmen nicht überein.")
        invoice = create_free_invoice_draft(tenant_billing_issuer(company), created_by)
        recipient = {"name": offer.tenant_name or offer.commission.tenant_name}
        if offer.license_agreement_erp_id == agreement_id and offer.recipient_person_erp_id is not None:
            recipient = {
                field: getattr(offer, "recipient_" + field)
                for field in ("name", "name_addition", "street", "postal_code", "town", "country_code")
            }
            recipient["person_id"] = offer.recipient_person_erp_id
        apply_tenant_billing(invoice, agreement, company, recipient)
    else:
        invoice = create_free_invoice_draft(offer.company_code, created_by)
        for field in ("type", "person_erp_id", "name", "name_addition", "street", "postal_code",
                      "town", "country_code", "email", "vat_id"):
            attribute = "recipient_" + field
            setattr(invoice, attribute, getattr(offer, attribute))
        invoice.recipient_license_agreement_erp_id = offer.license_agreement_erp_id
        invoice.recipient_license_agreement_idnum = offer.license_agreement_idnum

    service_date = (
        offer.commission.completed_at
        or offer.commission.execution_to
        or offer.commission.execution_from
    )

    if isinstance(service_date, datetime):
        service_date = service_date.date()

    if not isinstance(service_date, date):
        service_date = date.today()

    invoice.service_date_from = service_date
    invoice.service_date_to = service_date
    invoice.introductory_text = (
        "Für die ausgeführten und im Angebot "
        f"{offer.offer_number} vereinbarten "
        "Leistungen berechnen wir:"
    )
    invoice.commission_links.append(
        InvoiceCommission(
            commission=offer.commission
        )
    )

    rows = []

    for position in offer.positions:
        source_type = position.source_type

        rows.append(
            {
                "source_type": source_type,
                "task_template_id": (
                    position.task_template_id
                ),
                "inventory_item_id": position.inventory_item_id,
                "local_catalog_item_id": position.local_catalog_item_id,
                "source_data": {
                    **(position.source_data or {}),
                    "source_key": (
                        "offer-position:"
                        f"{position.id}"
                    ),
                    "offer_id": offer.id,
                    "offer_position_id": position.id,
                    "commission_id": offer.commission_id,
                },
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
        )

    if not rows:
        raise ValueError(
            "Das Angebot besitzt keine "
            "abrechenbaren Positionen."
        )

    replace_invoice_positions(
        invoice,
        rows,
    )

    offer.invoice = invoice

    return invoice


def create_commission_invoice_draft(
        commissions: list[Commission],
        created_by: User,
) -> Invoice:
    if not commissions:
        raise ValueError(
            "Mindestens ein Auftrag "
            "muss ausgewählt werden."
        )

    validate_commission_selection(
        commissions
    )

    company_code_erp_id = (
        commissions[0].company_code_erp_id
    )
    company_code = (
        WowiCompanyCode.query
        .filter_by(
            erp_id=company_code_erp_id,
            active=True,
        )
        .one_or_none()
    )

    if company_code is None:
        raise ValueError(
            "Das Buchwerk des Auftrags "
            "ist nicht verfügbar."
        )

    issuer = tenant_billing_issuer(company_code)
    invoice = create_invoice_draft(
        company_code=issuer,
        created_by=created_by,
        commissions=commissions,
    )

    if company_code.tenant_billing_issuer_id is not None:
        commission = commissions[0]
        agreement = tenant_billing_agreement(commission.license_agreement_id)
        if agreement_company_code(agreement).id != company_code.id:
            raise ValueError("Mietvertrag und Buchwerk des Auftrags stimmen nicht überein.")
        apply_tenant_billing(invoice, agreement, company_code, {"name": commission.tenant_name})

    return invoice


def validate_commission_selection(
        commissions: list[Commission],
) -> None:
    available_ids = {
        item.id
        for item
        in available_invoice_commissions()
    }

    selected_ids = {
        item.id
        for item in commissions
    }

    if not selected_ids.issubset(
            available_ids
    ):
        raise ValueError(
            "Mindestens ein ausgewählter Auftrag "
            "ist nicht mehr zur Abrechnung verfügbar."
        )

    company_code_ids = {
        item.company_code_erp_id
        for item in commissions
    }

    if (
            len(company_code_ids) != 1
            or None in company_code_ids
    ):
        raise ValueError(
            "Die ausgewählten Aufträge müssen "
            "demselben Buchwerk angehören."
        )

    agreement_references = {
        (
            item.license_agreement_id,
            item.license_agreement_idnum,
        )
        for item in commissions
    }

    if len(agreement_references) != 1:
        raise ValueError(
            "Die ausgewählten Aufträge müssen "
            "demselben Vertrag angehören."
        )

    agreement_id, agreement_idnum = next(
        iter(agreement_references)
    )

    if (
            len(commissions) > 1
            and agreement_id is None
            and not agreement_idnum
    ):
        raise ValueError(
            "Aufträge ohne Vertrag können nur "
            "einzeln übernommen werden."
        )


def create_invoice_draft(
        company_code: WowiCompanyCode,
        created_by: User,
        commissions: list[Commission],
) -> Invoice:
    if not company_code.active or company_code.document_creation_disabled:
        raise ValueError("Für dieses Buchwerk können keine neuen Rechnungen erstellt werden.")

    first_commission = (
        commissions[0]
        if commissions
        else None
    )

    service_dates = invoice_service_dates(
        commissions
    )

    invoice_date = date.today()
    payment_terms_days = (
        company_code
        .invoice_payment_terms_days
    )

    if payment_terms_days is None:
        payment_terms_days = settings.get(
            SettingKeys
            .INVOICE_PAYMENT_TERMS_DAYS
        )

    invoice = Invoice(
        status=InvoiceStatus.DRAFT.value,
        invoice_kind=InvoiceKind.INVOICE.value,
        document_type=(
            InvoiceDocumentType.INVOICE.value
        ),
        recipient_person_erp_id=None,
        recipient_license_agreement_erp_id=(
            first_commission
            .license_agreement_id
            if first_commission
            else None
        ),
        recipient_license_agreement_idnum=(
            first_commission
            .license_agreement_idnum
            if first_commission
            else None
        ),
        recipient_name=(
            first_commission.tenant_name
            if first_commission
            else None
        ),
        recipient_country_code="DE",
        currency_code="EUR",
        service_date_from=(
            min(service_dates)
            if service_dates
            else None
        ),
        service_date_to=(
            max(service_dates)
            if service_dates
            else None
        ),
        introductory_text=(
            "Für die ausgeführten Arbeiten "
            "berechnen wir Ihnen folgende Leistungen:"
        ),
        created_by=created_by,
        invoice_date=invoice_date,
        responsible_user=created_by,
        due_date=(invoice_date + timedelta(days=payment_terms_days)),
    )

    _set_invoice_issuer(invoice, company_code)
    db.session.add(invoice)

    for commission in commissions:
        invoice.commission_links.append(
            InvoiceCommission(
                commission=commission
            )
        )

    add_invoice_suggestions(
        invoice
    )

    return invoice


def _set_invoice_issuer(invoice: Invoice, company_code: WowiCompanyCode) -> None:
    invoice.company_code = company_code
    invoice.company_code_id = company_code.id
    invoice.company_code_erp_id = company_code.erp_id
    invoice.company_code_code = company_code.code
    for field in (
        "seller_name", "seller_street", "seller_postal_code", "seller_town", "seller_country_code",
        "seller_vat_id", "seller_tax_number", "seller_email", "seller_phone", "seller_iban", "seller_bic",
    ):
        setattr(invoice, field, getattr(company_code, field))


def invoice_service_dates(
        commissions: list[Commission],
) -> list[date]:
    values = []

    for commission in commissions:
        value = (
                commission.completed_at
                or commission.execution_to
                or commission.execution_from
        )

        if isinstance(value, datetime):
            values.append(value.date())
        elif isinstance(value, date):
            values.append(value)

    return values


def add_invoice_suggestions(
    invoice: Invoice,
) -> int:
    if (
        invoice.status
        != InvoiceStatus.DRAFT.value
    ):
        raise ValueError(
            "Vorschläge können nur einem "
            "Entwurf hinzugefügt werden."
        )

    existing_source_keys = {
        str(
            position.source_data.get(
                "source_key"
            )
        )
        for position in invoice.positions
        if (
            isinstance(
                position.source_data,
                dict,
            )
            and position.source_data.get(
                "source_key"
            )
        )
    }

    rule_map = invoice_billing_rule_map(
        invoice.company_code
    )
    suggestions = []

    for link in invoice.commission_links:
        suggestions.extend(
            invoice_time_suggestions(
                invoice,
                link.commission,
                rule_map,
            )
        )
        suggestions.extend(
            invoice_material_suggestions(
                invoice,
                link.commission,
            )
        )

    added_count = 0

    for suggestion in suggestions:
        source_key = suggestion[
            "source_data"
        ]["source_key"]

        if source_key in existing_source_keys:
            continue

        append_invoice_position(
            invoice,
            suggestion,
        )
        existing_source_keys.add(
            source_key
        )
        added_count += 1

    recalculate_invoice(
        invoice
    )

    return added_count


def invoice_time_suggestions(
    invoice: Invoice,
    commission: Commission,
    rule_map: dict[
        tuple[
            int | None,
            str,
            str | None,
            int | None,
        ],
        InvoiceBillingRule,
    ],
) -> list[dict[str, Any]]:
    valid_review_states = {
        TimeReviewStatus.OK.value,
        TimeReviewStatus.APPROVED.value,
    }
    task_groups: dict[
        tuple[int, int],
        dict[str, Any],
    ] = {}
    time_groups: dict[
        tuple[int, str],
        dict[str, Any],
    ] = {}

    for entry in commission.time_entries:
        if (
            entry.review_status
            not in valid_review_states
        ):
            continue

        task_rule = None

        if entry.task_template_id is not None:
            task_rule = resolve_invoice_rule(
                rule_map,
                invoice.company_code_id,
                InvoiceBillingRuleType
                .TASK_TEMPLATE.value,
                task_template_id=(
                    entry.task_template_id
                ),
            )

        if task_rule is not None:
            if not task_rule.enabled:
                continue

            group_key = (
                commission.id,
                entry.task_template_id,
            )
            group = task_groups.setdefault(
                group_key,
                {
                    "minutes": 0,
                    "entry_ids": [],
                    "task_template": (
                        entry.task_template
                    ),
                    "rule": task_rule,
                },
            )
            group["minutes"] += (
                entry.minutes
            )
            group["entry_ids"].append(
                entry.id
            )
            continue

        components = {
            key: int(
                getattr(entry, key, 0)
                or 0
            )
            for key, label
            in INVOICE_TIME_TYPES
        }

        if sum(components.values()) <= 0:
            components[
                "work_minutes"
            ] = entry.minutes

        for time_type, default_title in (
            INVOICE_TIME_TYPES
        ):
            minutes = components[
                time_type
            ]

            if minutes <= 0:
                continue

            rule = resolve_invoice_rule(
                rule_map,
                invoice.company_code_id,
                InvoiceBillingRuleType
                .TIME_TYPE.value,
                time_type=time_type,
            )

            if (
                rule is not None
                and not rule.enabled
            ):
                continue

            group_key = (
                commission.id,
                time_type,
            )
            group = time_groups.setdefault(
                group_key,
                {
                    "minutes": 0,
                    "entry_ids": [],
                    "time_type": time_type,
                    "default_title": (
                        default_title
                    ),
                    "rule": rule,
                },
            )
            group["minutes"] += minutes
            group["entry_ids"].append(
                entry.id
            )

    suggestions = []

    for (
        commission_id,
        task_template_id,
    ), group in task_groups.items():
        task_template = group[
            "task_template"
        ]
        rule = group["rule"]
        title = (
            rule.position_title
            or (
                task_template.title
                if task_template
                else "Arbeitsleistung"
            )
        )
        hourly_rate = invoice_hourly_rate(
            invoice,
            rule,
        )
        vat_rate = invoice_vat_rate(
            invoice,
            rule,
        )
        source_key = (
            f"time:commission:{commission_id}:"
            f"task:{task_template_id}"
        )

        suggestions.append(
            {
                "source_type": (
                    InvoicePositionSource
                    .TIME.value
                ),
                "task_template_id": (
                    task_template_id
                ),
                "description": title,
                "details": (
                    invoice_commission_reference(
                        commission
                    )
                ),
                "quantity": invoice_hours(
                    group["minutes"]
                ),
                "unit": "Std.",
                "unit_price_net": (
                    hourly_rate
                ),
                "vat_rate": vat_rate,
                "source_data": {
                    "source_key": source_key,
                    "commission_id": (
                        commission.id
                    ),
                    "time_entry_ids": (
                        group["entry_ids"]
                    ),
                    "minutes": (
                        group["minutes"]
                    ),
                    "rule_type": (
                        InvoiceBillingRuleType
                        .TASK_TEMPLATE.value
                    ),
                },
            }
        )

    for (
        commission_id,
        time_type,
    ), group in time_groups.items():
        rule = group["rule"]
        title = (
            rule.position_title
            if (
                rule is not None
                and rule.position_title
            )
            else group["default_title"]
        )
        hourly_rate = invoice_hourly_rate(
            invoice,
            rule,
        )
        vat_rate = invoice_vat_rate(
            invoice,
            rule,
        )
        source_key = (
            f"time:commission:{commission_id}:"
            f"type:{time_type}"
        )

        suggestions.append(
            {
                "source_type": (
                    InvoicePositionSource
                    .TIME.value
                ),
                "description": title,
                "details": (
                    invoice_commission_reference(
                        commission
                    )
                ),
                "quantity": invoice_hours(
                    group["minutes"]
                ),
                "unit": "Std.",
                "unit_price_net": (
                    hourly_rate
                ),
                "vat_rate": vat_rate,
                "source_data": {
                    "source_key": source_key,
                    "commission_id": (
                        commission.id
                    ),
                    "time_entry_ids": (
                        group["entry_ids"]
                    ),
                    "minutes": (
                        group["minutes"]
                    ),
                    "time_type": time_type,
                    "rule_type": (
                        InvoiceBillingRuleType
                        .TIME_TYPE.value
                    ),
                },
            }
        )

    return suggestions


def invoice_material_suggestions(
    invoice: Invoice,
    commission: Commission,
) -> list[dict[str, Any]]:
    valid_review_states = {
        TimeReviewStatus.OK.value,
        TimeReviewStatus.APPROVED.value,
    }
    groups = defaultdict(
        lambda: {
            "quantity": Decimal("0"),
            "movement_ids": [],
            "item": None,
        }
    )

    for movement in (
        commission.inventory_movements
    ):
        if (
            movement.movement_type
            != InventoryMovementType
            .WITHDRAWAL.value
            or movement.usage_status
            != MaterialUsageStatus
            .INSTALLED.value
            or movement.review_status
            not in valid_review_states
            or movement.quantity <= 0
        ):
            continue

        group = groups[
            movement.item_id
        ]
        group["quantity"] += as_decimal(
            movement.quantity
        )
        group["movement_ids"].append(
            movement.id
        )
        group["item"] = movement.item

    suggestions = []
    markup = invoice_material_markup(
        invoice
    )

    for item_id, group in groups.items():
        item = group["item"]

        if item is None:
            continue

        base_price = as_decimal(
            item.billing_base_price_net
        )
        unit_price = (
            base_price
            * (
                Decimal("1")
                + markup
                / Decimal("100")
            )
        ).quantize(
            Decimal("0.0001"),
            rounding=ROUND_HALF_UP,
        )
        vat_rate = (
            as_decimal(
                item.billing_vat_rate
            )
            if (
                item.billing_vat_rate
                is not None
            )
            else invoice_vat_rate(
                invoice,
                None,
            )
        )
        source_key = (
            f"material:commission:"
            f"{commission.id}:item:{item_id}"
        )

        suggestions.append(
            {
                "source_type": (
                    InvoicePositionSource
                    .MATERIAL.value
                ),
                "inventory_item_id": item_id,
                "description": item.name,
                "details": (
                    invoice_commission_reference(
                        commission
                    )
                ),
                "quantity": group["quantity"],
                "unit": item.unit or "Stk",
                "unit_price_net": unit_price,
                "vat_rate": vat_rate,
                "source_data": {
                    "source_key": source_key,
                    "commission_id": (
                        commission.id
                    ),
                    "inventory_movement_ids": (
                        group["movement_ids"]
                    ),
                    "material_markup_percent": (
                        str(markup)
                    ),
                },
            }
        )

    return suggestions


def invoice_billing_rule_map(
    company_code: WowiCompanyCode,
) -> dict[
    tuple[
        int | None,
        str,
        str | None,
        int | None,
    ],
    InvoiceBillingRule,
]:
    rules = (
        InvoiceBillingRule.query
        .filter(
            or_(
                InvoiceBillingRule
                .company_code_id
                .is_(None),
                InvoiceBillingRule
                .company_code_id
                == company_code.id,
            )
        )
        .all()
    )

    return {
        (
            rule.company_code_id,
            rule.rule_type,
            rule.time_type,
            rule.task_template_id,
        ): rule
        for rule in rules
    }


def resolve_invoice_rule(
    rule_map: dict[
        tuple[
            int | None,
            str,
            str | None,
            int | None,
        ],
        InvoiceBillingRule,
    ],
    company_code_id: int,
    rule_type: str,
    time_type: str | None = None,
    task_template_id: int | None = None,
) -> InvoiceBillingRule | None:
    specific_key = (
        company_code_id,
        rule_type,
        time_type,
        task_template_id,
    )
    global_key = (
        None,
        rule_type,
        time_type,
        task_template_id,
    )

    return (
        rule_map.get(specific_key)
        or rule_map.get(global_key)
    )


def invoice_hourly_rate(
    invoice: Invoice,
    rule: InvoiceBillingRule | None,
) -> Decimal:
    if (
        rule is not None
        and rule.hourly_rate_net
        is not None
    ):
        return as_decimal(
            rule.hourly_rate_net
        )

    if (
        invoice.company_code
        .invoice_default_hourly_rate_net
        is not None
    ):
        return as_decimal(
            invoice.company_code
            .invoice_default_hourly_rate_net
        )

    return as_decimal(
        settings.get(
            SettingKeys
            .INVOICE_DEFAULT_HOURLY_RATE_NET
        )
    )


def invoice_vat_rate(
    invoice: Invoice,
    rule: InvoiceBillingRule | None,
) -> Decimal:
    if (
        rule is not None
        and rule.vat_rate is not None
    ):
        return as_decimal(
            rule.vat_rate
        )

    if (
        invoice.company_code
        .invoice_default_vat_rate
        is not None
    ):
        return as_decimal(
            invoice.company_code
            .invoice_default_vat_rate
        )

    return as_decimal(
        settings.get(
            SettingKeys
            .INVOICE_DEFAULT_VAT_RATE
        )
    )


def invoice_material_markup(
    invoice: Invoice,
) -> Decimal:
    if (
        invoice.company_code
        .invoice_material_markup_percent
        is not None
    ):
        return as_decimal(
            invoice.company_code
            .invoice_material_markup_percent
        )

    return as_decimal(
        settings.get(
            SettingKeys
            .INVOICE_MATERIAL_MARKUP_PERCENT
        )
    )


def invoice_hours(
    minutes: int,
) -> Decimal:
    return (
        Decimal(minutes)
        / Decimal("60")
    ).quantize(
        Decimal("0.0001"),
        rounding=ROUND_HALF_UP,
    )


def invoice_commission_reference(
    commission: Commission,
) -> str:
    reference = (
        commission.erp_idnum
        or commission.code
        or str(commission.id)
    )

    return f"Auftrag {reference}"


def append_invoice_position(
    invoice: Invoice,
    values: dict[str, Any],
) -> InvoicePosition:
    quantity = as_decimal(
        values.get("quantity")
    )
    unit_price_net = as_decimal(
        values.get("unit_price_net")
    )
    vat_rate = as_decimal(
        values.get("vat_rate")
    )
    net_amount = money(
        quantity * unit_price_net
    )
    vat_amount = money(
        net_amount
        * vat_rate
        / Decimal("100")
    )
    gross_amount = money(
        net_amount + vat_amount
    )
    position_index = (
        max(
            (
                position.position_index
                for position
                in invoice.positions
            ),
            default=-1,
        )
        + 1
    )

    position = InvoicePosition(
        position_index=position_index,
        source_type=values.get(
            "source_type",
            InvoicePositionSource.FREE.value,
        ),
        task_template_id=values.get(
            "task_template_id"
        ),
        inventory_item_id=values.get(
            "inventory_item_id"
        ),
        local_catalog_item_id=values.get("local_catalog_item_id"),
        source_data=values.get(
            "source_data"
        ),
        description=str(
            values.get(
                "description",
                "",
            )
        ).strip(),
        details=(
            str(values.get("details")).strip()
            if values.get("details")
            else None
        ),
        quantity=quantity,
        unit=(
            str(
                values.get(
                    "unit",
                    "Stk",
                )
            ).strip()
            or "Stk"
        ),
        unit_price_net=unit_price_net,
        vat_rate=vat_rate,
        net_amount=net_amount,
        vat_amount=vat_amount,
        gross_amount=gross_amount,
    )
    invoice.positions.append(
        position
    )

    return position


def replace_invoice_positions(
    invoice: Invoice,
    rows: list[dict[str, Any]],
) -> None:
    invoice.positions.clear()
    db.session.flush()

    for row in rows:
        append_invoice_position(
            invoice,
            row,
        )

    recalculate_invoice(
        invoice
    )


def recalculate_invoice(
    invoice: Invoice,
) -> None:
    invoice.total_net = money(
        sum(
            (
                as_decimal(
                    position.net_amount
                )
                for position
                in invoice.positions
            ),
            Decimal("0"),
        )
    )
    invoice.total_vat = money(
        sum(
            (
                as_decimal(
                    position.vat_amount
                )
                for position
                in invoice.positions
            ),
            Decimal("0"),
        )
    )
    invoice.total_gross = money(
        invoice.total_net
        + invoice.total_vat
    )


def as_decimal(
    value: Any,
) -> Decimal:
    if value is None or value == "":
        return Decimal("0")

    if isinstance(value, Decimal):
        return value

    return Decimal(str(value))


def money(
    value: Any,
) -> Decimal:
    return as_decimal(value).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )


class InvoiceFinalizationError(RuntimeError):
    pass


def _finalization_decimal(
    value,
) -> Decimal:
    if value in (None, ""):
        return Decimal("0")

    if isinstance(value, Decimal):
        return value

    return Decimal(
        str(value).replace(",", ".")
    )


def _validate_invoice_for_finalization(
    invoice: Invoice,
) -> None:
    if (
        invoice.status
        != InvoiceStatus.DRAFT.value
    ):
        raise InvoiceFinalizationError(
            "Nur Entwürfe können festgeschrieben werden."
        )

    if invoice.company_code_id is None:
        raise InvoiceFinalizationError(
            "Das Buchwerk fehlt."
        )

    if invoice.invoice_kind != InvoiceKind.CANCELLATION.value:
        if (
            not invoice.tenant_billing_reference
            and invoice.recipient_license_agreement_erp_id is not None
            and agreement_uses_tenant_billing(invoice.recipient_license_agreement_erp_id)
        ):
            raise InvoiceFinalizationError(
                "Für den Mietvertrag ist eine Firmenrechnung vorgesehen. "
                "Bitte verwenden Sie den Einstieg „Rechnung mit Mietvertrag“."
            )
        try:
            invoice_payment_context(invoice, complete=True)
        except ValueError as exc:
            raise InvoiceFinalizationError(str(exc)) from exc

    required_recipient_values = (
        (
            invoice.recipient_name,
            "Der Rechnungsempfänger fehlt.",
        ),
        (
            invoice.recipient_street,
            "Die Straße des Empfängers fehlt.",
        ),
        (
            invoice.recipient_postal_code,
            "Die Postleitzahl des Empfängers fehlt.",
        ),
        (
            invoice.recipient_town,
            "Der Ort des Empfängers fehlt.",
        ),
    )

    for value, message in (
        required_recipient_values
    ):
        if not str(value or "").strip():
            raise InvoiceFinalizationError(
                message
            )

    if (
        invoice.service_date_from is None
        and invoice.service_date_to is None
    ):
        raise InvoiceFinalizationError(
            "Bitte geben Sie mindestens ein "
            "Leistungsdatum an."
        )

    if invoice.service_date_from is None:
        invoice.service_date_from = (
            invoice.service_date_to
        )

    if invoice.service_date_to is None:
        invoice.service_date_to = (
            invoice.service_date_from
        )

    if (
        invoice.service_date_from
        > invoice.service_date_to
    ):
        raise InvoiceFinalizationError(
            "Das Ende des Leistungszeitraums "
            "darf nicht vor dem Beginn liegen."
        )

    positions = list(
        invoice.positions
    )

    if not positions:
        raise InvoiceFinalizationError(
            "Die Rechnung enthält keine Positionen."
        )

    for position_number, position in enumerate(
        positions,
        start=1,
    ):
        if not str(
            position.description or ""
        ).strip():
            raise InvoiceFinalizationError(
                "Bei Position "
                f"{position_number} fehlt "
                "die Bezeichnung."
            )

        quantity = _finalization_decimal(
            position.quantity
        )
        vat_rate = _finalization_decimal(
            position.vat_rate
        )

        if (
                invoice.invoice_kind
                == InvoiceKind.CANCELLATION.value
        ):
            if quantity >= 0:
                raise InvoiceFinalizationError(
                    "Die Menge der Stornoposition "
                    f"{position_number} muss "
                    "kleiner als null sein."
                )
        elif quantity <= 0:
            raise InvoiceFinalizationError(
                "Die Menge der Position "
                f"{position_number} muss "
                "größer als null sein."
            )

        if (
            vat_rate < 0
            or vat_rate > 100
        ):
            raise InvoiceFinalizationError(
                "Der Umsatzsteuersatz der "
                f"Position {position_number} "
                "ist ungültig."
            )

    recalculate_invoice(invoice)

    total_gross = _finalization_decimal(
        invoice.total_gross
    )

    if (
            invoice.invoice_kind
            == InvoiceKind.CANCELLATION.value
    ):
        if invoice.original_invoice_id is None:
            raise InvoiceFinalizationError(
                "Der Stornorechnung fehlt die "
                "Ursprungsrechnung."
            )

        if not str(
                invoice.cancellation_reason or ""
        ).strip():
            raise InvoiceFinalizationError(
                "Der Stornogrund fehlt."
            )

        if total_gross >= 0:
            raise InvoiceFinalizationError(
                "Der Gesamtbetrag der "
                "Stornorechnung muss negativ sein."
            )
    elif total_gross <= 0:
        raise InvoiceFinalizationError(
            "Der Rechnungsbetrag muss größer "
            "als null sein."
        )

    for link in invoice.commission_links:
        commission = link.commission

        if commission is None:
            raise InvoiceFinalizationError(
                "Ein verknüpfter Auftrag wurde "
                "nicht gefunden."
            )

        if (
            commission.local_status
            == CommissionLocalStatus.DELETED.value
        ):
            raise InvoiceFinalizationError(
                "Der Auftrag "
                f"{commission.erp_idnum or commission.id} "
                "ist gelöscht."
            )

        if commission.erp_id is None:
            raise InvoiceFinalizationError(
                "Der Auftrag "
                f"{commission.erp_idnum or commission.id} "
                "hat keine Wowiport-ID."
            )


def _render_invoice_number(
    company_code: WowiCompanyCode,
    year: int,
    counter: int,
) -> str:
    raw_mask = str(
        company_code.invoice_number_mask
        or "<company_code>-<year>-<counter>"
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

    digits = max(
        1,
        min(
            12,
            int(
                company_code.invoice_counter_digits
                or 5
            ),
        ),
    )

    if (
        "{year}" not in mask
        or "{counter}" not in mask
    ):
        raise InvoiceFinalizationError(
            "Die Rechnungsnummernmaske muss "
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
        raise InvoiceFinalizationError(
            "Unbekannte Platzhalter in der "
            "Rechnungsnummernmaske: "
            + ", ".join(
                sorted(unknown_fields)
            )
        )

    number = (
        mask
        .replace(
            "{company_code}",
            str(company_code.code),
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
        raise InvoiceFinalizationError(
            "Die Rechnungsnummer ist leer."
        )

    return number


def _allocate_invoice_number(
    invoice: Invoice,
    year: int,
) -> str:
    company_code = db.session.execute(
        select(WowiCompanyCode)
        .where(
            WowiCompanyCode.id
            == invoice.company_code_id
        )
        .with_for_update()
    ).scalar_one()

    if (
        company_code.invoice_counter_year
        != year
    ):
        company_code.invoice_counter_year = (
            year
        )
        company_code.invoice_counter = 0

    for _ in range(100):
        company_code.invoice_counter = (
            int(
                company_code.invoice_counter
                or 0
            )
            + 1
        )

        invoice_number = (
            _render_invoice_number(
                company_code,
                year,
                company_code.invoice_counter,
            )
        )

        existing_invoice = (
            Invoice.query
            .filter(
                Invoice.company_code_id
                == company_code.id,
                Invoice.invoice_number
                == invoice_number,
                Invoice.id != invoice.id,
            )
            .first()
        )

        if existing_invoice is None:
            invoice.company_code = company_code
            return invoice_number

    raise InvoiceFinalizationError(
        "Es konnte keine freie "
        "Rechnungsnummer ermittelt werden."
    )


def _enqueue_invoice_commissions(
    invoice: Invoice,
) -> list[int]:
    commission_ids = []

    for link in invoice.commission_links:
        commission = link.commission

        enqueue_erp_operation(
            commission,
            ErpOperationType.COMPLETE_COMMISSION,
            (
                f"invoice:{invoice.id}:"
                f"commission:{commission.id}:done"
            ),
            {
                "invoice_id": invoice.id,
                "invoice_number": (
                    invoice.invoice_number
                ),
            },
        )
        commission_ids.append(
            commission.id
        )

    return commission_ids


def finalize_invoice(
    invoice: Invoice,
    user: User,
) -> InvoiceDocumentResult:
    _validate_invoice_for_finalization(
        invoice
    )

    finalized_at = utc_now()
    invoice_date = (
        invoice.invoice_date
        or finalized_at.date()
    )
    invoice.invoice_date = invoice_date

    if (
        invoice.invoice_kind
        == InvoiceKind.CANCELLATION.value
    ):
        invoice.due_date = None
    elif invoice.due_date is None:
        payment_terms_days = (
            invoice.company_code
            .invoice_payment_terms_days
        )

        if payment_terms_days is None:
            payment_terms_days = settings.get(
                SettingKeys
                .INVOICE_PAYMENT_TERMS_DAYS
            )

        payment_terms_days = max(
            0,
            int(payment_terms_days or 0),
        )
        invoice.due_date = (
            invoice_date
            + timedelta(
                days=payment_terms_days
            )
        )

    invoice.invoice_number = (
        _allocate_invoice_number(
            invoice,
            invoice_date.year,
        )
    )
    invoice.payment_reference = (
        invoice.payment_reference
        or invoice.invoice_number
    )

    if invoice.invoice_kind != InvoiceKind.CANCELLATION.value:
        freeze_invoice_payment_information(invoice)

    db.session.flush()

    document_result = None

    try:
        document_result = (
            create_invoice_document(
                invoice
            )
        )

        invoice.document_type = (
            InvoiceDocumentType.CANCELLATION.value
            if (
                invoice.invoice_kind
                == InvoiceKind.CANCELLATION.value
            )
            else InvoiceDocumentType.INVOICE.value
        )
        invoice.document_filename = (
            document_result.pdf_storage_name
        )
        invoice.document_sha256 = (
            document_result.pdf_sha256
        )
        invoice.status = (
            InvoiceStatus.FINALIZED.value
        )
        invoice.finalized_at = finalized_at
        invoice.finalized_by_user_id = user.id

        linked_offer = (
            Offer.query
            .filter_by(
                invoice_id=invoice.id
            )
            .one_or_none()
        )

        if linked_offer is not None:
            linked_offer.status = (
                OfferStatus.INVOICED.value
            )
            linked_offer.status_note = None
            linked_offer.invoiced_at = (
                finalized_at
            )

            audit(
                "offer.invoice.finalize",
                "offer",
                linked_offer.id,
                {
                    "invoice_id": invoice.id,
                    "invoice_number": (
                        invoice.invoice_number
                    ),
                },
            )

        commission_ids = (
            _enqueue_invoice_commissions(
                invoice
            )
        )

        audit(
            "invoice.finalize",
            "invoice",
            invoice.id,
            {
                "invoice_number": (
                    invoice.invoice_number
                ),
                "company_code_id": (
                    invoice.company_code_id
                ),
                "commission_ids": (
                    commission_ids
                ),
                "total_net": str(
                    invoice.total_net
                ),
                "total_vat": str(
                    invoice.total_vat
                ),
                "total_gross": str(
                    invoice.total_gross
                ),
                "document_sha256": (
                    invoice.document_sha256
                ),
                "xml_sha256": (
                    document_result.xml_sha256
                ),
            },
        )

        db.session.flush()

        return document_result
    except Exception:
        if document_result is not None:
            remove_invoice_documents(
                document_result
            )

        raise


INVOICE_CANCELLATION_EXCLUDED_COLUMNS = {
    "responsible_user_id",
    "responsibility_revision",
    "cover_letter_title",
    "cover_letter_text",
    "cover_letter_date",
    "tenant_charge_letter",
    "id",
    "public_id",
    "invoice_number",
    "invoice_date",
    "due_date",
    "status",
    "invoice_kind",
    "original_invoice_id",
    "cancellation_reason",
    "canceled_at",
    "canceled_by_user_id",
    "payment_reference",
    "document_type",
    "document_filename",
    "document_sha256",
    "dvelop_document_id",
    "archived_at",
    "archive_error",
    "reference_invoice_id",
    "finalized_at",
    "finalized_by_user_id",
    "dms_archive_status",
    "dms_archive_key",
    "dms_document_id",
    "dms_archive_pdf_sha256",
    "dms_archive_attempted_at",
    "dms_archived_at",
    "dms_archived_by_user_id",
    "dms_archive_error",
    "created_by_user_id",
    "created_at",
    "updated_at",
}


POSITION_CANCELLATION_EXCLUDED_COLUMNS = {
    "id",
    "invoice_id",
    "created_at",
    "updated_at",
}


def _copy_mapped_values(
    model_class,
    source,
    excluded_columns: set[str],
) -> dict:
    return {
        attribute.key: getattr(
            source,
            attribute.key,
        )
        for attribute in (
            sqlalchemy_inspect(
                model_class
            ).column_attrs
        )
        if attribute.key
        not in excluded_columns
    }


def _negative_position(
    source_position: InvoicePosition,
) -> InvoicePosition:
    values = _copy_mapped_values(
        InvoicePosition,
        source_position,
        POSITION_CANCELLATION_EXCLUDED_COLUMNS,
    )
    position = InvoicePosition(
        **values
    )

    quantity = _finalization_decimal(
        source_position.quantity
    )
    position.quantity = -abs(
        quantity
    )

    if hasattr(
        position,
        "unit_price_net",
    ):
        position.unit_price_net = abs(
            _finalization_decimal(
                source_position.unit_price_net
            )
        )

    for field_name in (
        "total_net",
        "net_amount",
        "vat_amount",
        "total_vat",
        "total_gross",
        "gross_amount",
    ):
        if not hasattr(
            position,
            field_name,
        ):
            continue

        source_value = getattr(
            source_position,
            field_name,
        )

        if source_value is not None:
            setattr(
                position,
                field_name,
                -abs(
                    _finalization_decimal(
                        source_value
                    )
                ),
            )

    return position


def create_cancellation_invoice(
    original_invoice: Invoice,
    reason: str,
    user: User,
) -> tuple[
    Invoice,
    InvoiceDocumentResult,
]:
    normalized_reason = str(
        reason or ""
    ).strip()

    if not normalized_reason:
        raise InvoiceFinalizationError(
            "Bitte geben Sie einen Stornogrund an."
        )

    if (
        original_invoice.invoice_kind
        != InvoiceKind.INVOICE.value
    ):
        raise InvoiceFinalizationError(
            "Eine Stornorechnung kann nicht "
            "erneut storniert werden."
        )

    if (
        original_invoice.status
        != InvoiceStatus.FINALIZED.value
    ):
        raise InvoiceFinalizationError(
            "Nur festgeschriebene Rechnungen "
            "können storniert werden."
        )

    if (
        original_invoice.cancellation_invoice
        is not None
    ):
        raise InvoiceFinalizationError(
            "Für diese Rechnung existiert bereits "
            "eine Stornorechnung."
        )

    values = _copy_mapped_values(
        Invoice,
        original_invoice,
        INVOICE_CANCELLATION_EXCLUDED_COLUMNS,
    )
    cancellation_invoice = Invoice(
        **values
    )
    cancellation_invoice.status = (
        InvoiceStatus.DRAFT.value
    )
    cancellation_invoice.invoice_kind = (
        InvoiceKind.CANCELLATION.value
    )
    cancellation_invoice.document_type = (
        InvoiceDocumentType.CANCELLATION.value
    )
    cancellation_invoice.original_invoice = (
        original_invoice
    )
    cancellation_invoice.cancellation_reason = (
        normalized_reason
    )
    cancellation_invoice.invoice_date = (
        utc_now().date()
    )
    cancellation_invoice.due_date = None
    cancellation_invoice.payment_reference = (
        original_invoice.invoice_number
    )
    cancellation_invoice.introductory_text = ""
    cancellation_invoice.concluding_text = ""

    if hasattr(
        cancellation_invoice,
        "created_by_user_id",
    ):
        cancellation_invoice.created_by_user_id = (
            user.id
        )

    for source_position in (
        original_invoice.positions
    ):
        cancellation_invoice.positions.append(
            _negative_position(
                source_position
            )
        )

    db.session.add(
        cancellation_invoice
    )
    db.session.flush()

    document_result = None

    try:
        document_result = finalize_invoice(
            cancellation_invoice,
            user,
        )
        canceled_at = utc_now()
        original_invoice.status = (
            InvoiceStatus.CANCELED.value
        )
        original_invoice.cancellation_reason = (
            normalized_reason
        )
        original_invoice.canceled_at = (
            canceled_at
        )
        original_invoice.canceled_by_user_id = (
            user.id
        )
        linked_offer = (
            Offer.query
            .filter_by(
                invoice_id=original_invoice.id
            )
            .one_or_none()
        )

        if linked_offer is not None:
            linked_offer.status = (
                OfferStatus
                .REVIEW_REQUIRED.value
            )
            linked_offer.status_note = (
                "Die Rechnung "
                f"{original_invoice.invoice_number} "
                "wurde storniert: "
                f"{normalized_reason}"
            )
            linked_offer.invoiced_at = None

            audit(
                "offer.invoice.cancel",
                "offer",
                linked_offer.id,
                {
                    "invoice_id": (
                        original_invoice.id
                    ),
                    "cancellation_invoice_id": (
                        cancellation_invoice.id
                    ),
                    "reason": normalized_reason,
                },
            )

        audit(
            "invoice.cancel",
            "invoice",
            original_invoice.id,
            {
                "invoice_number": (
                    original_invoice.invoice_number
                ),
                "cancellation_invoice_id": (
                    cancellation_invoice.id
                ),
                "cancellation_invoice_number": (
                    cancellation_invoice.invoice_number
                ),
                "reason": normalized_reason,
            },
        )

        db.session.flush()

        return (
            cancellation_invoice,
            document_result,
        )
    except Exception:
        if document_result is not None:
            remove_invoice_documents(
                document_result
            )

        raise
