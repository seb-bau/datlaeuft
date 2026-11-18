from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

from dvelopdmspy.dvelopdmspy import DvelopDmsPy
from flask import current_app

from .audit import audit
from .invoice_document import (
    InvoiceDocumentError,
    resolve_invoice_document_path,
)
from .models import (
    Invoice,
    InvoiceArchiveStatus,
    InvoiceKind,
    InvoiceStatus,
    User,
    utc_now,
)
from .settings import SettingKeys, settings
from .dms_client import CloudDmsClient


class InvoiceArchiveError(RuntimeError):
    pass


@dataclass(frozen=True)
class InvoiceArchiveResult:
    document_id: str
    already_archived: bool


def _required_setting(
    definition,
) -> str:
    value = str(
        settings.get(definition) or ""
    ).strip()

    if not value:
        raise InvoiceArchiveError(
            f"Die Einstellung „{definition.label}“ fehlt."
        )

    return value


def _decimal_text(
    value,
) -> str:
    decimal_value = Decimal(
        str(value or 0)
    ).quantize(
        Decimal("0.01")
    )

    return format(
        decimal_value,
        ".2f",
    )


def _archive_values(
    invoice: Invoice,
    archive_key: str,
) -> dict[str, str]:
    company_code = invoice.company_code

    return {
        "archive_key": archive_key,
        "invoice_number": str(
            invoice.invoice_number or ""
        ),
        "invoice_date": (
            invoice.invoice_date.isoformat()
            if invoice.invoice_date
            else ""
        ),
        "due_date": (
            invoice.due_date.isoformat()
            if invoice.due_date
            else ""
        ),
        "company_code": str(
            company_code.code or ""
        ),
        "company_code_name": str(
            company_code.name or ""
        ),
        "contract_number": str(
            invoice.billing_contract_number
            or ""
        ),
        "recipient_name": str(
            invoice.recipient_name or ""
        ),
        "total_net": _decimal_text(
            invoice.total_net
        ),
        "total_vat": _decimal_text(
            invoice.total_vat
        ),
        "total_gross": _decimal_text(
            invoice.total_gross
        ),
        "invoice_kind": str(
            invoice.invoice_kind or ""
        ),
        "original_invoice_number": str(
            (
                invoice.original_invoice.invoice_number
                if invoice.original_invoice
                else ""
            )
            or ""
        ),
    }


def _render_property_value(
    template: str,
    values: dict[str, str],
) -> str:
    try:
        return str(
            template
        ).format_map(
            values
        )
    except KeyError as exc:
        raise InvoiceArchiveError(
            "Unbekannter Platzhalter in der "
            "d.velop-Eigenschaftszuordnung: "
            f"{exc.args[0]}"
        ) from exc


def _verify_invoice_pdf(
    invoice: Invoice,
) -> Path:
    if not invoice.document_filename:
        raise InvoiceArchiveError(
            "Für die Rechnung ist kein PDF gespeichert."
        )

    try:
        document_path = (
            resolve_invoice_document_path(
                invoice.document_filename
            )
        )
    except InvoiceDocumentError as exc:
        raise InvoiceArchiveError(
            str(exc)
        ) from exc

    if not document_path.is_file():
        raise InvoiceArchiveError(
            "Die gespeicherte Rechnungsdatei "
            "wurde nicht gefunden."
        )

    content_hash = sha256(
        document_path.read_bytes()
    ).hexdigest()

    if (
        invoice.document_sha256
        and content_hash
        != invoice.document_sha256
    ):
        raise InvoiceArchiveError(
            "Die Prüfsumme der Rechnungsdatei "
            "stimmt nicht mit der festgeschriebenen "
            "Rechnung überein."
        )

    return document_path


def _create_client() -> DvelopDmsPy:
    hostname = _required_setting(
        SettingKeys.DMS_HOSTNAME
    )
    api_key = _required_setting(
        SettingKeys.DMS_API_KEY
    )
    repository = _required_setting(
        SettingKeys.DMS_REPOSITORY
    )

    try:
        return CloudDmsClient(
            hostname=hostname,
            api_key=api_key,
            repository=repository,
        )
    except Exception as exc:
        raise InvoiceArchiveError(
            "Die Verbindung zu d.velop "
            "konnte nicht hergestellt werden."
        ) from exc


def _existing_document_id(
    client: DvelopDmsPy,
    category_id: str,
    property_id: str,
    archive_key: str,
) -> str | None:
    search_properties = (
        client.add_property(
            "",
            archive_key,
            prop_guid=property_id,
        )
    )

    try:
        documents = client.get_documents(
            properties=search_properties,
            categories=[category_id],
            limit=2,
        )
    except Exception as exc:
        raise InvoiceArchiveError(
            "Die Rechnung konnte in d.velop "
            "nicht gesucht werden."
        ) from exc

    if len(documents) > 1:
        raise InvoiceArchiveError(
            "In d.velop wurden mehrere Dokumente "
            "mit demselben Rechnungsschlüssel gefunden."
        )

    if not documents:
        return None

    document_id = str(
        documents[0].id_ or ""
    ).strip()

    if not document_id:
        raise InvoiceArchiveError(
            "d.velop hat für das vorhandene "
            "Dokument keine ID geliefert."
        )

    return document_id


def _upload_properties(
    client: DvelopDmsPy,
    invoice: Invoice,
    archive_key: str,
    id_property_id: str,
) -> list[dict]:
    raw_mapping = settings.get(
        SettingKeys.DMS_INVOICE_PROPERTY_MAP
    )

    if raw_mapping is None:
        raw_mapping = {}

    if not isinstance(raw_mapping, dict):
        raise InvoiceArchiveError(
            "Die d.velop-Eigenschaftszuordnung "
            "muss ein JSON-Objekt sein."
        )

    values = _archive_values(
        invoice,
        archive_key,
    )
    property_values = {
        id_property_id: archive_key,
    }

    for property_id, template in (
        raw_mapping.items()
    ):
        normalized_property_id = str(
            property_id or ""
        ).strip()

        if not normalized_property_id:
            raise InvoiceArchiveError(
                "Die d.velop-Eigenschaftszuordnung "
                "enthält eine leere Eigenschafts-ID."
            )

        rendered_value = (
            _render_property_value(
                str(template),
                values,
            )
        )

        if (
            normalized_property_id
            == id_property_id
            and rendered_value
            != archive_key
        ):
            raise InvoiceArchiveError(
                "Die Eigenschaft für den eindeutigen "
                "Rechnungsschlüssel darf nicht "
                "überschrieben werden."
            )

        property_values[
            normalized_property_id
        ] = rendered_value

    properties = []

    for property_id, value in (
        property_values.items()
    ):
        properties = (
            client.add_upload_property(
                "",
                value,
                prop_guid=property_id,
                plist=properties,
            )
        )

    return properties


def archive_invoice(
    invoice: Invoice,
    user: User,
) -> InvoiceArchiveResult:
    if invoice.status not in {
        InvoiceStatus.FINALIZED.value,
        InvoiceStatus.CANCELED.value,
    }:
        raise InvoiceArchiveError(
            "Nur festgeschriebene Rechnungen "
            "können archiviert werden."
        )

    document_path = _verify_invoice_pdf(
        invoice
    )

    if (
        invoice.dms_archive_status
        == InvoiceArchiveStatus.ARCHIVED.value
        and invoice.dms_document_id
    ):
        if (
            invoice.dms_archive_pdf_sha256
            and invoice.dms_archive_pdf_sha256
                != invoice.document_sha256
        ):
            raise InvoiceArchiveError(
                "Die archivierte Prüfsumme stimmt "
                "nicht mit dem aktuellen PDF überein."
            )

        return InvoiceArchiveResult(
            document_id=invoice.dms_document_id,
            already_archived=True,
        )

    category_id = _required_setting(
        SettingKeys.DMS_INVOICE_CATEGORY_ID
    )
    id_property_id = _required_setting(
        SettingKeys.DMS_INVOICE_ID_PROPERTY_ID
    )
    archive_key = (
        invoice.dms_archive_key
        or f"datlaeuft-invoice-{invoice.id}"
    )
    invoice.dms_archive_key = archive_key
    invoice.dms_archive_attempted_at = (
        utc_now()
    )

    client = _create_client()
    existing_document_id = (
        _existing_document_id(
            client,
            category_id,
            id_property_id,
            archive_key,
        )
    )
    already_archived = (
        existing_document_id is not None
    )

    if existing_document_id is None:
        properties = _upload_properties(
            client,
            invoice,
            archive_key,
            id_property_id,
        )

        try:
            upload_result = (
                client.archive_file(
                    str(document_path),
                    category_id=category_id,
                    properties=properties,
                )
            )
        except Exception as exc:
            raise InvoiceArchiveError(
                "Die Rechnung konnte nicht nach "
                "d.velop übertragen werden."
            ) from exc

        document_id = str(
            upload_result or ""
        ).strip()

        if (
            not document_id
            or document_id == "unknown"
        ):
            raise InvoiceArchiveError(
                "d.velop hat nach der Archivierung "
                "keine Dokument-ID geliefert."
            )
    else:
        document_id = existing_document_id

    archived_at = utc_now()
    invoice.dms_archive_status = (
        InvoiceArchiveStatus.ARCHIVED.value
    )
    invoice.dms_document_id = document_id
    invoice.dms_archive_pdf_sha256 = (
        invoice.document_sha256
    )
    invoice.dms_archived_at = archived_at
    invoice.dms_archived_by_user_id = (
        user.id
    )
    invoice.dms_archive_error = None

    audit(
        "invoice.archive",
        "invoice",
        invoice.id,
        {
            "invoice_number": (
                invoice.invoice_number
            ),
            "dms_document_id": document_id,
            "archive_key": archive_key,
            "pdf_sha256": (
                invoice.document_sha256
            ),
            "existing_document": (
                already_archived
            ),
        },
    )

    return InvoiceArchiveResult(
        document_id=document_id,
        already_archived=already_archived,
    )