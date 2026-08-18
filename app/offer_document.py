from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import re

from flask import current_app, render_template
from pypdf import PdfReader, PdfWriter
from weasyprint import HTML

from .models import Offer


MONEY_QUANTIZER = Decimal("0.01")
QUANTITY_QUANTIZER = Decimal("0.001")


class OfferDocumentError(RuntimeError):
    pass


@dataclass(frozen=True)
class OfferDocumentResult:
    pdf_storage_name: str
    pdf_sha256: str


def decimal_value(
    value,
    default: str = "0",
) -> Decimal:
    if value in (None, ""):
        return Decimal(default)

    if isinstance(value, Decimal):
        return value

    return Decimal(
        str(value).replace(",", ".")
    )


def money_value(value) -> Decimal:
    return decimal_value(value).quantize(
        MONEY_QUANTIZER,
        rounding=ROUND_HALF_UP,
    )


def required_text(
    value,
    label: str,
) -> str:
    normalized = str(value or "").strip()

    if not normalized:
        raise OfferDocumentError(
            f"{label} fehlt."
        )

    return normalized


def format_date(value: date | None) -> str:
    if value is None:
        return ""

    return value.strftime("%d.%m.%Y")


def format_money(value) -> str:
    normalized = money_value(value)
    formatted = f"{normalized:,.2f}"

    return (
        formatted
        .replace(",", "_")
        .replace(".", ",")
        .replace("_", ".")
    )


def format_quantity(value) -> str:
    normalized = decimal_value(value).quantize(
        QUANTITY_QUANTIZER,
        rounding=ROUND_HALF_UP,
    )

    return (
        format(normalized, "f")
        .rstrip("0")
        .rstrip(".")
        .replace(".", ",")
    )


def safe_filename(value: str) -> str:
    normalized = re.sub(
        r"[^A-Za-z0-9._-]+",
        "-",
        value.strip(),
    ).strip("-")

    if not normalized:
        raise OfferDocumentError(
            "Die Angebotsnummer kann nicht "
            "als Dateiname verwendet werden."
        )

    return normalized


def stationery_path(
    offer: Offer,
) -> Path:
    filename = str(
        offer.company_code.stationery_filename
        or ""
    ).strip()

    if not filename:
        raise OfferDocumentError(
            "Der Briefbogen des Buchwerks fehlt."
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

    raise OfferDocumentError(
        f"Der Briefbogen {filename} "
        "wurde nicht gefunden."
    )


def prepare_positions(
    offer: Offer,
) -> list[dict]:
    source_positions = sorted(
        offer.positions,
        key=lambda position: (
            position.position_index,
            position.id or 0,
        ),
    )

    if not source_positions:
        raise OfferDocumentError(
            "Das Angebot enthält keine Positionen."
        )

    positions = []

    for number, position in enumerate(
        source_positions,
        start=1,
    ):
        quantity = decimal_value(
            position.quantity,
            "1",
        )
        unit_price_net = money_value(
            position.unit_price_net
        )
        total_net = money_value(
            position.net_amount
        )
        vat_rate = decimal_value(
            position.vat_rate
        )
        vat_amount = money_value(
            position.vat_amount
        )
        gross_amount = money_value(
            position.gross_amount
        )

        positions.append(
            {
                "number": number,
                "description": required_text(
                    position.description,
                    (
                        "Bezeichnung der Position "
                        f"{number}"
                    ),
                ),
                "details": str(
                    position.details or ""
                ).strip(),
                "quantity": quantity,
                "quantity_formatted": (
                    format_quantity(quantity)
                ),
                "unit": str(
                    position.unit or "Stk"
                ).strip(),
                "unit_price_net": (
                    unit_price_net
                ),
                "unit_price_formatted": (
                    format_money(unit_price_net)
                ),
                "vat_rate": vat_rate,
                "vat_rate_formatted": (
                    format_quantity(vat_rate)
                ),
                "total_net": total_net,
                "total_net_formatted": (
                    format_money(total_net)
                ),
                "vat_amount": vat_amount,
                "gross_amount": gross_amount,
            }
        )

    return positions


def prepare_vat_rows(
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
        groups[rate]["net"] += (
            position["total_net"]
        )
        groups[rate]["vat"] += (
            position["vat_amount"]
        )

    return [
        {
            "rate_formatted": (
                format_quantity(rate)
            ),
            "net_formatted": format_money(
                values["net"]
            ),
            "vat_formatted": format_money(
                values["vat"]
            ),
        }
        for rate, values in sorted(
            groups.items(),
            key=lambda item: item[0],
        )
    ]


def recipient_address(
    offer: Offer,
) -> list[str]:
    lines = []

    for value in (
        offer.recipient_name,
        offer.recipient_name_addition,
        offer.recipient_street,
    ):
        normalized = str(value or "").strip()

        if normalized:
            lines.append(normalized)

    town_line = " ".join(
        value
        for value in (
            str(
                offer.recipient_postal_code
                or ""
            ).strip(),
            str(
                offer.recipient_town
                or ""
            ).strip(),
        )
        if value
    )

    if town_line:
        lines.append(town_line)

    country_code = str(
        offer.recipient_country_code
        or "DE"
    ).strip().upper()

    if country_code != "DE":
        lines.append(country_code)

    return lines


def document_context(
    offer: Offer,
    positions: list[dict],
    vat_rows: list[dict],
) -> dict:
    return {
        "document_title": "Angebot",
        "offer_number": required_text(
            offer.offer_number,
            "Angebotsnummer",
        ),
        "offer_date_formatted": format_date(
            offer.offer_date
        ),
        "valid_until_formatted": format_date(
            offer.valid_until
        ),
        "recipient_address": (
            recipient_address(offer)
        ),
        "recipient_name": (
            offer.recipient_name or ""
        ),
        "contract_number": (
            offer.license_agreement_idnum
            or ""
        ),
        "object_address": (
            offer.object_address or ""
        ),
        "object_location": (
            offer.object_location or ""
        ),
        "craft_activity_name": (
            offer.craft_activity.name
            if offer.craft_activity
            else ""
        ),
        "title": offer.title or "",
        "introductory_text": (
            offer.introductory_text or ""
        ),
        "concluding_text": (
            offer.concluding_text or ""
        ),
        "positions": positions,
        "vat_rows": vat_rows,
        "currency": offer.currency_code,
        "total_net_formatted": format_money(
            offer.total_net
        ),
        "total_vat_formatted": format_money(
            offer.total_vat
        ),
        "total_gross_formatted": format_money(
            offer.total_gross
        ),
    }


def apply_stationery(
    source_path: Path,
    target_path: Path,
    background_path: Path,
) -> None:
    content_reader = PdfReader(
        str(source_path)
    )
    background_reader = PdfReader(
        str(background_path)
    )

    if not background_reader.pages:
        raise OfferDocumentError(
            "Der Briefbogen enthält keine Seite."
        )

    writer = PdfWriter()

    for page_number, content_page in enumerate(
        content_reader.pages
    ):
        if page_number == 0:
            background_page = (
                background_reader.pages[0]
            )
            background_page.merge_page(
                content_page
            )
            writer.add_page(background_page)
        else:
            writer.add_page(content_page)

    with target_path.open("wb") as output_file:
        writer.write(output_file)


def create_offer_document(
    offer: Offer,
) -> OfferDocumentResult:
    if offer.offer_date is None:
        raise OfferDocumentError(
            "Das Angebotsdatum fehlt."
        )

    positions = prepare_positions(offer)
    vat_rows = prepare_vat_rows(positions)
    context = document_context(
        offer,
        positions,
        vat_rows,
    )
    background_path = stationery_path(offer)
    company_code = safe_filename(
        offer.company_code_code
    )
    offer_number = safe_filename(
        context["offer_number"]
    )

    storage_root = (
        Path(current_app.instance_path)
        / "offer_documents"
    )
    target_directory = (
        storage_root
        / str(offer.offer_date.year)
        / company_code
    )
    target_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    pdf_path = (
        target_directory
        / f"angebot-{offer_number}.pdf"
    )

    with TemporaryDirectory() as directory:
        temporary_directory = Path(directory)
        content_path = (
            temporary_directory
            / "content.pdf"
        )
        merged_path = (
            temporary_directory
            / "offer.pdf"
        )

        html = render_template(
            "admin/offer_pdf.html",
            **context,
        )

        HTML(
            string=html,
            base_url=current_app.root_path,
        ).write_pdf(
            str(content_path)
        )

        apply_stationery(
            content_path,
            merged_path,
            background_path,
        )

        merged_path.replace(pdf_path)

    pdf_bytes = pdf_path.read_bytes()

    return OfferDocumentResult(
        pdf_storage_name=str(
            pdf_path.relative_to(
                Path(current_app.instance_path)
            )
        ),
        pdf_sha256=sha256(
            pdf_bytes
        ).hexdigest(),
    )


def resolve_offer_document_path(
    storage_name: str,
) -> Path:
    storage_root = (
        Path(current_app.instance_path)
        / "offer_documents"
    ).resolve()
    candidate = (
        Path(current_app.instance_path)
        / storage_name
    ).resolve()

    if (
        candidate != storage_root
        and storage_root not in candidate.parents
    ):
        raise OfferDocumentError(
            "Der Angebotsdokumentpfad "
            "ist ungültig."
        )

    return candidate


def remove_offer_document(
    result: OfferDocumentResult,
) -> None:
    try:
        resolve_offer_document_path(
            result.pdf_storage_name
        ).unlink(missing_ok=True)
    except OSError:
        current_app.logger.exception(
            "offer_document_cleanup_failed "
            "storage_name=%s",
            result.pdf_storage_name,
        )