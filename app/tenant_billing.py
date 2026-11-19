from __future__ import annotations

from .models import (
    Invoice, InvoiceRecipientType, WowiCompanyCode, WowiEconomicUnit, WowiLicenseAgreement, WowiUseUnit,
)


def tenant_billing_agreement(agreement_id: int) -> WowiLicenseAgreement:
    agreement = WowiLicenseAgreement.query.filter_by(
        erp_id=agreement_id, active=True, is_vacancy=False,
    ).one_or_none()
    if agreement is None or agreement.use_unit is None or agreement.use_unit.economic_unit is None:
        raise ValueError("Der ausgewählte Mietvertrag ist nicht verfügbar.")
    return agreement


def agreement_company_code(agreement: WowiLicenseAgreement) -> WowiCompanyCode:
    company = WowiCompanyCode.query.filter_by(
        erp_id=agreement.use_unit.economic_unit.company_code_erp_id, active=True,
    ).one_or_none()
    if company is None:
        raise ValueError("Das Buchwerk des Mietvertrags ist nicht verfügbar.")
    return company


def agreement_uses_tenant_billing(agreement_id: int) -> bool:
    return WowiCompanyCode.query.join(
        WowiEconomicUnit, WowiEconomicUnit.company_code_erp_id == WowiCompanyCode.erp_id,
    ).join(
        WowiUseUnit, WowiUseUnit.economic_unit_id == WowiEconomicUnit.id,
    ).join(
        WowiLicenseAgreement, WowiLicenseAgreement.use_unit_id == WowiUseUnit.id,
    ).filter(
        WowiLicenseAgreement.erp_id == agreement_id,
        WowiCompanyCode.tenant_billing_issuer_id.is_not(None),
    ).first() is not None


def tenant_billing_issuer(company: WowiCompanyCode, *, existing_issuer_id: int | None = None) -> WowiCompanyCode:
    issuer = company.tenant_billing_issuer if company.tenant_billing_issuer_id is not None else company
    if issuer is None or not issuer.active or (
        issuer.document_creation_disabled and (existing_issuer_id is None or issuer.id != existing_issuer_id)
    ):
        raise ValueError("Für das ausstellende Buchwerk können keine neuen Rechnungen erstellt werden.")
    return issuer


def contract_recipients(service, agreement_id: int) -> list[dict]:
    return [
        candidate for candidate in service.get_invoice_recipient_candidates(agreement_id)
        if candidate.get("person_id") and str(candidate.get("name") or "").strip()
    ]


def choose_contract_recipient(candidates: list[dict], person_id: int | None) -> dict:
    if person_id is not None:
        for candidate in candidates:
            if candidate["person_id"] == person_id:
                return candidate
        raise ValueError("Die ausgewählte Person gehört nicht zum Mietvertrag.")
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise ValueError("Für den Mietvertrag wurde keine Empfängeranschrift gefunden.")
    raise ValueError("Bitte wählen Sie einen Vertragspartner aus.")


def apply_contract_recipient(invoice: Invoice, agreement: WowiLicenseAgreement, candidate: dict) -> None:
    invoice.recipient_license_agreement_erp_id = agreement.erp_id
    invoice.recipient_license_agreement_idnum = agreement.id_num
    invoice.recipient_person_erp_id = candidate.get("person_id")
    invoice.recipient_type = candidate.get("recipient_type") or InvoiceRecipientType.PRIVATE.value
    invoice.recipient_name = candidate.get("name")
    invoice.recipient_name_addition = candidate.get("name_addition")
    invoice.recipient_street = candidate.get("street")
    invoice.recipient_postal_code = candidate.get("postal_code")
    invoice.recipient_town = candidate.get("town")
    invoice.recipient_country_code = candidate.get("country_code") or "DE"
    invoice.recipient_email = candidate.get("email")
    invoice.recipient_vat_id = candidate.get("vat_id")


def apply_tenant_billing(
    invoice: Invoice, agreement: WowiLicenseAgreement, company: WowiCompanyCode, candidate: dict,
) -> None:
    tenant_name = candidate.get("name")
    if not tenant_name:
        tenant_name = ", ".join(dict.fromkeys(
            contractor.display_name for contractor in agreement.contractors
            if contractor.active and contractor.display_name
        ))
    unit = agreement.use_unit
    building = unit.building
    property_address = unit.address or (building.address if building else None)
    if not property_address and building:
        locality = " ".join(part for part in (building.zip_code, building.town) if part)
        property_address = ", ".join(part for part in (building.street, locality) if part)

    if not all(str(value or "").strip() for value in (tenant_name, property_address, agreement.id_num)):
        raise ValueError("Im Mietvertrag fehlen Name, Objektanschrift oder Vertragsnummer.")
    if not all(str(value or "").strip() for value in (company.seller_street, company.seller_postal_code, company.seller_town)):
        raise ValueError(f"Bitte hinterlegen Sie die Rechnungsanschrift für das Buchwerk {company.code}.")

    invoice.tenant_billing_reference = {
        "company_code_id": company.id,
        "agreement_erp_id": agreement.erp_id,
        "agreement_number": agreement.id_num,
        "tenant_name": tenant_name,
        "property_address": property_address,
        "person_erp_id": candidate.get("person_id"),
        "tenant_address": {
            field: candidate.get(field)
            for field in ("name", "name_addition", "street", "postal_code", "town", "country_code")
        },
    }
    invoice.recipient_license_agreement_erp_id = None
    invoice.recipient_license_agreement_idnum = None
    invoice.recipient_person_erp_id = None
    invoice.recipient_type = InvoiceRecipientType.BUSINESS.value
    invoice.recipient_name = company.seller_name or company.name
    invoice.recipient_name_addition = None
    invoice.recipient_street = company.seller_street
    invoice.recipient_postal_code = company.seller_postal_code
    invoice.recipient_town = company.seller_town
    invoice.recipient_country_code = company.seller_country_code
    invoice.recipient_email = company.tenant_billing_recipient_email
    invoice.recipient_vat_id = company.seller_vat_id
    invoice.payment_information = None
