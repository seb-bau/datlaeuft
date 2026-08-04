from __future__ import annotations

from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.utils import parseaddr
from hashlib import sha256
from html import escape
import secrets
import smtplib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import (
    AppointmentRequestStatus,
    AppointmentWindow,
    Commission,
    CommissionAppointmentProposal,
    CommissionAppointmentRequest,
    User,
    utc_now,
)
from .settings import SettingKeys, settings


GERMAN_WEEKDAYS = [
    "Montag",
    "Dienstag",
    "Mittwoch",
    "Donnerstag",
    "Freitag",
    "Samstag",
    "Sonntag",
]


def appointment_timezone() -> ZoneInfo:
    timezone_name = settings.get(
        SettingKeys.APPOINTMENT_TIMEZONE
    ).strip()

    try:
        return ZoneInfo(
            timezone_name
            or "Europe/Berlin"
        )
    except ZoneInfoNotFoundError as exc:
        raise ValueError(
            "Die konfigurierte Termin-Zeitzone ist ungültig."
        ) from exc


def normalize_email_address(
    value: str,
) -> str:
    _, email_address = parseaddr(
        value.strip()
    )

    if (
        not email_address
        or "@" not in email_address
        or "." not in email_address.rsplit(
            "@",
            1,
        )[-1]
    ):
        raise ValueError(
            "Die E-Mail-Adresse ist ungültig."
        )

    return email_address.lower()


def appointment_token_hash(
    token: str,
) -> str:
    return sha256(
        token.encode("utf-8")
    ).hexdigest()


def create_appointment_request(
    commission: Commission,
    user: User,
    payload: dict,
) -> tuple[
    CommissionAppointmentRequest,
    str,
]:
    recipient_email = normalize_email_address(
        str(
            payload.get(
                "recipient_email"
            )
            or ""
        )
    )

    recipient_name = (
        str(
            payload.get(
                "recipient_name"
            )
            or ""
        ).strip()[:255]
        or None
    )

    request_message = (
        str(
            payload.get("message")
            or ""
        ).strip()[:2000]
        or None
    )

    proposal_rows = payload.get(
        "proposals"
    )

    if not isinstance(
        proposal_rows,
        list,
    ):
        raise ValueError(
            "Es wurden keine Terminvorschläge übermittelt."
        )

    if not 1 <= len(proposal_rows) <= 12:
        raise ValueError(
            "Es müssen zwischen 1 und 12 Terminvorschläge angegeben werden."
        )

    parsed_rows: list[
        tuple[date, AppointmentWindow]
    ] = []

    seen_values: set[
        tuple[date, int]
    ] = set()

    today = datetime.now(
        appointment_timezone()
    ).date()

    for row in proposal_rows:
        if not isinstance(row, dict):
            raise ValueError(
                "Ein Terminvorschlag ist ungültig."
            )

        try:
            proposed_date = date.fromisoformat(
                str(row.get("date") or "")
            )
            window_id = int(
                row.get("window_id")
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Datum oder Zeitfenster eines Vorschlags ist ungültig."
            ) from exc

        if proposed_date < today:
            raise ValueError(
                "Terminvorschläge dürfen nicht in der Vergangenheit liegen."
            )

        window = (
            AppointmentWindow.query
            .filter(
                AppointmentWindow.id
                == window_id,
                AppointmentWindow.active.is_(
                    True
                ),
            )
            .one_or_none()
        )

        if window is None:
            raise ValueError(
                "Ein ausgewähltes Zeitfenster ist nicht mehr verfügbar."
            )

        unique_value = (
            proposed_date,
            window.id,
        )

        if unique_value in seen_values:
            continue

        seen_values.add(
            unique_value
        )
        parsed_rows.append(
            (
                proposed_date,
                window,
            )
        )

    if not parsed_rows:
        raise ValueError(
            "Es ist kein gültiger Terminvorschlag vorhanden."
        )

    valid_days = max(
        settings.get(
            SettingKeys.APPOINTMENT_LINK_VALID_DAYS
        ),
        1,
    )

    public_token = secrets.token_urlsafe(
        32
    )

    appointment_request = CommissionAppointmentRequest(
        commission=commission,
        created_by=user,
        recipient_name=recipient_name,
        recipient_email=recipient_email,
        message=request_message,
        token_hash=appointment_token_hash(
            public_token
        ),
        status=AppointmentRequestStatus.PENDING.value,
        expires_at=(
            utc_now()
            + timedelta(days=valid_days)
        ),
    )

    for proposed_date, window in parsed_rows:
        appointment_request.proposals.append(
            CommissionAppointmentProposal(
                proposed_date=proposed_date,
                window=window,
                start_time=window.start_time,
                end_time=window.end_time,
                window_name=window.name,
                accepted=False,
            )
        )

    return (
        appointment_request,
        public_token,
    )


def format_proposal(
    proposal: CommissionAppointmentProposal,
) -> str:
    weekday = GERMAN_WEEKDAYS[
        proposal.proposed_date.weekday()
    ]

    return (
        f"{weekday}, "
        f"{proposal.proposed_date.strftime('%d.%m.%Y')}, "
        f"{proposal.start_time.strftime('%H:%M')} bis "
        f"{proposal.end_time.strftime('%H:%M')} Uhr "
        f"({proposal.window_name})"
    )


def send_appointment_request_email(
    appointment_request: CommissionAppointmentRequest,
    public_token: str,
) -> None:
    smtp_host = settings.get(
        SettingKeys.SMTP_HOST
    ).strip()

    smtp_port = settings.get(
        SettingKeys.SMTP_PORT
    )

    smtp_sender = settings.get(
        SettingKeys.SMTP_SENDER
    ).strip()

    public_base_url = settings.get(
        SettingKeys.PUBLIC_BASE_URL
    ).strip().rstrip("/")

    if not smtp_host:
        raise RuntimeError(
            "Es ist kein SMTP-Server konfiguriert."
        )

    if not smtp_sender:
        raise RuntimeError(
            "Es ist keine SMTP-Absenderadresse konfiguriert."
        )

    if not public_base_url:
        raise RuntimeError(
            "Es ist keine öffentliche Basis-URL konfiguriert."
        )

    public_url = (
        f"{public_base_url}/termine/"
        f"{public_token}"
    )

    commission = (
        appointment_request.commission
    )

    order_number = (
        commission.erp_idnum
        or commission.code
        or str(commission.id)
    )

    safe_order_number = (
        str(order_number)
        .replace("\r", " ")
        .replace("\n", " ")
    )

    salutation = (
        f"Guten Tag {appointment_request.recipient_name},"
        if appointment_request.recipient_name
        else "Guten Tag,"
    )

    proposal_lines = [
        format_proposal(proposal)
        for proposal
        in appointment_request.proposals
    ]

    plain_proposals = "\n".join(
        f"- {value}"
        for value in proposal_lines
    )

    optional_message = ""

    if appointment_request.message:
        optional_message = (
            "\n\nNachricht des Handwerkers:\n"
            f"{appointment_request.message}"
        )

    plain_body = (
        f"{salutation}\n\n"
        f"zu Ihrem Auftrag {safe_order_number} "
        "stehen folgende Terminvorschläge zur Auswahl:\n\n"
        f"{plain_proposals}"
        f"{optional_message}\n\n"
        "Bitte öffnen Sie den folgenden Link, "
        "um einen Termin anzunehmen oder alle "
        "Vorschläge abzulehnen:\n\n"
        f"{public_url}\n\n"
        "Für die Terminbestätigung ist keine Anmeldung erforderlich."
    )

    html_proposals = "".join(
        f"<li>{escape(value)}</li>"
        for value in proposal_lines
    )

    html_message = ""

    if appointment_request.message:
        html_message = (
            "<p><strong>Nachricht des Handwerkers:</strong><br>"
            f"{escape(appointment_request.message)}</p>"
        )

    html_body = (
        "<!doctype html>"
        "<html lang=\"de\">"
        "<body style=\"font-family:Arial,sans-serif;line-height:1.5\">"
        f"<p>{escape(salutation)}</p>"
        f"<p>Zu Ihrem Auftrag <strong>{escape(safe_order_number)}</strong> "
        "stehen folgende Terminvorschläge zur Auswahl:</p>"
        f"<ul>{html_proposals}</ul>"
        f"{html_message}"
        "<p>"
        f"<a href=\"{escape(public_url)}\" "
        "style=\"display:inline-block;padding:12px 18px;"
        "background:#0d6efd;color:#fff;text-decoration:none;"
        "border-radius:6px\">Terminvorschläge öffnen</a>"
        "</p>"
        "<p>Für die Terminbestätigung ist keine Anmeldung erforderlich.</p>"
        "</body>"
        "</html>"
    )

    message = EmailMessage()
    message["Subject"] = (
        f"Terminvorschläge zu Auftrag "
        f"{safe_order_number}"
    )
    message["From"] = smtp_sender
    message["To"] = (
        appointment_request.recipient_email
    )
    message.set_content(
        plain_body
    )
    message.add_alternative(
        html_body,
        subtype="html",
    )

    try:
        with smtplib.SMTP(
            smtp_host,
            smtp_port,
            timeout=20,
        ) as smtp:
            smtp.send_message(
                message
            )
    except (
        OSError,
        smtplib.SMTPException,
    ) as exc:
        raise RuntimeError(
            f"Die Termin-E-Mail konnte nicht versendet werden: {exc}"
        ) from exc

    appointment_request.sent_at = (
        utc_now()
    )