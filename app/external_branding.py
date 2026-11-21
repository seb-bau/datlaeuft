from __future__ import annotations

from copy import deepcopy
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

from .mail_service import send_email
from .settings import SettingKeys, normalize_external_portal_name, settings


def get_external_portal_name() -> str:
    return normalize_external_portal_name(
        settings.get(SettingKeys.EXTERNAL_PORTAL_NAME)
    )


def send_external_email(
    message: EmailMessage,
    *,
    smtp_host: str,
    smtp_port: int,
) -> None:
    portal_name = get_external_portal_name()
    outgoing = deepcopy(message)
    sender_address = parseaddr(str(outgoing.get("From", "")))[1]

    if not sender_address:
        raise RuntimeError("Die konfigurierte Absenderadresse ist ungültig.")

    outgoing.replace_header(
        "From",
        formataddr((portal_name, sender_address)),
    )

    send_email(
        outgoing,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        external_craftsman=True,
    )