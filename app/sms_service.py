from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable

import phonenumbers
import requests

from .settings import SMS_DEBUG_SUPPRESSED_COMMISSION_ID, SettingKeys, settings


SEVEN_API_URL = "https://gateway.seven.io/api"
REQUEST_TIMEOUT = 10


class SmsSendError(RuntimeError):
    pass


class SmsDisabled(SmsSendError):
    pass


class SmsSuppressed(SmsSendError):
    pass


class SmsDeliveryUncertain(SmsSendError):
    pass


@dataclass(frozen=True)
class SmsResult:
    state: str
    message_id: str | None = None


def sms_is_suppressed(commission_id: int | None = None) -> bool:
    return bool(settings.get(SettingKeys.SMTP_DEBUG_RECIPIENT).strip()) or (
        commission_id is not None and commission_id <= settings.get(SMS_DEBUG_SUPPRESSED_COMMISSION_ID)
    )


def normalize_phone_number(number: str, default_region: str = "DE") -> str | None:
    if not isinstance(number, str) or not re.fullmatch(r"\+?[\d\s()./\-]+", number.strip()):
        return None
    number = number.strip()
    candidates = [number]
    if not number.startswith(("+", "0")):
        candidates.insert(0, "+" + number)
    for candidate in candidates:
        try:
            parsed = phonenumbers.parse(candidate, default_region)
        except phonenumbers.NumberParseException:
            continue
        if phonenumbers.is_valid_number(parsed) and not parsed.extension:
            return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    return None


def send_sms(
    recipient_number: str,
    text: str,
    *,
    foreign_id: str | None = None,
    before_send: Callable[[], None] | None = None,
) -> SmsResult:
    if sms_is_suppressed():
        raise SmsSuppressed("Der SMS-Versand ist durch die Debug-E-Mail-Adresse gesperrt.")
    if not settings.get(SettingKeys.SMS_ENABLED):
        raise SmsDisabled("Der SMS-Versand ist global deaktiviert.")
    api_key = settings.get(SettingKeys.SMS_API_KEY).strip()
    if not api_key:
        raise SmsSendError("Der seven API-Key für den SMS-Versand fehlt.")
    number = normalize_phone_number(recipient_number)
    if not number:
        return SmsResult("skipped")
    number_type = phonenumbers.number_type(phonenumbers.parse(number, None))
    if number_type not in (phonenumbers.PhoneNumberType.MOBILE, phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE):
        return SmsResult("skipped")
    if not isinstance(text, str) or not text.strip():
        raise SmsSendError("Der SMS-Text ist leer.")
    headers = {"X-Api-Key": api_key, "Accept": "application/json"}
    try:
        response = requests.get(
            SEVEN_API_URL + "/lookup/format", params={"number": number}, headers=headers, timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        lookup = response.json()
    except (requests.RequestException, ValueError, TypeError):
        raise SmsSendError("Die Mobilfunkprüfung bei seven ist fehlgeschlagen.") from None
    if not isinstance(lookup, dict) or lookup.get("success") is not True:
        raise SmsSendError("seven konnte die Rufnummer nicht prüfen.")
    if lookup.get("network_type") != "mobile":
        return SmsResult("skipped")
    if normalize_phone_number(lookup.get("international")) != number:
        raise SmsSendError("seven hat bei der Mobilfunkprüfung eine abweichende Rufnummer zurückgegeben.")
    payload = {"to": number, "text": text}
    sender = settings.get(SettingKeys.SMS_SENDER).strip()
    if sender:
        payload["from"] = sender
    if foreign_id:
        payload["foreign_id"] = foreign_id
    if before_send is not None:
        before_send()
    if sms_is_suppressed():
        raise SmsSuppressed("Der SMS-Versand ist durch die Debug-E-Mail-Adresse gesperrt.")
    if not settings.get(SettingKeys.SMS_ENABLED):
        raise SmsDisabled("Der SMS-Versand ist global deaktiviert.")
    try:
        response = requests.post(SEVEN_API_URL + "/sms", json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
    except requests.RequestException:
        raise SmsDeliveryUncertain("Die SMS-Übergabe an seven ist unklar. Bitte vor einem erneuten Versand im seven-Konto prüfen.") from None
    if 400 <= response.status_code < 500:
        raise SmsSendError(f"seven hat die SMS-Anfrage mit HTTP {response.status_code} abgelehnt.")
    if not 200 <= response.status_code < 300:
        raise SmsDeliveryUncertain("seven hat die SMS-Übergabe nicht eindeutig bestätigt. Bitte den Versand im seven-Konto prüfen.")
    try:
        result = response.json()
    except (ValueError, TypeError):
        raise SmsDeliveryUncertain("Die Antwort nach der SMS-Übergabe ist unlesbar. Bitte den Versand im seven-Konto prüfen.") from None
    if not isinstance(result, dict):
        raise SmsDeliveryUncertain("seven hat die SMS-Übergabe nicht eindeutig bestätigt.")
    code = str(result.get("success", ""))
    if code not in {"100", "101"}:
        if code.isdigit():
            raise SmsSendError(f"seven hat die SMS abgelehnt (Code {code}).")
        raise SmsDeliveryUncertain("seven hat die SMS-Übergabe nicht eindeutig bestätigt.")
    if str(result.get("debug", "false")).lower() == "true":
        return SmsResult("simulated")
    messages = result.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict) or normalize_phone_number(message.get("recipient")) != number:
                continue
            if message.get("success") is True and message.get("id"):
                return SmsResult("sent", str(message["id"]))
            if message.get("success") is False:
                raise SmsSendError("seven hat die SMS für den vorgesehenen Empfänger abgelehnt.")
    raise SmsDeliveryUncertain("seven hat die Annahme der SMS für den vorgesehenen Empfänger nicht eindeutig bestätigt.")
