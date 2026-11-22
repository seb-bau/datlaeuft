from __future__ import annotations

from copy import deepcopy
from email import policy
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid, parseaddr
import smtplib

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from .extensions import db
from .models import AppSetting, ExternalCraftsman
from .settings import SettingKeys, validate_debug_email_address


def _valid_single_header(message: EmailMessage, name: str) -> bool:
    headers = message.get_all(name, [])
    if len(headers) != 1:
        return False
    header = headers[0]
    parsed = policy.default.header_factory(name, str(header))
    return bool(str(header).strip()) and not getattr(header, "defects", ()) and not parsed.defects


def send_email(
    message: EmailMessage,
    *,
    smtp_host: str,
    smtp_port: int,
    external_craftsman: bool = False,
) -> str | None:
    try:
        with db.engine.connect() as connection:
            row = connection.execute(
                select(AppSetting.value_text).where(
                    AppSetting.key
                    == SettingKeys.SMTP_DEBUG_RECIPIENT.key
                )
            ).one_or_none()

            debug_recipient = validate_debug_email_address(row[0] if row is not None else "")
            if not debug_recipient:
                external_value = connection.execute(select(AppSetting.value_text).where(
                    AppSetting.key == SettingKeys.SMTP_EXTERNAL_DEBUG_RECIPIENT.key,
                )).scalar_one_or_none()
                external_debug = validate_debug_email_address(external_value or "")
                if external_debug:
                    addresses = [address.strip().lower() for _, address in getaddresses([
                        str(value) for name in ("To", "Cc", "Bcc", "Resent-To", "Resent-Cc", "Resent-Bcc")
                        for value in message.get_all(name, [])
                    ]) if address]
                    is_external = external_craftsman or bool(addresses and connection.execute(
                        select(ExternalCraftsman.id).where(func.lower(func.trim(ExternalCraftsman.email)).in_(addresses)).limit(1)
                    ).first())
                    if is_external:
                        debug_recipient = external_debug
    except (SQLAlchemyError, ValueError) as exc:
        raise RuntimeError(
            "E-Mail-Versand abgebrochen: Die Debug-E-Mail-Einstellung "
            "konnte nicht sicher gelesen werden oder ist ungültig."
        ) from exc

    outgoing_message = deepcopy(message)
    if not _valid_single_header(outgoing_message, "Message-ID"):
        del outgoing_message["Message-ID"]
        sender_domain = parseaddr(str(outgoing_message.get("From", "")))[1].rpartition("@")[2]
        try:
            sender_domain = sender_domain.encode("idna").decode("ascii")
        except UnicodeError:
            sender_domain = ""
        outgoing_message["Message-ID"] = make_msgid(domain=sender_domain or None)
    if not _valid_single_header(outgoing_message, "Date"):
        del outgoing_message["Date"]
        outgoing_message["Date"] = formatdate(usegmt=True)
    recipients = None

    if debug_recipient:
        for header in list(outgoing_message.keys()):
            if (
                header.lower() in {
                    "to",
                    "cc",
                    "bcc",
                    "disposition-notification-to",
                    "return-receipt-to",
                }
                or header.lower().startswith("resent-")
            ):
                del outgoing_message[header]

        outgoing_message["To"] = debug_recipient
        recipients = [debug_recipient]

        subject = str(outgoing_message.get("Subject", ""))
        del outgoing_message["Subject"]
        outgoing_message["Subject"] = f"[DEBUG] {subject}"

    try:
        with smtplib.SMTP(
            smtp_host,
            smtp_port,
            timeout=20,
        ) as smtp:
            smtp.send_message(
                outgoing_message,
                to_addrs=recipients,
            )
    except (OSError, smtplib.SMTPException) as exc:
        raise RuntimeError(
            f"Die E-Mail konnte nicht versendet werden: {exc}"
        ) from exc

    return debug_recipient or None
