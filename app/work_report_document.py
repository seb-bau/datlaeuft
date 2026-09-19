from __future__ import annotations

from datetime import datetime
from io import BytesIO
import re

from flask import current_app, render_template
from pypdf import PdfReader, PdfWriter
from weasyprint import HTML

from .invoice_document import (
    InvoiceDocumentError,
    resolve_invoice_document_path,
)
from .models import (
    Commission,
    Invoice,
    InvoiceStatus,
    TimeEntry,
)

from .performance_review_service import (
    active_installed_material_movements,
)


WORK_REPORT_SCOPE_BILLED = "billed"
WORK_REPORT_SCOPE_ALL = "all"

TIME_COMPONENTS = (
    ("work_minutes", "Arbeitszeit vor Ort"),
    ("waiting_minutes", "Wartezeit"),
    ("material_minutes", "Materialbeschaffung"),
    ("travel_minutes", "Fahrt / Wegezeit"),
    (
        "documentation_minutes",
        "Dokumentation / Rücksprache",
    ),
    ("extra_minutes", "Zusatzarbeit"),
)


class WorkReportDocumentError(RuntimeError):
    pass


def normalize_work_report_scope(
    value: str | None,
) -> str:
    scope = str(
        value or WORK_REPORT_SCOPE_BILLED
    ).strip().lower()

    if scope not in {
        WORK_REPORT_SCOPE_BILLED,
        WORK_REPORT_SCOPE_ALL,
    }:
        raise WorkReportDocumentError(
            "Die Auswahl der Zeitnachweise "
            "ist ungültig."
        )

    return scope


def commission_work_report_entries(
    commission: Commission,
) -> list[TimeEntry]:
    return sorted(
        commission.time_entries,
        key=lambda entry: (
            entry.service_date
            or entry.created_at.date(),
            entry.created_at,
            entry.id,
        ),
    )


def invoice_time_entry_ids(
    invoice: Invoice,
) -> set[int]:
    entry_ids: set[int] = set()

    for position in invoice.positions:
        source_data = (
            position.source_data
            if isinstance(
                position.source_data,
                dict,
            )
            else {}
        )
        source_entry_ids = source_data.get(
            "time_entry_ids"
        )

        if not isinstance(
            source_entry_ids,
            list,
        ):
            continue

        for value in source_entry_ids:
            try:
                entry_ids.add(int(value))
            except (TypeError, ValueError):
                continue

    return entry_ids


def invoice_work_report_groups(
    invoice: Invoice,
    scope: str,
) -> list[
    tuple[Commission, list[TimeEntry]]
]:
    normalized_scope = (
        normalize_work_report_scope(scope)
    )
    billed_entry_ids = (
        invoice_time_entry_ids(invoice)
        if (
            normalized_scope
            == WORK_REPORT_SCOPE_BILLED
        )
        else None
    )
    groups = []

    for link in sorted(
        invoice.commission_links,
        key=lambda item: (
            item.id,
            item.commission_id,
        ),
    ):
        entries = (
            commission_work_report_entries(
                link.commission
            )
        )

        if billed_entry_ids is not None:
            entries = [
                entry
                for entry in entries
                if entry.id in billed_entry_ids
            ]

        if entries:
            groups.append(
                (
                    link.commission,
                    entries,
                )
            )

    return groups


def work_report_counts(
    invoice: Invoice,
) -> dict[int, dict[str, int]]:
    result: dict[
        int,
        dict[str, int],
    ] = {}

    for scope in (
        WORK_REPORT_SCOPE_BILLED,
        WORK_REPORT_SCOPE_ALL,
    ):
        for commission, entries in (
            invoice_work_report_groups(
                invoice,
                scope,
            )
        ):
            item = result.setdefault(
                commission.id,
                {
                    WORK_REPORT_SCOPE_BILLED: 0,
                    WORK_REPORT_SCOPE_ALL: 0,
                },
            )
            item[scope] = len(entries)

    return result


def minutes_text(
    value: int,
) -> str:
    total_minutes = max(
        int(value or 0),
        0,
    )
    hours, minutes = divmod(
        total_minutes,
        60,
    )
    values = []

    if hours:
        values.append(
            f"{hours} Std."
        )

    if minutes or not values:
        values.append(
            f"{minutes} Min."
        )

    return " ".join(values)


def work_report_entry_context(
    entry: TimeEntry,
) -> dict:
    component_rows = []

    for attribute_name, label in TIME_COMPONENTS:
        minutes = int(
            getattr(
                entry,
                attribute_name,
                0,
            )
            or 0
        )

        if minutes <= 0:
            continue

        component_rows.append(
            {
                "label": label,
                "minutes": minutes,
                "duration": minutes_text(
                    minutes
                ),
            }
        )

    return {
        "id": entry.id,
        "service_date": (
            entry.service_date
            or entry.created_at.date()
        ).strftime(
            "%d.%m.%Y"
        ),
        "user_name": entry.user.display_name,
        "task_code": (
            entry.task_template.code
            if entry.task_template
            else None
        ),
        "task_title": (
            entry.task_template.title
            if entry.task_template
            else "Zeitbuchung"
        ),
        "minutes": entry.minutes,
        "duration": minutes_text(
            entry.minutes
        ),
        "component_rows": component_rows,
        "deviation_reason": (
            entry.deviation_reason
        ),
        "note": entry.note,
    }


def material_quantity_text(
    value: float,
) -> str:
    return (
        f"{float(value):g}"
        .replace(".", ",")
    )


def work_report_material_context(
    commission: Commission,
) -> list[dict]:
    grouped = {}

    for movement in (
        active_installed_material_movements(
            commission
        )
    ):
        key = (
            movement.task_template_id,
            movement.item_id,
        )
        row = grouped.setdefault(
            key,
            {
                "task_title": (
                    movement.task_template.title
                    if movement.task_template
                    else "Ohne Leistungsposition"
                ),
                "item_number": (
                    movement.item.item_number
                ),
                "item_name": movement.item.name,
                "unit": movement.item.unit,
                "quantity": 0.0,
            },
        )
        row["quantity"] += float(
            movement.quantity
        )

    rows = list(grouped.values())

    for row in rows:
        row["quantity_text"] = (
            material_quantity_text(
                row["quantity"]
            )
        )

    return sorted(
        rows,
        key=lambda row: (
            row["task_title"],
            row["item_name"],
        ),
    )


def work_report_context(
    commission: Commission,
    entries: list[TimeEntry],
    scope: str,
) -> dict:
    if not entries:
        raise WorkReportDocumentError(
            "Für diesen Auftrag sind keine "
            "Zeitnachweise vorhanden."
        )

    normalized_scope = (
        normalize_work_report_scope(scope)
    )
    component_totals = []

    for attribute_name, label in TIME_COMPONENTS:
        minutes = sum(
            int(
                getattr(
                    entry,
                    attribute_name,
                    0,
                )
                or 0
            )
            for entry in entries
        )

        if minutes <= 0:
            continue

        component_totals.append(
            {
                "label": label,
                "duration": minutes_text(
                    minutes
                ),
            }
        )

    service_dates = [
        (
                entry.service_date
                or entry.created_at.date()
        )
        for entry in entries
    ]

    material_rows = (
        work_report_material_context(
            commission
        )
    )

    return {
        "commission": commission,
        "commission_number": (
            commission.erp_idnum
            or commission.code
            or str(commission.id)
        ),
        "commission_title": (
            commission.display_title
        ),
        "address": (
            commission.apartment_address
            or "–"
        ),
        "location": (
            commission.apartment_location
        ),
        "tenant_name": (
            commission.tenant_name
        ),
        "contract_number": (
            commission.license_agreement_idnum
        ),
        "company_code": (
            commission.company_code_code
        ),
        "entries": [
            work_report_entry_context(
                entry
            )
            for entry in entries
        ],
        "entry_count": len(entries),
        "total_duration": minutes_text(
            sum(
                entry.minutes
                for entry in entries
            )
        ),
        "component_totals": component_totals,
        "service_date_from": min(
            service_dates
        ).strftime("%d.%m.%Y"),
        "service_date_to": max(
            service_dates
        ).strftime("%d.%m.%Y"),
        "scope_label": (
            "Berechnete Zeitnachweise"
            if (
                normalized_scope
                == WORK_REPORT_SCOPE_BILLED
            )
            else "Alle Zeitnachweise"
        ),
        "material_rows": material_rows,
        "material_count": len(material_rows),
        "generated_at": datetime.now().strftime(
            "%d.%m.%Y %H:%M"
        ),
    }


def create_work_report_pdf(
    commission: Commission,
    entries: list[TimeEntry],
    scope: str = WORK_REPORT_SCOPE_ALL,
) -> BytesIO:
    context = work_report_context(
        commission,
        entries,
        scope,
    )
    html_text = render_template(
        "admin/work_report_pdf.html",
        **context,
    )

    try:
        pdf_bytes = HTML(
            string=html_text,
            base_url=current_app.root_path,
        ).write_pdf()
    except Exception as exc:
        raise WorkReportDocumentError(
            "Der Arbeitsnachweis konnte "
            "nicht erzeugt werden."
        ) from exc

    result = BytesIO(pdf_bytes)
    result.seek(0)

    return result


def create_invoice_with_work_reports_pdf(
    invoice: Invoice,
    scope: str,
) -> BytesIO:
    if (
        invoice.status not in {
            InvoiceStatus.FINALIZED.value,
            InvoiceStatus.CANCELED.value,
        }
        or not invoice.document_filename
    ):
        raise WorkReportDocumentError(
            "Die Rechnung ist noch nicht "
            "festgeschrieben."
        )

    groups = invoice_work_report_groups(
        invoice,
        scope,
    )

    if not groups:
        raise WorkReportDocumentError(
            "Für diese Auswahl sind keine "
            "Zeitnachweise vorhanden."
        )

    try:
        invoice_path = (
            resolve_invoice_document_path(
                invoice.document_filename
            )
        )
    except InvoiceDocumentError as exc:
        raise WorkReportDocumentError(
            "Die Rechnungsdatei wurde "
            "nicht gefunden."
        ) from exc

    if not invoice_path.is_file():
        raise WorkReportDocumentError(
            "Die Rechnungsdatei wurde "
            "nicht gefunden."
        )

    try:
        invoice_reader = PdfReader(
            str(invoice_path)
        )
        writer = PdfWriter()
        writer.clone_document_from_reader(
            invoice_reader
        )
        report_buffers = []

        for commission, entries in groups:
            report_buffer = (
                create_work_report_pdf(
                    commission,
                    entries,
                    scope,
                )
            )
            report_buffers.append(
                report_buffer
            )
            writer.append(
                PdfReader(report_buffer)
            )

        result = BytesIO()
        writer.write(result)
        result.seek(0)

        return result
    except WorkReportDocumentError:
        raise
    except Exception as exc:
        raise WorkReportDocumentError(
            "Rechnung und Arbeitsnachweise "
            "konnten nicht zusammengeführt "
            "werden."
        ) from exc


def safe_document_name(
    value: str,
) -> str:
    normalized = re.sub(
        r"[^A-Za-z0-9._-]+",
        "-",
        str(value or "").strip(),
    ).strip("-")

    return normalized or "Dokument"


def work_report_filename(
    commission: Commission,
) -> str:
    number = (
        commission.erp_idnum
        or commission.code
        or str(commission.id)
    )

    return (
        "Arbeitsnachweis-"
        f"{safe_document_name(number)}.pdf"
    )


def invoice_work_reports_filename(
    invoice: Invoice,
) -> str:
    return (
        "Rechnung-"
        f"{safe_document_name(invoice.invoice_number or str(invoice.id))}"
        "-mit-Arbeitsnachweisen.pdf"
    )