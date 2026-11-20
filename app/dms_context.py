from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


DOCUMENT_TYPES = {
    "invoice": "Rechnung",
    "cancellation_invoice": "Stornorechnung",
    "offer": "Angebot",
    "work_report": "Arbeitsnachweis",
    "invoice_with_work_reports": "Rechnung mit Arbeitsnachweisen",
    "safety_protocol_economic_unit": "Prüfprotokoll – Wirtschaftseinheit",
    "safety_protocol_building": "Prüfprotokoll – Gebäude",
    "safety_protocol_use_unit": "Prüfprotokoll – Nutzungseinheit",
}

FIELD_LABELS = {
    "archive_key": "Archivierungsschlüssel",
    "generation_id": "Erzeugungs-ID",
    "filename": "Dateiname",
    "document_type": "Dokumentart",
    "generated_at": "Erzeugt am",
    "company_code": "Buchungskreis",
    "company_code_name": "Name des Buchungskreises",
    "contract_number": "Vertragsnummer",
    "recipient_name": "Empfänger",
    "total_net": "Nettobetrag",
    "total_vat": "Umsatzsteuerbetrag",
    "total_gross": "Bruttobetrag",
    "invoice_number": "Rechnungsnummer",
    "invoice_date": "Rechnungsdatum",
    "due_date": "Fällig am",
    "invoice_kind": "Rechnungsart",
    "original_invoice_number": "Ursprüngliche Rechnungsnummer",
    "offer_number": "Angebotsnummer",
    "offer_date": "Angebotsdatum",
    "valid_until": "Gültig bis",
    "craft": "Gewerk",
    "economic_unit_id": "Wirtschaftseinheit-ID",
    "status": "Status",
    "commission_number": "Auftragsnummer",
    "commission_numbers": "Auftragsnummern",
    "commission_title": "Auftragstitel",
    "tenant_name": "Mieter",
    "object_address": "Objektadresse",
    "object_location": "Objektlage",
    "completed_at": "Abgeschlossen am",
    "completed_by": "Abgeschlossen durch",
    "completion_reason": "Abschlussvermerk",
    "entry_count": "Anzahl Zeitbuchungen",
    "total_minutes": "Arbeitszeit in Minuten",
    "craftsmen": "Ausführende Personen",
    "scope": "Umfang der Zeitnachweise",
    "protocol_id": "Protokoll-ID",
    "protocol_version": "Protokollversion",
    "job_id": "Prüfauftrag-ID",
    "job_public_id": "Öffentliche Prüfauftrag-ID",
    "inspection_name": "Prüfung",
    "inspection_group": "Prüfgruppe",
    "schedule_name": "Name des Zeitplans",
    "inspector_name": "Prüfer (Nachname Vorname)",
    "completed_date": "Abschlussdatum (YYYY-MM-DD)",
    "period_start": "Prüfzeitraum von",
    "period_end": "Prüfzeitraum bis",
    "due_from": "Prüffälligkeit von",
    "due_to": "Prüffälligkeit bis",
    "object_level": "Objektebene",
    "object_erp_id": "Objekt-ID",
    "company_code_erp_id": "Buchungskreis-ID",
    "company_code_code": "Buchungskreisnummer",
    "economic_unit_erp_id": "Wirtschaftseinheit-ID",
    "economic_unit_idnum": "Wowiport-Wirtschaftseinheit-IdNum",
    "economic_unit_name": "Wirtschaftseinheitname",
    "building_erp_id": "Gebäude-ID",
    "building_idnum": "Wowiport-Gebäude-IdNum",
    "use_unit_erp_id": "Nutzungseinheit-ID",
    "use_unit_idnum": "Wowiport-Nutzungseinheit-IdNum",
    "department": "Abteilung",
    "defect_count": "Anzahl Mängel",
    "correction_reason": "Korrekturgrund",
}

COMMON_FIELDS = {
    "archive_key", "generation_id", "filename", "document_type", "generated_at",
}

OBJECT_IDNUM_FIELDS = {
    "building_idnum", "economic_unit_idnum", "use_unit_idnum",
}

INVOICE_DOCUMENT_TYPES = {
    "invoice", "cancellation_invoice", "invoice_with_work_reports",
}

INVOICE_FIELDS = {
    "invoice_number", "invoice_date", "due_date", "company_code",
    "company_code_name", "contract_number", "recipient_name",
    "total_net", "total_vat", "total_gross", "invoice_kind",
    "original_invoice_number", "commission_numbers", "scope",
} | OBJECT_IDNUM_FIELDS

OFFER_FIELDS = {
    "offer_number", "offer_date", "valid_until", "company_code",
    "company_code_name", "contract_number", "recipient_name", "craft",
    "economic_unit_id", "commission_number", "status",
    "total_net", "total_vat", "total_gross",
}

WORK_REPORT_FIELDS = {
    "commission_number", "commission_numbers", "commission_title",
    "company_code", "contract_number", "tenant_name", "object_address",
    "object_location", "completed_at", "completed_by", "completion_reason",
    "entry_count", "total_minutes", "craftsmen", "scope",
} | OBJECT_IDNUM_FIELDS

PROTOCOL_FIELDS = (
    set(FIELD_LABELS)
    - COMMON_FIELDS
    - INVOICE_FIELDS
    - OFFER_FIELDS
    - WORK_REPORT_FIELDS
)
PROTOCOL_FIELDS |= {
    "status", "completed_at", "completed_by", "object_address", "object_location",
} | OBJECT_IDNUM_FIELDS


def available_fields(document_type):
    if document_type.startswith("safety_protocol_"):
        fields = PROTOCOL_FIELDS
    elif document_type == "offer":
        fields = OFFER_FIELDS
    elif document_type == "work_report":
        fields = WORK_REPORT_FIELDS
    elif document_type in INVOICE_DOCUMENT_TYPES:
        fields = INVOICE_FIELDS
    else:
        fields = set()
    return {
        key: FIELD_LABELS[key]
        for key in sorted(COMMON_FIELDS | fields, key=FIELD_LABELS.get)
    }


def template_fields(document_type):
    array_fields = {"commission_numbers", "craftsmen"}
    if document_type in INVOICE_DOCUMENT_TYPES:
        array_fields |= OBJECT_IDNUM_FIELDS
    return {key: label for key, label in available_fields(document_type).items() if key not in array_fields}


def invoice_context(invoice, scope="all"):
    from .invoice_archive import _archive_values

    return {
        **_archive_values(invoice, ""),
        **invoice_object_idnums(invoice),
        "scope": scope,
        "commission_numbers": [
            str(
                link.commission.erp_idnum
                or link.commission.code
                or link.commission_id
            )
            for link in invoice.commission_links
        ],
    }


def invoice_object_idnums(invoice):
    commissions = [link.commission for link in invoice.commission_links if link.commission is not None]
    if not commissions and invoice.original_invoice is not None:
        commissions = [link.commission for link in invoice.original_invoice.commission_links if link.commission is not None]
    if commissions:
        return {
            field: sorted({
                str(getattr(commission, field) or "").strip()
                for commission in commissions
                if str(getattr(commission, field) or "").strip()
            })
            for field in OBJECT_IDNUM_FIELDS
        }

    values = {field: [] for field in OBJECT_IDNUM_FIELDS}
    if invoice.billing_contract_erp_id is None:
        return values

    from .models import WowiLicenseAgreement

    agreement = WowiLicenseAgreement.query.filter_by(erp_id=invoice.billing_contract_erp_id).one_or_none()
    unit = agreement.use_unit if agreement is not None else None
    if unit is not None:
        for field, item in (
            ("use_unit_idnum", unit),
            ("building_idnum", unit.building),
            ("economic_unit_idnum", unit.economic_unit or (unit.building.economic_unit if unit.building else None)),
        ):
            value = str(item.id_num or "").strip() if item is not None else ""
            if value:
                values[field] = [value]
    return values


def offer_context(offer):
    from .offer_archive import offer_archive_values

    return offer_archive_values(offer, "")


def protocol_context(protocol):
    from .safety_inspection_archive import protocol_archive_values

    values = dict(protocol_archive_values(protocol, ""))
    values["completed_at"] = iso_timestamp(protocol.job.completed_at)
    return values


def iso_timestamp(value):
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def date_only(value):
    if value is None:
        return ""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        value = value.astimezone(ZoneInfo("Europe/Berlin")).date()
    return value.isoformat()


def inspector_name(user):
    if user is None:
        return ""
    parts = (getattr(user, "last_name", None), getattr(user, "first_name", None))
    return " ".join(str(part).strip() for part in parts if part and str(part).strip()) or user.display_name


def work_report_context(commission, entries, scope):
    actor = (
        commission.completion_submitted_by
        or commission.completion_submitted_by_external_craftsman
    )
    number = str(commission.erp_idnum or commission.code or commission.id)
    return {
        "commission_number": number,
        "commission_numbers": [number],
        "commission_title": commission.display_title,
        "company_code": commission.company_code_code or "",
        **{field: getattr(commission, field) or "" for field in OBJECT_IDNUM_FIELDS},
        "contract_number": commission.license_agreement_idnum or "",
        "tenant_name": commission.tenant_name or "",
        "object_address": commission.apartment_address or "",
        "object_location": commission.apartment_location or "",
        "completed_at": iso_timestamp(commission.completed_at),
        "completed_by": getattr(
            actor, "display_name", getattr(actor, "name", ""),
        ),
        "completion_reason": commission.completion_reason or "",
        "entry_count": len(entries),
        "total_minutes": sum(entry.minutes for entry in entries),
        "craftsmen": sorted({
            entry.user.display_name
            for entry in entries
            if entry.user is not None
        }),
        "scope": scope,
    }
