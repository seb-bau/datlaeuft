from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import re

from facturx import generate_cii_xml, generate_from_file
from flask import current_app
from pypdf import PdfReader, PdfWriter
from weasyprint import HTML, default_url_fetcher

from .models import (
    Invoice,
    InvoiceKind,
)
from .invoice_payments import invoice_payment_context
from .invoice_attachments import append_invoice_attachments
from .invoice_layout import DEFAULT_INVOICE_LAYOUT, render_invoice_html
from .settings import SettingKeys, settings
from .stationery_layout import company_page_layout, page_stylesheets

MONEY_QUANTIZER = Decimal("0.01")
QUANTITY_QUANTIZER = Decimal("0.001")


class InvoiceDocumentError(RuntimeError):
    pass


@dataclass(frozen=True)
class InvoiceDocumentResult:
    pdf_storage_name: str
    xml_storage_name: str
    pdf_sha256: str
    xml_sha256: str


def _read_value(instance, *names, default=None):
    if instance is None:
        return default

    for name in names:
        if hasattr(instance, name):
            value = getattr(instance, name)

            if value is not None:
                return value

    return default


def _decimal(value, default: str = "0") -> Decimal:
    if value in (None, ""):
        return Decimal(default)

    if isinstance(value, Decimal):
        return value

    return Decimal(str(value).replace(",", "."))


def _money(value) -> Decimal:
    return _decimal(value).quantize(
        MONEY_QUANTIZER,
        rounding=ROUND_HALF_UP,
    )


def _required_text(value, label: str) -> str:
    normalized = str(value or "").strip()

    if not normalized:
        raise InvoiceDocumentError(
            f"{label} fehlt."
        )

    return normalized


def _date_value(value, label: str) -> date:
    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    raise InvoiceDocumentError(
        f"{label} fehlt oder ist ungültig."
    )


def _format_date(value: date | None) -> str:
    if value is None:
        return ""

    return value.strftime("%d.%m.%Y")


def _format_money(value: Decimal) -> str:
    normalized = _money(value)
    formatted = f"{normalized:,.2f}"

    return (
        formatted
        .replace(",", "_")
        .replace(".", ",")
        .replace("_", ".")
    )


def _format_quantity(value: Decimal) -> str:
    normalized = value.quantize(
        QUANTITY_QUANTIZER,
        rounding=ROUND_HALF_UP,
    )
    formatted = format(normalized, "f")

    if "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")

    return formatted.replace(".", ",")


def _xml_amount(value: Decimal) -> str:
    return format(_money(value), ".2f")


def _xml_quantity(value: Decimal) -> str:
    normalized = value.quantize(
        QUANTITY_QUANTIZER,
        rounding=ROUND_HALF_UP,
    )

    return format(normalized, "f")


def _safe_filename(value: str) -> str:
    normalized = re.sub(
        r"[^A-Za-z0-9._-]+",
        "-",
        value.strip(),
    ).strip("-")

    if not normalized:
        raise InvoiceDocumentError(
            "Die Rechnungsnummer kann nicht als Dateiname verwendet werden."
        )

    return normalized


def _street_line(street, house_number) -> str:
    values = [
        str(value).strip()
        for value in (street, house_number)
        if str(value or "").strip()
    ]

    return " ".join(values)


def _unit_code(unit: str) -> str:
    normalized = str(unit or "").strip().lower()

    if normalized in {
        "h",
        "std",
        "std.",
        "stunde",
        "stunden",
    }:
        return "HUR"

    if normalized in {
        "min",
        "minute",
        "minuten",
    }:
        return "MIN"

    if normalized in {
        "m",
        "meter",
    }:
        return "MTR"

    if normalized in {
        "m²",
        "m2",
        "quadratmeter",
    }:
        return "MTK"

    if normalized in {
        "kg",
        "kilogramm",
    }:
        return "KGM"

    if normalized in {
        "l",
        "liter",
    }:
        return "LTR"

    return "C62"


def _vat_category(vat_rate: Decimal) -> str:
    if vat_rate == Decimal("0"):
        return "Z"

    return "S"


def _compact(value):
    if isinstance(value, dict):
        result = {}

        for key, item in value.items():
            compacted = _compact(item)

            if compacted not in (
                None,
                "",
                [],
                {},
            ):
                result[key] = compacted

        return result

    if isinstance(value, list):
        return [
            compacted
            for item in value
            if (
                compacted := _compact(item)
            ) not in (
                None,
                "",
                [],
                {},
            )
        ]

    return value


def _stationery_path(invoice: Invoice) -> Path:
    company_code = invoice.company_code
    filename = _read_value(
        company_code,
        "invoice_stationery_filename",
        "stationery_filename",
    )
    filename = _required_text(
        filename,
        "Der Briefbogen des Buchwerks",
    )

    candidates = [
        (
            Path(current_app.instance_path)
            / "invoice_stationery"
            / filename
        ),
        (
            Path(current_app.instance_path)
            / "uploads"
            / "invoice_stationery"
            / filename
        ),
        (
            Path(current_app.root_path)
            / "uploads"
            / "stationery"
            / filename
        ),
    ]

    for candidate in candidates:
        if candidate.is_file():
            return candidate

    raise InvoiceDocumentError(
        f"Der Briefbogen {filename} wurde nicht gefunden."
    )


def _prepare_positions(
    invoice: Invoice,
) -> list[dict]:
    source_positions = sorted(
        list(invoice.positions),
        key=lambda item: (
            _read_value(
                item,
                "position_index",
                "sort_order",
                "position_number",
                default=0,
            ),
            item.id or 0,
        ),
    )

    if not source_positions:
        raise InvoiceDocumentError(
            "Die Rechnung enthält keine Positionen."
        )

    positions = []

    for number, position in enumerate(
        source_positions,
        start=1,
    ):
        description = _required_text(
            _read_value(
                position,
                "description",
                "name",
            ),
            f"Bezeichnung der Position {number}",
        )
        details = str(
            _read_value(
                position,
                "details",
                default="",
            )
            or ""
        ).strip()
        quantity = _decimal(
            _read_value(
                position,
                "quantity",
                default=1,
            ),
            default="1",
        )
        unit = str(
            _read_value(
                position,
                "unit",
                "unit_name",
                default="Stück",
            )
        ).strip()
        unit_price_net = _money(
            _read_value(
                position,
                "unit_price_net",
                "net_unit_price",
                default=0,
            )
        )
        vat_rate = _decimal(
            _read_value(
                position,
                "vat_rate",
                default=19,
            )
        )

        stored_net = _read_value(
            position,
            "total_net",
            "net_amount",
        )
        total_net = _money(
            (
                quantity * unit_price_net
                if stored_net is None
                else stored_net
            )
        )

        stored_vat = _read_value(
            position,
            "vat_amount",
            "total_vat",
        )
        vat_amount = _money(
            (
                total_net
                * vat_rate
                / Decimal("100")
                if stored_vat is None
                else stored_vat
            )
        )
        total_gross = total_net + vat_amount

        positions.append(
            {
                "number": number,
                "article_number": str(getattr(position, "article_number", "") or ""),
                "description": description,
                "details": details,
                "quantity": quantity,
                "quantity_formatted": (
                    _format_quantity(quantity)
                ),
                "unit": unit,
                "unit_code": _unit_code(unit),
                "unit_price_net": unit_price_net,
                "unit_price_formatted": (
                    _format_money(unit_price_net)
                ),
                "vat_rate": vat_rate,
                "vat_rate_formatted": (
                    _format_quantity(vat_rate)
                ),
                "vat_category": (
                    _vat_category(vat_rate)
                ),
                "total_net": total_net,
                "total_net_formatted": (
                    _format_money(total_net)
                ),
                "vat_amount": vat_amount,
                "total_gross": total_gross,
                "item_code": str(
                    _read_value(
                        position,
                        "article_number",
                        "catalogue_code",
                        "service_code",
                        default="",
                    )
                    or ""
                ).strip(),
            }
        )

    return positions


def _prepare_vat_rows(
    positions: list[dict],
) -> list[dict]:
    groups = defaultdict(
        lambda: {
            "net": Decimal("0"),
            "vat": Decimal("0"),
        }
    )

    for position in positions:
        rate = position["vat_rate"]
        groups[rate]["net"] += position["total_net"]
        groups[rate]["vat"] += position["vat_amount"]

    return [
        {
            "rate": rate,
            "rate_formatted": _format_quantity(rate),
            "net": _money(values["net"]),
            "net_formatted": _format_money(
                values["net"]
            ),
            "vat": _money(values["vat"]),
            "vat_formatted": _format_money(
                values["vat"]
            ),
            "category": _vat_category(rate),
        }
        for rate, values in sorted(
            groups.items(),
            key=lambda item: item[0],
        )
    ]


def _company_value(
    invoice: Invoice,
    invoice_names: tuple[str, ...],
    company_names: tuple[str, ...],
    default=None,
):
    value = _read_value(
        invoice,
        *invoice_names,
    )

    if value is not None:
        return value

    return _read_value(
        invoice.company_code,
        *company_names,
        default=default,
    )


def _document_context(
    invoice: Invoice,
    positions: list[dict],
    vat_rows: list[dict],
) -> dict:
    invoice_number = _required_text(
        _read_value(
            invoice,
            "invoice_number",
            "number",
        ),
        "Rechnungsnummer",
    )
    invoice_date = _date_value(
        _read_value(
            invoice,
            "invoice_date",
            "issued_at",
        ),
        "Rechnungsdatum",
    )
    due_date_value = _read_value(
        invoice,
        "due_date",
    )
    due_date = (
        _date_value(
            due_date_value,
            "Fälligkeitsdatum",
        )
        if due_date_value is not None
        else None
    )
    service_date_from_value = _read_value(
        invoice,
        "service_date_from",
    )
    service_date_to_value = _read_value(
        invoice,
        "service_date_to",
    )
    service_date_from = (
        _date_value(
            service_date_from_value,
            "Leistungsbeginn",
        )
        if service_date_from_value is not None
        else None
    )
    service_date_to = (
        _date_value(
            service_date_to_value,
            "Leistungsende",
        )
        if service_date_to_value is not None
        else None
    )

    seller_name = _required_text(
        _company_value(
            invoice,
            ("seller_name",),
            (
                "invoice_seller_name",
                "seller_name",
                "name",
            ),
        ),
        "Name des Rechnungsausstellers",
    )
    seller_street = _street_line(
        _company_value(
            invoice,
            ("seller_street",),
            (
                "invoice_seller_street",
                "seller_street",
            ),
        ),
        _company_value(
            invoice,
            ("seller_house_number",),
            (
                "invoice_seller_house_number",
                "seller_house_number",
            ),
        ),
    )
    seller_postal_code = _required_text(
        _company_value(
            invoice,
            ("seller_postal_code",),
            (
                "invoice_seller_postal_code",
                "seller_postal_code",
            ),
        ),
        "Postleitzahl des Rechnungsausstellers",
    )
    seller_city = _required_text(
        _company_value(
            invoice,
            (
                "seller_town",
                "seller_city",
            ),
            (
                "seller_town",
                "invoice_seller_city",
                "seller_city",
            ),
        ),
        "Ort des Rechnungsausstellers",
    )
    seller_country_code = str(
        _company_value(
            invoice,
            ("seller_country_code",),
            (
                "invoice_seller_country_code",
                "seller_country_code",
            ),
            default="DE",
        )
        or "DE"
    ).upper()
    seller_tax_number = str(
        _company_value(
            invoice,
            ("seller_tax_number",),
            (
                "invoice_tax_number",
                "seller_tax_number",
            ),
            default="",
        )
        or ""
    ).strip()
    seller_vat_id = str(
        _company_value(
            invoice,
            ("seller_vat_id",),
            (
                "invoice_vat_id",
                "seller_vat_id",
            ),
            default="",
        )
        or ""
    ).replace(" ", "")
    seller_email = str(
        _company_value(
            invoice,
            ("seller_email",),
            (
                "invoice_email",
                "seller_email",
            ),
            default="",
        )
        or ""
    ).strip()
    seller_phone = str(
        _company_value(
            invoice,
            ("seller_phone",),
            (
                "invoice_phone",
                "seller_phone",
            ),
            default="",
        )
        or ""
    ).strip()
    seller_iban = str(
        _company_value(
            invoice,
            ("seller_iban",),
            (
                "invoice_iban",
                "seller_iban",
            ),
            default="",
        )
        or ""
    ).replace(" ", "")
    seller_bic = str(
        _company_value(
            invoice,
            ("seller_bic",),
            (
                "invoice_bic",
                "seller_bic",
            ),
            default="",
        )
        or ""
    ).replace(" ", "")
    seller_bank_name = str(
        _company_value(
            invoice,
            ("seller_bank_name",),
            (
                "invoice_bank_name",
                "seller_bank_name",
            ),
            default="",
        )
        or ""
    ).strip()

    if not seller_street:
        raise InvoiceDocumentError(
            "Straße des Rechnungsausstellers fehlt."
        )

    if not seller_tax_number and not seller_vat_id:
        raise InvoiceDocumentError(
            "Steuernummer oder Umsatzsteuer-ID des Rechnungsausstellers fehlt."
        )

    recipient_name = _required_text(
        _read_value(
            invoice,
            "recipient_name",
        ),
        "Name des Rechnungsempfängers",
    )
    recipient_addition = str(
        _read_value(
            invoice,
            "recipient_name_addition",
            "recipient_addition",
            default="",
        )
        or ""
    ).strip()
    recipient_street = _street_line(
        _read_value(
            invoice,
            "recipient_street",
        ),
        _read_value(
            invoice,
            "recipient_house_number",
        ),
    )
    recipient_postal_code = _required_text(
        _read_value(
            invoice,
            "recipient_postal_code",
        ),
        "Postleitzahl des Rechnungsempfängers",
    )
    recipient_city = _required_text(
        _read_value(
            invoice,
            "recipient_town",
            "recipient_city",
        ),
        "Ort des Rechnungsempfängers",
    )
    recipient_country_code = str(
        _read_value(
            invoice,
            "recipient_country_code",
            default="DE",
        )
        or "DE"
    ).upper()
    recipient_email = str(
        _read_value(
            invoice,
            "recipient_email",
            default="",
        )
        or ""
    ).strip()
    recipient_vat_id = str(
        _read_value(
            invoice,
            "recipient_vat_id",
            default="",
        )
        or ""
    ).replace(" ", "")
    if not recipient_street:
        raise InvoiceDocumentError(
            "Straße des Rechnungsempfängers fehlt."
        )

    total_net = _money(
        sum(
            (
                position["total_net"]
                for position in positions
            ),
            Decimal("0"),
        )
    )
    total_vat = _money(
        sum(
            (
                position["vat_amount"]
                for position in positions
            ),
            Decimal("0"),
        )
    )
    total_gross = total_net + total_vat
    contract_number = str(
        _read_value(
            invoice,
            "recipient_license_agreement_idnum",
            "contract_number",
            "recipient_contract_number",
            "external_contract_number",
            default="",
        )
        or ""
    ).strip()
    payment_reference = str(
        _read_value(
            invoice,
            "payment_reference",
            default=invoice_number,
        )
        or invoice_number
    ).strip()
    payment_terms_text = str(
        _read_value(
            invoice,
            "payment_terms_text",
            default="",
        )
        or ""
    ).strip()

    if not payment_terms_text and due_date is not None:
        payment_terms_text = (
            "Bitte überweisen Sie den Rechnungsbetrag "
            f"bis zum {_format_date(due_date)}."
        )

    seller_address = [
        seller_name,
        seller_street,
        f"{seller_postal_code} {seller_city}",
    ]
    recipient_address = [
        recipient_name,
        recipient_addition,
        recipient_street,
        (
            f"{recipient_postal_code} "
            f"{recipient_city}"
        ),
    ]

    if recipient_country_code != "DE":
        recipient_address.append(
            recipient_country_code
        )

    is_cancellation = (
        invoice.invoice_kind
        == InvoiceKind.CANCELLATION.value
    )
    payment_context = {}
    if not is_cancellation:
        try:
            payment_context = invoice_payment_context(invoice, complete=True)
        except ValueError as exc:
            raise InvoiceDocumentError(str(exc)) from exc
    original_invoice = (
        invoice.original_invoice
        if is_cancellation
        else None
    )
    original_invoice_number = str(
        (
            original_invoice.invoice_number
            if original_invoice is not None
            else ""
        )
        or ""
    ).strip()
    original_invoice_date = (
        original_invoice.invoice_date
        if original_invoice is not None
        else None
    )
    cancellation_reason = str(
        invoice.cancellation_reason or ""
    ).strip()

    if is_cancellation:
        payment_terms_text = ""

    return {
        "invoice_number": invoice_number,
        "invoice_date": invoice_date,
        "invoice_date_formatted": (
            _format_date(invoice_date)
        ),
        "due_date": due_date,
        "due_date_formatted": (
            _format_date(due_date)
        ),
        "service_date_from": service_date_from,
        "service_date_to": service_date_to,
        "service_date_formatted": (
            _format_date(service_date_from)
            if service_date_from == service_date_to
            else (
                f"{_format_date(service_date_from)}"
                f" bis "
                f"{_format_date(service_date_to)}"
            )
        ),
        "seller_name": seller_name,
        "seller_street": seller_street,
        "seller_postal_code": seller_postal_code,
        "seller_city": seller_city,
        "seller_country_code": seller_country_code,
        "seller_tax_number": seller_tax_number,
        "seller_vat_id": seller_vat_id,
        "seller_email": seller_email,
        "seller_phone": seller_phone,
        "seller_iban": seller_iban,
        "seller_bic": seller_bic,
        "seller_bank_name": seller_bank_name,
        "seller_address": seller_address,
        "recipient_name": recipient_name,
        "recipient_street": recipient_street,
        "recipient_postal_code": (
            recipient_postal_code
        ),
        "recipient_city": recipient_city,
        "recipient_country_code": (
            recipient_country_code
        ),
        "recipient_email": recipient_email,
        "recipient_vat_id": recipient_vat_id,
        "recipient_address": [
            value
            for value in recipient_address
            if value
        ],
        "contract_number": contract_number,
        "tenant_billing_reference": invoice.tenant_billing_reference,
        "insurance_claim_number": invoice.insurance_claim_number,
        "buyer_reference": (
            contract_number or invoice_number
        ),
        "payment_reference": payment_reference,
        "payment_terms_text": payment_terms_text,
        **payment_context,
        "intro_text": str(
            _read_value(
                invoice,
                "intro_text",
                "introductory_text",
                default="",
            )
            or ""
        ).strip(),
        "closing_text": str(
            _read_value(
                invoice,
                "concluding_text",
                "closing_text",
                default="",
            )
            or ""
        ).strip(),
        "currency": str(
            _read_value(
                invoice,
                "currency_code",
                "currency",
                default="EUR",
            )
            or "EUR"
        ).upper(),
        "positions": positions,
        "vat_rows": vat_rows,
        "total_net": total_net,
        "total_net_formatted": (
            _format_money(total_net)
        ),
        "total_vat": total_vat,
        "total_vat_formatted": (
            _format_money(total_vat)
        ),
        "total_gross": total_gross,
        "total_gross_formatted": (
            _format_money(total_gross)
        ),
        "is_cancellation": is_cancellation,
        "document_title": (
            "Stornorechnung"
            if is_cancellation
            else "Rechnung"
        ),
        "total_label": (
            "Stornobetrag"
            if is_cancellation
            else "Rechnungsbetrag"
        ),
        "original_invoice_number": (
            original_invoice_number
        ),
        "original_invoice_date": (
            original_invoice_date
        ),
        "original_invoice_date_formatted": (
            _format_date(
                original_invoice_date
            )
        ),
        "cancellation_reason": (
            cancellation_reason
        ),
    }


def _zugferd_data(context: dict) -> dict:
    is_cancellation = context[
        "is_cancellation"
    ]

    def document_amount(
        value: Decimal,
    ) -> Decimal:
        if is_cancellation:
            return abs(value)

        return value

    invoice_lines = []

    for position in context["positions"]:
        invoice_lines.append(
            _compact(
                {
                    "BT-126": str(
                        position["number"]
                    ),
                    "BT-153": (
                        position["description"]
                    ),
                    "BT-127": (
                        position["details"]
                    ),
                    "BT-155": (
                        position["item_code"]
                    ),
                    "BT-146": _xml_amount(
                        abs(
                            position[
                                "unit_price_net"
                            ]
                        )
                    ),
                    "BT-129": _xml_quantity(
                        abs(
                            position["quantity"]
                        )
                    ),
                    "BT-130": (
                        position["unit_code"]
                    ),
                    "BT-151": (
                        position["vat_category"]
                    ),
                    "BT-152": _xml_quantity(
                        abs(
                            position["vat_rate"]
                        )
                    ),
                    "BT-131": _xml_amount(
                        document_amount(
                            position["total_net"]
                        )
                    ),
                }
            )
        )

    vat_breakdown = [
        {
            "BT-116": _xml_amount(
                document_amount(
                    row["net"]
                )
            ),
            "BT-117": _xml_amount(
                document_amount(
                    row["vat"]
                )
            ),
            "BT-118": row["category"],
            "BT-119": _xml_quantity(
                abs(
                    row["rate"]
                )
            ),
        }
        for row in context["vat_rows"]
    ]

    data = {
        "BT-1": context["invoice_number"],
        "BT-2": context["invoice_date"],
        "BT-3": (
            "381"
            if is_cancellation
            else "380"
        ),
        "BT-5": context["currency"],
        "BT-9": (
            None
            if is_cancellation
            else context["due_date"]
        ),
        "BT-10": context["buyer_reference"],
        "BT-20": (
            ""
            if is_cancellation
            else context["payment_terms_text"]
        ),
        "BT-27": context["seller_name"],
        "BT-31": context["seller_vat_id"],
        "BT-32": context["seller_tax_number"],
        "BT-34": context["seller_email"],
        "BT-34-1": (
            "0225"
            if context["seller_email"]
            else None
        ),
        "BT-35": context["seller_street"],
        "BT-37": context["seller_city"],
        "BT-38": context["seller_postal_code"],
        "BT-40": context["seller_country_code"],
        "BT-42": context["seller_phone"],
        "BT-43": context["seller_email"],
        "BT-44": context["recipient_name"],
        "BT-48": context["recipient_vat_id"],
        "BT-49": context["recipient_email"],
        "BT-49-1": (
            "0225"
            if context["recipient_email"]
            else None
        ),
        "BT-50": context["recipient_street"],
        "BT-52": context["recipient_city"],
        "BT-53": context["recipient_postal_code"],
        "BT-55": (
            context["recipient_country_code"]
        ),
        "BT-58": context["recipient_email"],
        "BT-72": context["service_date_to"],
        "BT-73": context["service_date_from"],
        "BT-74": context["service_date_to"],
        "BG-3": (
            [
                {
                    "BT-25": (
                        context[
                            "original_invoice_number"
                        ]
                    ),
                    "BT-26": (
                        context[
                            "original_invoice_date"
                        ]
                    ),
                }
            ]
            if is_cancellation
            else []
        ),
        "BG-23": vat_breakdown,
        "BG-25": invoice_lines,
        "BT-106": _xml_amount(
            document_amount(
                context["total_net"]
            )
        ),
        "BT-109": _xml_amount(
            document_amount(
                context["total_net"]
            )
        ),
        "BT-110": _xml_amount(
            document_amount(
                context["total_vat"]
            )
        ),
        "BT-110-1": context["currency"],
        "BT-112": _xml_amount(
            document_amount(
                context["total_gross"]
            )
        ),
        "BT-115": _xml_amount(
            document_amount(
                context["total_gross"]
            )
        ),
    }

    reference = context["tenant_billing_reference"]
    notes = []
    if reference:
        notes.append(
            f"Leistungsbezug: {reference['tenant_name']}, {reference['property_address']}. "
            f"Mietvertrag {reference['agreement_number']}"
        )
    if context["insurance_claim_number"]:
        notes.append(f"Versicherungsfall-Nr. {context['insurance_claim_number']}")
    if notes:
        data["BG-1"] = [{"BT-22": "\n".join(notes)}]

    if not reference and not is_cancellation:
        if context["payment_method"] == "sepa_debit":
            data.update({"BT-81": "59", "BT-82": "SEPA-Lastschrift", "BT-83": context["payment_reference"],
                         "BT-89": context["payment_mandate_reference"], "BT-90": context["payment_creditor_id"], "BT-91": context["payment_iban"]})
        else:
            data.update({"BT-81": "58", "BT-82": "SEPA-Überweisung", "BT-83": context["payment_reference"],
                         "BT-84": context["payment_iban"], "BT-85": context["seller_name"], "BT-86": context["payment_bic"]})

    return _compact(data)


def _apply_stationery(
    source_path: Path,
    target_path: Path,
    stationery_path: Path,
) -> None:
    content_reader = PdfReader(
        str(source_path)
    )
    stationery_reader = PdfReader(
        str(stationery_path)
    )

    if not stationery_reader.pages:
        raise InvoiceDocumentError(
            "Der Briefbogen enthält keine Seite."
        )

    writer = PdfWriter()

    for page_number, content_page in enumerate(
        content_reader.pages
    ):
        if page_number == 0:
            stationery_page = (
                writer.add_page(stationery_reader.pages[0])
            )
            stationery_page.merge_page(
                content_page
            )
        else:
            writer.add_page(
                content_page
            )

    with target_path.open("wb") as output_file:
        writer.write(output_file)


def _invoice_layout_url_fetcher(url):
    if not url.casefold().startswith("data:"):
        raise ValueError("Grafiken und Schriften im Rechnungs-Layout müssen als Daten-URL eingebunden sein.")
    return default_url_fetcher(url)


def render_invoice_content_pdf(context, *, layout=None, page_layout=None) -> bytes:
    if layout is None:
        layout = settings.get(SettingKeys.INVOICE_LAYOUT) or DEFAULT_INVOICE_LAYOUT
    return HTML(string=render_invoice_html(context, layout), url_fetcher=_invoice_layout_url_fetcher).write_pdf(stylesheets=page_stylesheets(page_layout))


def create_invoice_document(
    invoice: Invoice,
) -> InvoiceDocumentResult:
    positions = _prepare_positions(invoice)
    vat_rows = _prepare_vat_rows(
        positions
    )
    context = _document_context(
        invoice,
        positions,
        vat_rows,
    )
    stationery_path = _stationery_path(
        invoice
    )
    xml_data = _zugferd_data(
        context
    )
    xml_bytes = generate_cii_xml(
        xml_data,
        level="en16931",
        check_xsd=True,
        check_schematron=False,
        prefixed_namespaces=True,
    )

    company_code = _required_text(
        _read_value(
            invoice.company_code,
            "code",
            "name",
        ),
        "Buchwerk",
    )
    safe_company_code = _safe_filename(
        company_code
    )
    safe_invoice_number = _safe_filename(
        context["invoice_number"]
    )
    storage_root = (
        Path(current_app.instance_path)
        / "invoice_documents"
    )
    target_directory = (
        storage_root
        / str(context["invoice_date"].year)
        / safe_company_code
    )
    target_directory.mkdir(
        parents=True,
        exist_ok=True,
    )
    document_prefix = (
        "stornorechnung"
        if context["is_cancellation"]
        else "rechnung"
    )

    pdf_path = (
        target_directory
        / (
            f"{document_prefix}-"
            f"{safe_invoice_number}.pdf"
        )
    )
    xml_path = (
        target_directory
        / (
            f"{document_prefix}-"
            f"{safe_invoice_number}.xml"
        )
    )

    with TemporaryDirectory(
        dir=target_directory
    ) as temporary_directory:
        temporary_path = Path(
            temporary_directory
        )
        content_pdf_path = (
            temporary_path
            / "content.pdf"
        )
        stationery_pdf_path = (
            temporary_path
            / "stationery.pdf"
        )
        zugferd_pdf_path = (
            temporary_path
            / "zugferd.pdf"
        )
        temporary_xml_path = (
            temporary_path
            / "factur-x.xml"
        )

        try:
            content_pdf_path.write_bytes(render_invoice_content_pdf(context, page_layout=company_page_layout(invoice.company_code)))
        except ValueError as exc:
            raise InvoiceDocumentError(str(exc)) from exc
        _apply_stationery(
            content_pdf_path,
            stationery_pdf_path,
            stationery_path,
        )
        try:
            stationery_pdf_path.write_bytes(append_invoice_attachments(invoice, stationery_pdf_path.read_bytes(), lock=True))
        except ValueError as exc:
            raise InvoiceDocumentError(str(exc)) from exc
        temporary_xml_path.write_bytes(
            xml_bytes
        )

        generate_from_file(
            str(stationery_pdf_path),
            xml_bytes,
            flavor="factur-x",
            level="en16931",
            check_xsd=True,
            check_schematron=False,
            pdf_metadata={
                "author": context["seller_name"],
                "keywords": (
                    "Stornorechnung, ZUGFeRD, Factur-X"
                    if context["is_cancellation"]
                    else "Rechnung, ZUGFeRD, Factur-X"
                ),
                "title": (
                    f"{context['document_title']} "
                    f"{context['invoice_number']}"
                ),
                "subject": (
                    f"{context['document_title']} "
                    f"{context['invoice_number']}"
                ),
            },
            lang="de-DE",
            output_pdf_file=str(
                zugferd_pdf_path
            ),
        )

        zugferd_pdf_path.replace(
            pdf_path
        )
        temporary_xml_path.replace(
            xml_path
        )

    pdf_bytes = pdf_path.read_bytes()
    stored_xml_bytes = xml_path.read_bytes()

    from .dms_context import invoice_context
    from .dms_service import register_generated_pdf

    try:
        register_generated_pdf(
            "cancellation_invoice" if context["is_cancellation"] else "invoice",
            "invoice",
            invoice.id,
            pdf_path.name,
            pdf_bytes,
            invoice_context(invoice),
        )
    except (RuntimeError, OSError) as exc:
        raise InvoiceDocumentError(
            "Der Archivierungsauftrag konnte nicht gespeichert werden."
        ) from exc

    return InvoiceDocumentResult(
        pdf_storage_name=str(
            pdf_path.relative_to(
                Path(current_app.instance_path)
            )
        ),
        xml_storage_name=str(
            xml_path.relative_to(
                Path(current_app.instance_path)
            )
        ),
        pdf_sha256=sha256(
            pdf_bytes
        ).hexdigest(),
        xml_sha256=sha256(
            stored_xml_bytes
        ).hexdigest(),
    )


def resolve_invoice_document_path(
    storage_name: str,
) -> Path:
    instance_root = Path(
        current_app.instance_path
    ).resolve()
    document_path = (
        instance_root
        / storage_name
    ).resolve()

    if (
        document_path != instance_root
        and instance_root
        not in document_path.parents
    ):
        raise InvoiceDocumentError(
            "Ungültiger Dokumentpfad."
        )

    return document_path


def remove_invoice_documents(
    result: InvoiceDocumentResult,
) -> None:
    for storage_name in (
        result.pdf_storage_name,
        result.xml_storage_name,
    ):
        try:
            document_path = (
                resolve_invoice_document_path(
                    storage_name
                )
            )
            document_path.unlink(
                missing_ok=True
            )
        except Exception:
            current_app.logger.exception(
                "invoice_document_cleanup_failed "
                "storage_name=%s",
                storage_name,
            )
