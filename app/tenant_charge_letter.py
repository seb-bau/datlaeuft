from datetime import date
from types import SimpleNamespace

from markupsafe import escape

from .document_previews import address_lines
from .invoice_correspondence import SIGNATURE_ANCHOR, letter_date, prepend_letter_pdf, render_letter_pdf
from .invoice_document import _format_money
from .invoice_payments import masked_payment_iban, payment_options_from_wowiport
from .settings import LEGACY_TENANT_CHARGE_CLOSING_TEMPLATE, SettingKeys, settings
from .tenant_billing import choose_contract_recipient, contract_recipients


def tenant_letter_recipients(invoice, service) -> list[dict]:
    reference = invoice.tenant_billing_reference
    address = reference["tenant_address"]
    person_id = reference["person_erp_id"]
    if person_id is not None and all(address.get(field) for field in ("name", "street", "postal_code", "town")):
        return [{**address, "person_id": person_id}]

    recipients = contract_recipients(service, reference["agreement_erp_id"])
    if person_id is not None:
        recipients = [choose_contract_recipient(recipients, person_id)]
    recipients = [
        recipient for recipient in recipients
        if all(recipient.get(field) for field in ("street", "postal_code", "town"))
    ]
    if not recipients:
        raise ValueError("Für das Mieteranschreiben fehlt eine vollständige Postanschrift.")
    return recipients


def tenant_letter_payment(invoice, company, service) -> dict:
    agreement_id = invoice.tenant_billing_reference["agreement_erp_id"]
    creditor_id = (company.seller_creditor_id or "").strip()
    available = payment_options_from_wowiport(service, agreement_id, creditor_id)
    debit_options = [option for option in available["options"] if option["method"] == "sepa_debit"]

    if debit_options:
        eligible = [
            option for option in debit_options
            if option["iban"] and option["mandate_reference"] and option["creditor_id"]
            and (not creditor_id or option["creditor_id"] == creditor_id)
        ]
        if len(eligible) != 1:
            raise ValueError("Bitte prüfen Sie das zum Buchwerk passende SEPA-Mandat in Wowiport.")
        mandate = eligible[0]
        return {
            "method": "sepa_debit",
            "source": mandate["source"],
            "mandate_reference": mandate["mandate_reference"],
            "creditor_id": mandate["creditor_id"],
            "iban_display": masked_payment_iban(mandate["iban"]),
        }

    transfer = next((option for option in available["options"] if option["method"] == "bank_transfer"), {})
    if not transfer.get("iban"):
        raise ValueError("Für diesen Mietvertrag ist in Wowiport keine IBAN für Überweisungen hinterlegt.")
    return {field: transfer[field] for field in ("method", "source", "iban", "bic")}


def tenant_letter_purpose(invoice) -> str:
    descriptions = list(dict.fromkeys(
        link.commission.display_title for link in invoice.commission_links if link.commission.display_title
    ))
    if not descriptions:
        descriptions = list(dict.fromkeys(
            position.description for position in invoice.positions if position.description
        ))
    issuer = invoice.company_code
    template = settings.get(SettingKeys.INVOICE_TENANT_CHARGE_PURPOSE_TEMPLATE)
    return template.format(
        invoice_issuer=invoice.seller_name or issuer.seller_name or issuer.name,
        gross_amount=f"{_format_money(invoice.total_gross)} {invoice.currency_code or 'EUR'}",
        work_description="; ".join(descriptions)[:2000],
    ).strip()


def tenant_letter_closing(company, letter) -> str:
    if "closing" in letter:
        return letter["closing"]
    template = LEGACY_TENANT_CHARGE_CLOSING_TEMPLATE if letter else settings.get(SettingKeys.INVOICE_TENANT_CHARGE_CLOSING_TEMPLATE)
    return template.format(seller_name=company.seller_name or company.name, sig="{sig}").strip()


def tenant_letter_context(invoice, company, letter) -> dict:
    name = company.seller_name or company.name
    sender_address = address_lines(company, "seller")
    if not company.seller_name:
        sender_address.insert(0, name)
    recipient = letter["recipient"]
    address = SimpleNamespace(**{f"recipient_{field}": value for field, value in recipient.items()})
    amount = _format_money(invoice.total_gross)
    currency = invoice.currency_code
    due = date.fromisoformat(letter["due_date"]).strftime("%d.%m.%Y")
    agreement = invoice.tenant_billing_reference["agreement_number"]
    payment = letter["payment"]
    if payment.get("source") != "wowiport":
        raise ValueError("Bitte speichern Sie das Mieteranschreiben erneut, um die Zahlungsdaten des Mietvertrags zu übernehmen.")
    if payment["method"] == "sepa_debit":
        terms = (
            f"Den Betrag von {amount} {currency} ziehen wir aufgrund Ihres SEPA-Mandats zum {due} ein. "
            "Fällt der Termin auf ein Wochenende oder einen Feiertag, verschiebt er sich auf den nächsten Werktag.\n"
            f"Mandatsreferenz: {payment['mandate_reference']}\n"
            f"Gläubiger-ID: {payment['creditor_id']}\n"
            f"Einzugskonto (IBAN): {payment['iban_display']}"
        )
    else:
        terms = (
            f"Bitte überweisen Sie {amount} {currency} bis zum {due} an {name} "
            f"auf das Konto mit der IBAN {payment['iban']}."
        )
        if payment.get("bic"):
            terms += f" Die BIC lautet {payment['bic']}."
        terms += f" Bitte geben Sie den Mietvertrag {agreement} als Verwendungszweck an."

    text = escape(
        f"Guten Tag {recipient['name']},\n\n{letter['purpose']}\n\n{terms}\n\n"
        "Eine Kopie der Rechnung ist beigefügt."
    )
    closing = tenant_letter_closing(company, letter)
    if closing:
        text += "\n\n" + escape(closing).replace("{sig}", SIGNATURE_ANCHOR)
    return {
        **vars(address),
        "title": "Mieterbelastung", "text": text,
        "recipient_address": "\n".join(address_lines(address)),
        "sender_line": " · ".join(sender_address),
        "seller_name": name,
        "invoice_number": invoice.invoice_number,
        "invoice_date": invoice.invoice_date.strftime("%d.%m.%Y"),
        "contract_number": agreement,
        "letter_date": date.fromisoformat(letter["letter_date"]).strftime("%d.%m.%Y"),
        "amount": amount, "currency": currency, "due_date": due, "payment_terms": terms,
        "caseworker_name": letter["caseworker_name"],
        "caseworker_email": company.seller_email, "caseworker_phone": company.seller_phone,
        "preview": False,
    }


def render_tenant_charge_letter(invoice, company, letter, *, layout=None) -> bytes:
    return render_letter_pdf(tenant_letter_context(invoice, company, letter), company, layout=layout)


def tenant_charge_package(invoice, company, letter, *, invoice_pdf=None) -> bytes:
    from .invoice_delivery import final_invoice_pdf
    from .work_report_document import WORK_REPORT_SCOPE_ALL, create_invoice_with_work_reports_pdf

    original = final_invoice_pdf(invoice) if invoice_pdf is None else invoice_pdf
    if letter.get("include_work_reports"):
        original = create_invoice_with_work_reports_pdf(invoice, WORK_REPORT_SCOPE_ALL, invoice_pdf=original).getvalue()
    return prepend_letter_pdf(original, render_tenant_charge_letter(invoice, company, letter))
