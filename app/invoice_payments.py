from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import json
import re
from zoneinfo import ZoneInfo

from flask import request
from stdnum.iban import is_valid as valid_iban

from .settings import SettingKeys, settings, validate_invoice_payment_text


PAYMENT_LABELS = {"bank_transfer": "Überweisung", "sepa_debit": "SEPA-Mandat"}
PAYMENT_FIELDS = {"iban": 80, "bic": 40, "mandate_reference": 140, "creditor_id": 80, "account_holder": 255}


def payment_value(row, key, default=None):
    if not isinstance(row, dict):
        return default
    target = key.replace("_", "").casefold()
    return next((value for name, value in row.items() if name.replace("_", "").casefold() == target), default)


def normalize_payment_information(value, complete=False):
    if not isinstance(value, dict):
        raise ValueError("Die Zahlungsinformationen sind ungültig.")
    result = {"method": str(value.get("method") or ""), "source": str(value.get("source") or "manual")[:40]}
    if result["method"] not in {*PAYMENT_LABELS, ""}:
        raise ValueError("Bitte eine gültige Zahlungsart auswählen.")
    for field, maximum in PAYMENT_FIELDS.items():
        text = str(value.get(field) or "").strip()
        if len(text) > maximum or any(ord(character) < 32 or ord(character) == 127 for character in text):
            raise ValueError("Die Angaben zu Mandat und Bankverbindung sind zu lang oder enthalten ungültige Zeichen.")
        result[field] = re.sub(r"\s+", "", text).upper() if field in {"iban", "bic"} else text
    if result["iban"] and not valid_iban(result["iban"]):
        raise ValueError("Die IBAN in den Zahlungsinformationen ist ungültig.")
    if result["bic"] and not re.fullmatch(r"[A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?", result["bic"]):
        raise ValueError("Die BIC in den Zahlungsinformationen ist ungültig.")
    if value.get("license_agreement_erp_id") is not None:
        try:
            result["license_agreement_erp_id"] = int(value["license_agreement_erp_id"])
        except (ValueError, TypeError) as exc:
            raise ValueError("Der Vertrag der Zahlungsinformationen ist ungültig.") from exc
    if "terms_text" in value:
        result["terms_text"] = str(value["terms_text"] or "")
    if complete:
        if not result["method"] or not result["iban"]:
            raise ValueError("Bitte die Zahlungsinformationen abrufen oder über „ändern“ vervollständigen.")
        if result["method"] == "sepa_debit" and (not result["mandate_reference"] or not result["creditor_id"]):
            raise ValueError("Für das SEPA-Mandat fehlen die Mandatsreferenz oder die Gläubiger-ID. Bitte die Zahlungsinformationen ergänzen.")
    return result


def invoice_payment_information(invoice):
    value = getattr(invoice, "payment_information", None)
    agreement_id = invoice.recipient_license_agreement_erp_id
    if isinstance(value, dict):
        if value.get("license_agreement_erp_id") != agreement_id:
            return {"method": "", "iban": "", "bic": "", "source": "unresolved", "license_agreement_erp_id": agreement_id}
        return dict(value)
    company = invoice.company_code
    if agreement_id is not None and invoice.status == "draft":
        return {"method": "", "iban": "", "bic": "", "source": "unresolved", "license_agreement_erp_id": agreement_id}
    return {"method": "bank_transfer", "iban": invoice.seller_iban or company.seller_iban or "",
            "bic": invoice.seller_bic or company.seller_bic or "", "source": "company", "license_agreement_erp_id": agreement_id}


def save_invoice_payment_form(invoice):
    raw = request.form.get("payment_information_data")
    if raw is None:
        value = invoice_payment_information(invoice)
    else:
        try:
            value = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise ValueError("Die Zahlungsinformationen konnten nicht gelesen werden.") from exc
    value = normalize_payment_information(value)
    if value.get("license_agreement_erp_id") != invoice.recipient_license_agreement_erp_id:
        raise ValueError("Die Zahlungsinformationen gehören zu einem anderen Vertrag. Bitte neu abrufen.")
    value.pop("terms_text", None)
    invoice.payment_information = value


def masked_payment_iban(value):
    return f"{value[:4]} **** **** **** {value[-4:]}" if value else ""


def invoice_payment_context(invoice, complete=False):
    if getattr(invoice, "tenant_billing_reference", None):
        return {"payment_terms_text": ""}
    value = normalize_payment_information(invoice_payment_information(invoice), complete=complete)
    display_iban = masked_payment_iban(value["iban"]) if value["method"] == "sepa_debit" else value["iban"]
    number = invoice.invoice_number or "Entwurf"
    due = invoice.due_date.strftime("%d.%m.%Y") if invoice.due_date else "vereinbarten Fälligkeitstermin"
    definition = SettingKeys.INVOICE_SEPA_PAYMENT_TEXT if value["method"] == "sepa_debit" else SettingKeys.INVOICE_TRANSFER_PAYMENT_TEXT
    template = settings.get(definition)
    validate_invoice_payment_text(template)
    text = value.get("terms_text")
    if text is None:
        text = template.format_map({"due_date": due, "invoice_number": number, "payment_reference": invoice.payment_reference or number,
                                    "amount": f"{Decimal(str(invoice.total_gross or 0)):,.2f}".replace(",", "_").replace(".", ",").replace("_", "."),
                                    "currency": invoice.currency_code or "EUR", **{field: value.get(field, "") for field in PAYMENT_FIELDS},
                                    "iban": display_iban, "bic": "" if value["method"] == "sepa_debit" else value["bic"]})
    if value["method"] == "sepa_debit" and value["iban"]:
        text = text.replace(value["iban"], display_iban)
    if not value["method"]:
        text = "Zahlungsinformationen noch nicht vollständig. Bitte vor dem Abschluss prüfen."
    return {"payment_method": value["method"], "payment_method_label": PAYMENT_LABELS.get(value["method"], "Noch nicht festgelegt"),
            "payment_iban": value["iban"], "payment_iban_display": display_iban, "payment_bic": value["bic"], "payment_account_holder": value["account_holder"],
            "payment_mandate_reference": value["mandate_reference"], "payment_creditor_id": value["creditor_id"], "payment_terms_text": text}


def freeze_invoice_payment_information(invoice):
    if invoice.tenant_billing_reference:
        return
    context = invoice_payment_context(invoice, complete=True)
    invoice.payment_information = {**normalize_payment_information(invoice_payment_information(invoice), complete=True),
                                   "terms_text": context["payment_terms_text"]}


def payment_options_from_wowiport(service, agreement_id, creditor_id=""):
    today = datetime.now(ZoneInfo("Europe/Berlin")).date()
    agreements = service.read_all_rows("RentAccounting/LicenseAgreements", {
        "licenseAgreementId": agreement_id, "includeBanking": "true", "showNullValues": "true",
    }, paginate=False)
    agreement = next((row for row in agreements if int(payment_value(row, "Id") or 0) == agreement_id), None)
    if agreement is None:
        raise ValueError("Der Vertrag konnte nicht aus Wowiport geladen werden.")
    banking = payment_value(agreement, "Banking") or {}
    collective = payment_value(banking, "CollectiveAccount") or {}
    virtual_iban = payment_value(banking, "VirtualIban")
    use_virtual_iban = payment_value(banking, "UseVirtualIban")
    if use_virtual_iban is True or (use_virtual_iban is not False and virtual_iban):
        transfer_iban = virtual_iban
    else:
        transfer_iban = None if payment_value(collective, "NoRealBankAccount") else payment_value(collective, "Iban")
    transfer = {"method": "bank_transfer", "iban": transfer_iban or "", "bic": payment_value(collective, "Bic") or "",
                "account_holder": payment_value(collective, "AccountHolder") or "", "source": "wowiport", "license_agreement_erp_id": agreement_id}
    rows = service.read_all_rows("RentAccountingPersonDetails/PaymentModes", {
        "licenseAgreementId": agreement_id, "paymentModeActiveOn": today.isoformat(), "showNullValues": "true",
    })
    options = []
    for row in rows:
        contract = payment_value(row, "LicenseAgreement") or {}
        if int(payment_value(contract, "Id") or 0) != agreement_id:
            continue
        start = str(payment_value(row, "ActiveFrom") or "")[:10]
        end = str(payment_value(row, "ActiveTo") or "")[:10]
        if (
            (start and start > today.isoformat()) or (end and end < today.isoformat())
            or payment_value(row, "Active") is False
        ):
            continue
        mode = payment_value(row, "Mode") or {}
        mandate = payment_value(row, "SepaMandate") or {}
        account = payment_value(row, "BankAccount") or {}
        mode_name = str(payment_value(mode, "Name") or "").casefold()
        is_transfer = any(word in mode_name for word in ("überweisung", "bank transfer", "keine lastschrift", "ohne lastschrift"))
        is_sepa = not is_transfer and (bool(mandate) or any(word in mode_name for word in ("sepa", "lastschrift")))
        if is_sepa and payment_value(mandate, "Active") is False:
            continue
        mandate_iban = re.sub(r"\s+", "", str(payment_value(mandate, "Iban") or "")).upper()
        account_iban = re.sub(r"\s+", "", str(payment_value(account, "Iban") or "")).upper()
        if is_sepa and mandate_iban and account_iban and mandate_iban != account_iban:
            raise ValueError("Wowiport liefert unterschiedliche IBANs für Mandat und Einzugskonto. Bitte die Zahlungsinformationen prüfen.")
        value = ({"method": "sepa_debit", "iban": payment_value(mandate, "Iban") or payment_value(account, "Iban") or "", "bic": payment_value(account, "Bic") or "",
                  "mandate_reference": payment_value(mandate, "MandateId") or "", "creditor_id": payment_value(mandate, "CreditorIdentifier") or creditor_id or "",
                  "account_holder": payment_value(account, "AccountHolder") or "", "source": "wowiport", "license_agreement_erp_id": agreement_id}
                 if is_sepa else dict(transfer))
        value = normalize_payment_information(value)
        if value not in options:
            options.append(value)
    if not options:
        options = [normalize_payment_information(transfer)]
    selected = options[0] if len(options) == 1 else {"method": "", "iban": "", "bic": "", "source": "unresolved", "license_agreement_erp_id": agreement_id}
    alternatives = list(options)
    normalized_transfer = normalize_payment_information(transfer)
    if normalized_transfer["iban"] and normalized_transfer not in alternatives:
        alternatives.append(normalized_transfer)
    return {"selected": selected, "options": alternatives, "requires_selection": len(options) > 1}
