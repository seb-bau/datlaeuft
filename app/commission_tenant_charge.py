from __future__ import annotations

import re
from datetime import date

from flask import current_app

from .models import Commission, WowiUseUnit
from .object_directory import directory_today, get_commission_object_choice
from .tenant_billing import TenantBillingAgreement, agreement_company_code, choose_contract_recipient, contract_recipients


def tenant_charge_object_choices(service, object_type, object_id, commission_type_id):
    if commission_type_id not in Commission.tenant_charge_type_ids():
        raise ValueError("Die ausgewählte Auftragsart ist nicht für eine Mieterbelastung eingerichtet.")
    if object_type not in {"license_agreement", "use_unit"} or not object_id:
        raise ValueError("Bitte eine Wohnung oder einen Mietvertrag auswählen.")
    choice = get_commission_object_choice(object_type, object_id)
    return tenant_charge_choices(
        service, (choice.get("use_unit") or {}).get("erp_id"),
        (choice.get("license_agreement") or {}).get("erp_id"),
    )


def tenant_charge_choices(service, use_unit_id, current_agreement_id=None):
    if not use_unit_id:
        raise ValueError("Bitte eine Wohnung oder einen Mietvertrag auswählen.")
    agreements = service.get_tenant_charge_agreements(use_unit_id)
    reference_date = (agreements.get(current_agreement_id) or {}).get("start") or directory_today()
    previous = {
        agreement_id: entry["end"] for agreement_id, entry in agreements.items()
        if agreement_id != current_agreement_id and entry["end"] and entry["end"] < reference_date
    }
    latest_end = max(previous.values(), default=None)
    latest_ids = [agreement_id for agreement_id, end in previous.items() if end == latest_end]
    items = []
    current_available = False
    for agreement_id, entry in sorted(
        agreements.items(), key=lambda pair: (pair[1]["start"] or date.min, pair[0]), reverse=True,
    ):
        recipients = {person["person_id"]: person["name"] for person in contract_recipients(service, agreement_id)}
        if agreement_id == current_agreement_id:
            current_available = len(recipients) == 1
        for person_id, name in recipients.items():
            start = entry["start"].strftime("%d.%m.%Y") if entry["start"] else "offen"
            end = entry["end"].strftime("%d.%m.%Y") if entry["end"] else "laufend"
            items.append({
                "value": f"{agreement_id}:{person_id}",
                "label": f"{name} · Vertrag {entry['number']} · {start} bis {end}",
                "suggested": latest_ids == [agreement_id] and len(recipients) == 1,
            })
    return {"current_available": current_available, "items": items}


def resolve_tenant_charge_target(service, use_unit_id, current_agreement_id, selection):
    if not use_unit_id:
        raise ValueError("Bitte für die Mieterbelastung eine Wohnung oder einen Mietvertrag auswählen.")
    if selection == "current" and current_agreement_id:
        agreement_id, person_id = current_agreement_id, None
    elif isinstance(selection, str) and re.fullmatch(r"[1-9][0-9]{0,17}:[1-9][0-9]{0,17}", selection):
        agreement_id, person_id = map(int, selection.split(":"))
    else:
        raise ValueError("Bitte wählen Sie aus, wer belastet werden soll.")
    agreement = service.get_tenant_charge_agreements(use_unit_id, agreement_id=agreement_id).get(agreement_id)
    if agreement is None:
        raise ValueError("Der Mietvertrag für die Belastung gehört nicht zur ausgewählten Wohnung oder ist nicht verfügbar.")
    recipients = {person["person_id"]: person for person in contract_recipients(service, agreement_id)}
    recipient = choose_contract_recipient(list(recipients.values()), person_id)
    return {
        "agreement_id": agreement_id,
        "agreement_number": agreement["number"],
        "person_id": recipient["person_id"],
        "name": recipient["name"],
    }, recipient


def tenant_charge_invoice_recipient(commission):
    from .wowiport import WowiportService

    target = commission.tenant_charge_target
    use_unit = WowiUseUnit.query.filter_by(erp_id=commission.use_unit_id).one_or_none()
    if use_unit is None or use_unit.economic_unit is None:
        raise ValueError("Die Wohnung für die Mieterbelastung ist nicht verfügbar.")
    try:
        verified, recipient = resolve_tenant_charge_target(
            WowiportService(), use_unit.erp_id, commission.license_agreement_id,
            f"{target['agreement_id']}:{target['person_id']}",
        )
    except ValueError:
        raise
    except Exception as exc:
        current_app.logger.exception("tenant_charge_invoice_recipient_failed commission_id=%s", commission.id)
        raise ValueError("Die Empfängeranschrift konnte nicht aus Wowiport geladen werden.") from exc
    if not all(str(recipient.get(field) or "").strip() for field in ("name", "street", "postal_code", "town")):
        raise ValueError("Für die zu belastende Person fehlt eine vollständige Postanschrift in Wowiport.")
    agreement = TenantBillingAgreement(verified["agreement_id"], verified["agreement_number"], use_unit)
    return agreement, agreement_company_code(agreement), recipient
