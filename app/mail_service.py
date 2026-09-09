from __future__ import annotations

from copy import deepcopy
from email.message import EmailMessage
import smtplib

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from .extensions import db
from .models import AppSetting
from .settings import SettingKeys, validate_debug_email_address


def send_email(
    message: EmailMessage,
    *,
    smtp_host: str,
    smtp_port: int,
) -> None:
    try:
        with db.engine.connect() as connection:
            row = connection.execute(
                select(AppSetting.value_text).where(
                    AppSetting.key
                    == SettingKeys.SMTP_DEBUG_RECIPIENT.key
                )
            ).one_or_none()

        debug_recipient = validate_debug_email_address(
            row[0] if row is not None else ""
        )
    except (SQLAlchemyError, ValueError) as exc:
        raise RuntimeError(
            "E-Mail-Versand abgebrochen: Die Debug-E-Mail-Einstellung "
            "konnte nicht sicher gelesen werden oder ist ungültig."
        ) from exc

    outgoing_message = deepcopy(message)
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
