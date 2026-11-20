from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from .document_previews import address_lines
from .invoice_correspondence import cover_letter_stationery, render_cover_letter_content_pdf
from .invoice_document import _apply_stationery, _stationery_path, render_invoice_content_pdf
from .invoice_layout import example_invoice_layout_context
from .offer_document import render_offer_content_pdf
from .stationery_layout import company_page_layout


def selected_stationery(company, slot, form, uploads):
    if slot in uploads:
        return uploads[slot]
    if f"remove_{slot}" not in form:
        filename = getattr(company, slot, None)
        if filename:
            path = cover_letter_stationery(company) if slot == "cover_letter_stationery" else _stationery_path(SimpleNamespace(company_code=company))
            return path.read_bytes()
    if slot == "cover_letter_stationery":
        return selected_stationery(company, "stationery", form, uploads)
    return None


def sample_document_context(company, form):
    values = example_invoice_layout_context()
    today = datetime.now(ZoneInfo("Europe/Berlin")).date()
    seller = SimpleNamespace(**{
        f"seller_{field}": form.get(f"seller_{field}", getattr(company, f"seller_{field}", None))
        for field in ("name", "name_addition", "street", "postal_code", "town", "country_code")
    })
    values.update(
        preview=True, seller_address=address_lines(seller, "seller") or values["seller_address"],
        invoice_date_formatted=today.strftime("%d.%m.%Y"),
        due_date_formatted=(today + timedelta(days=14)).strftime("%d.%m.%Y"),
        service_date_formatted=today.strftime("%d.%m.%Y"),
        total_net_formatted="400,00", total_vat_formatted="76,00", total_gross_formatted="476,00",
        vat_rows=[{"rate_formatted": "19", "net_formatted": "400,00", "vat_formatted": "76,00"}],
        payment_terms_text="Bitte überweisen Sie den Rechnungsbetrag innerhalb von 14 Tagen unter Angabe der Rechnungsnummer.",
    )
    values["positions"] = [{
        "number": index, "article_number": f"BEISPIEL-{index:03d}",
        "description": f"Beispielposition {index:02d}: Reparatur- und Instandhaltungsarbeiten",
        "details": "Beispieldaten zur Prüfung der Seitenumbrüche und des freien Fußbereichs.",
        "quantity_formatted": "1", "unit": "Stk", "unit_price_formatted": "10,00",
        "vat_rate_formatted": "19", "total_net_formatted": "10,00",
    } for index in range(1, 41)]
    return values


def render_stationery_preview(company, kind, layouts, form, uploads):
    slot = "cover_letter_stationery" if kind == "cover_letter" else "stationery"
    own_cover = "cover_letter_stationery" in uploads or (
        bool(company.cover_letter_stationery_filename) and "remove_cover_letter_stationery" not in form
    )
    page_layout = company_page_layout(company, slot, layouts=layouts, has_own_cover_stationery=own_cover)
    context = sample_document_context(company, form)
    if kind == "invoice":
        content = render_invoice_content_pdf(context, page_layout=page_layout)
    elif kind == "offer":
        context.update(
            document_title="Angebot", offer_number="AN-BEISPIEL-001",
            offer_date_formatted=context["invoice_date_formatted"], valid_until_formatted=context["due_date_formatted"],
            title="Beispielangebot", introductory_text="Gerne bieten wir Ihnen die folgenden Leistungen an.",
            concluding_text="Wir freuen uns auf Ihre Beauftragung.", object_address="Musterstraße 12",
            object_location="Erdgeschoss", craft_activity_name="Instandhaltung",
        )
        content = render_offer_content_pdf(context, page_layout=page_layout)
    elif kind == "cover_letter":
        context.update(
            title="Ihre Rechnung – Beispielanschreiben", recipient_address="\n".join(context["recipient_address"]),
            invoice_number="RE-BEISPIEL-001", letter_date=context["invoice_date_formatted"],
            invoice_date=context["invoice_date_formatted"], due_date=context["due_date_formatted"],
            seller_name=context["seller_address"][0], sender_line=" · ".join(context["seller_address"]),
            caseworker_name="Erika Musterfrau", caseworker_phone="02306 123456", caseworker_email="erika@example.invalid",
            amount=context["total_gross_formatted"], payment_terms=context["payment_terms_text"],
            text="Sehr geehrte Damen und Herren,\n\n" + "\n\n".join(
                f"Absatz {index}: Dieser Beispieltext zeigt den verfügbaren Druckbereich und die Seitenumbrüche des Anschreibens. "
                "Der untere Freiraum schützt die Angaben auf Ihrem Geschäftspapier."
                for index in range(1, 21)
            ) + "\n\nBitte beachten Sie die Zahlungsanweisungen auf der beigefügten Rechnung.",
        )
        content = render_cover_letter_content_pdf(context, page_layout=page_layout)
    else:
        raise ValueError("Die Dokumentart für die Briefbogen-Vorschau ist ungültig.")
    background = selected_stationery(company, slot, form, uploads)
    if background is not None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source, target, paper = root / "content.pdf", root / "preview.pdf", root / "stationery.pdf"
            source.write_bytes(content)
            paper.write_bytes(background)
            _apply_stationery(source, target, paper)
            content = target.read_bytes()
    return content
