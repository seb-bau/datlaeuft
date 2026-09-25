from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from email.message import EmailMessage
from email.utils import parseaddr
from hashlib import sha256
from html import escape
import secrets
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .mail_service import send_email
from uuid import NAMESPACE_URL, uuid5
from icalendar import Calendar, Event

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

    custom_times_enabled = settings.get(
        SettingKeys.APPOINTMENT_CUSTOM_TIMES_ENABLED
    )
    custom_duration_minutes = settings.get(
        SettingKeys.APPOINTMENT_CUSTOM_TIME_DURATION_MINUTES
    )

    if custom_duration_minutes < 1:
        raise ValueError(
            "Die Dauer konkreter Termine muss mindestens eine Minute betragen."
        )

    appointment_zone = appointment_timezone()
    current_datetime = datetime.now(
        appointment_zone
    )

    parsed_rows: list[
        tuple[
            date,
            time,
            time,
            AppointmentWindow | None,
            str,
        ]
    ] = []

    seen_values: set[
        tuple[
            date,
            time,
            time,
        ]
    ] = set()
    proposal_mode: str | None = None

    for row in proposal_rows:
        if not isinstance(row, dict):
            raise ValueError(
                "Ein Terminvorschlag ist ungültig."
            )

        try:
            proposed_date = date.fromisoformat(
                str(row.get("date") or "")
            )
        except ValueError as exc:
            raise ValueError(
                "Das Datum eines Vorschlags ist ungültig."
            ) from exc

        raw_window_id = row.get(
            "window_id"
        )
        has_window = bool(
            raw_window_id is not None
            and str(raw_window_id).strip()
        )

        current_mode = (
            "window"
            if has_window
            else "custom"
        )

        if (
            proposal_mode is not None
            and proposal_mode != current_mode
        ):
            raise ValueError(
                "Zeitprofile und konkrete Uhrzeiten dürfen nicht in einer Nachricht gemischt werden."
            )

        proposal_mode = current_mode

        if has_window:
            try:
                window_id = int(
                    raw_window_id
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "Das Zeitfenster eines Vorschlags ist ungültig."
                ) from exc

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

            proposed_start_time = (
                window.start_time
            )
            proposed_end_time = (
                window.end_time
            )
            window_name = window.name

        else:
            if not custom_times_enabled:
                raise ValueError(
                    "Konkrete Uhrzeiten sind derzeit nicht freigeschaltet."
                )

            raw_start_time = str(
                row.get("start_time")
                or ""
            ).strip()

            try:
                proposed_start_time = (
                    datetime.strptime(
                        raw_start_time,
                        "%H:%M",
                    ).time()
                )
            except ValueError as exc:
                raise ValueError(
                    "Die Beginnzeit eines Vorschlags ist ungültig."
                ) from exc

            proposed_end_datetime = (
                datetime.combine(
                    proposed_date,
                    proposed_start_time,
                    tzinfo=appointment_zone,
                )
                + timedelta(
                    minutes=(
                        custom_duration_minutes
                    )
                )
            )

            if (
                proposed_end_datetime.date()
                != proposed_date
            ):
                raise ValueError(
                    "Die konfigurierte Kalenderdauer darf nicht bis in den Folgetag reichen."
                )

            proposed_end_time = (
                proposed_end_datetime.time()
                .replace(
                    second=0,
                    microsecond=0,
                )
            )
            window = None
            window_name = "Konkrete Uhrzeit"

        proposed_start_datetime = (
            datetime.combine(
                proposed_date,
                proposed_start_time,
                tzinfo=appointment_zone,
            )
        )

        if (
            proposed_start_datetime
            <= current_datetime
        ):
            raise ValueError(
                "Terminvorschläge müssen in der Zukunft liegen."
            )

        if (
            proposed_end_time
            <= proposed_start_time
        ):
            raise ValueError(
                "Das Ende eines Terminvorschlags muss nach dem Beginn liegen."
            )

        unique_value = (
            proposed_date,
            proposed_start_time,
            proposed_end_time,
        )

        if unique_value in seen_values:
            continue

        seen_values.add(
            unique_value
        )
        parsed_rows.append(
            (
                proposed_date,
                proposed_start_time,
                proposed_end_time,
                window,
                window_name,
            )
        )

    if not parsed_rows:
        raise ValueError(
            "Es ist kein gültiger Terminvorschlag vorhanden."
        )

    parsed_rows.sort(
        key=lambda row: (
            row[0],
            row[1],
            row[2],
        )
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

    for (
        proposed_date,
        proposed_start_time,
        proposed_end_time,
        window,
        window_name,
    ) in parsed_rows:
        appointment_request.proposals.append(
            CommissionAppointmentProposal(
                proposed_date=proposed_date,
                window=window,
                start_time=proposed_start_time,
                end_time=proposed_end_time,
                window_name=window_name,
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

    if proposal.window_id is None:
        return (
            f"{weekday}, "
            f"{proposal.proposed_date.strftime('%d.%m.%Y')}, "
            f"Beginn {proposal.start_time.strftime('%H:%M')} Uhr"
        )

    return (
        f"{weekday}, "
        f"{proposal.proposed_date.strftime('%d.%m.%Y')}, "
        f"{proposal.start_time.strftime('%H:%M')} bis "
        f"{proposal.end_time.strftime('%H:%M')} Uhr "
        f"({proposal.window_name})"
    )


def appointment_email_details(
    appointment_request: CommissionAppointmentRequest,
) -> list[str]:
    commission = appointment_request.commission
    craftsman = commission.user or appointment_request.created_by
    craftsman_name = " ".join(
        (craftsman.display_name if craftsman else "Ihr Handwerker").split()
    ) or "Ihr Handwerker"

    details = [f"Handwerker: {craftsman_name}"]

    if settings.get(SettingKeys.APPOINTMENT_EMAIL_INCLUDE_TOPIC):
        topic = " ".join(
            (commission.title or commission.display_title).split()
        )
        if topic:
            details.append(f"Thema: {topic}")

    return details


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

    details = appointment_email_details(appointment_request)
    plain_details = "\n".join(details)
    html_details = "<br>".join(escape(value) for value in details)

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
        f"{plain_details}\n\n"
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
        f"<p>{html_details}</p>"
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

    send_email(
        message,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
    )

    appointment_request.sent_at = (
        utc_now()
    )


def send_appointment_confirmation_email(
    appointment_request: CommissionAppointmentRequest,
    accepted_proposal: CommissionAppointmentProposal,
    appointment_from: datetime,
    appointment_to: datetime,
) -> None:
    if (
        appointment_request.status != AppointmentRequestStatus.ACCEPTED.value
        or not accepted_proposal.accepted
        or accepted_proposal.appointment_request_id != appointment_request.id
    ):
        raise ValueError("Es liegt kein bestätigter Termin vor.")

    if (
        appointment_from.utcoffset() is None
        or appointment_to.utcoffset() is None
    ):
        raise ValueError("Die Zeitzone des bestätigten Termins fehlt.")

    start_at = appointment_from.astimezone(timezone.utc)
    end_at = appointment_to.astimezone(timezone.utc)

    if end_at <= start_at:
        raise ValueError("Das Ende des Termins muss nach dem Beginn liegen.")

    smtp_host = settings.get(SettingKeys.SMTP_HOST).strip()
    smtp_port = settings.get(SettingKeys.SMTP_PORT)
    smtp_sender = settings.get(SettingKeys.SMTP_SENDER).strip()

    if not smtp_host or not smtp_sender:
        raise RuntimeError("SMTP-Server oder Absenderadresse fehlen.")

    commission = appointment_request.commission
    order_number = " ".join(
        str(commission.erp_idnum or commission.code or commission.id).split()
    )
    salutation = (
        f"Guten Tag {appointment_request.recipient_name},"
        if appointment_request.recipient_name
        else "Guten Tag,"
    )
    appointment_text = format_proposal(accepted_proposal)
    details = [
        f"Auftrag: {order_number}",
        *appointment_email_details(appointment_request),
    ]

    if commission.apartment_address:
        details.append(f"Adresse: {commission.apartment_address}")

    plain_details = "\n".join(details)
    html_details = "<br>".join(escape(value) for value in details)
    if accepted_proposal.window_id is None:
        calendar_duration_minutes = max(
            int(
                (
                    appointment_to
                    - appointment_from
                ).total_seconds()
                // 60
            ),
            1,
        )
        calendar_hint = (
            "Die Kalenderdatei reserviert ab dem Terminbeginn "
            f"{calendar_duration_minutes} Minuten. "
            "Die tatsächliche Arbeitsdauer kann davon abweichen."
        )
    else:
        calendar_hint = (
            "Öffnen Sie die Kalenderdatei im Anhang, "
            "um den Termin in Ihren Kalender zu übernehmen."
        )

    message = EmailMessage()
    message["Subject"] = f"Terminbestätigung – Auftrag {order_number}"
    message["From"] = smtp_sender
    message["To"] = normalize_email_address(appointment_request.recipient_email)
    message.set_content(
        f"{salutation}\n\n"
        "Ihr Termin wurde bestätigt:\n\n"
        f"{appointment_text}\n\n"
        f"{plain_details}\n\n"
        f"{calendar_hint}\n"
    )
    message.add_alternative(
        '<!doctype html><html lang="de">'
        '<body style="font-family:Arial,sans-serif;line-height:1.5">'
        f"<p>{escape(salutation)}</p>"
        "<p>Ihr Termin wurde bestätigt:</p>"
        f"<p><strong>{escape(appointment_text)}</strong></p>"
        f"<p>{html_details}</p>"
        f"<p>{escape(calendar_hint)}</p>"
        "</body></html>",
        subtype="html",
    )

    calendar = Calendar()
    calendar.add("prodid", "-//WattNu//Terminvereinbarung//DE")
    calendar.add("version", "2.0")
    calendar.add("calscale", "GREGORIAN")

    event = Event()
    event.add(
        "uid",
        str(uuid5(
            NAMESPACE_URL,
            f"wattnu:appointment:{appointment_request.token_hash}",
        )),
    )
    event.add("dtstamp", utc_now())
    event.add("dtstart", start_at)
    event.add("dtend", end_at)
    event.add("summary", f"Handwerkertermin – Auftrag {order_number}")
    event.add("description", plain_details)
    event.add("status", "CONFIRMED")
    event.add("transp", "OPAQUE")
    event.add("sequence", 0)

    if commission.apartment_address:
        event.add("location", commission.apartment_address)

    calendar.add_component(event)
    message.add_attachment(
        calendar.to_ical(),
        maintype="text",
        subtype="calendar",
        filename="termin.ics",
        params={"charset": "utf-8"},
    )

    send_email(message, smtp_host=smtp_host, smtp_port=smtp_port)


def send_appointment_response_email(
    appointment_request: CommissionAppointmentRequest,
    accepted_proposal: CommissionAppointmentProposal | None,
) -> None:
    commission = appointment_request.commission
    assigned_user = commission.user

    if assigned_user is None:
        raise RuntimeError(
            "Dem Auftrag ist kein Handwerker zugeordnet."
        )

    recipient_email = normalize_email_address(
        assigned_user.email
    )

    smtp_host = settings.get(
        SettingKeys.SMTP_HOST
    ).strip()
    smtp_port = settings.get(
        SettingKeys.SMTP_PORT
    )
    smtp_sender = settings.get(
        SettingKeys.SMTP_SENDER
    ).strip()

    if not smtp_host:
        raise RuntimeError(
            "Es ist kein SMTP-Server konfiguriert."
        )

    if not smtp_sender:
        raise RuntimeError(
            "Es ist keine SMTP-Absenderadresse konfiguriert."
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
        f"Guten Tag {assigned_user.display_name},"
    )

    if (
        appointment_request.status
        == AppointmentRequestStatus.ACCEPTED.value
    ):
        if accepted_proposal is None:
            raise RuntimeError(
                "Der bestätigte Terminvorschlag fehlt."
            )

        subject = (
            f"Termin bestätigt – Auftrag "
            f"{safe_order_number}"
        )

        response_text = (
            "der Bewohner hat folgenden Termin bestätigt:\n\n"
            f"{format_proposal(accepted_proposal)}"
        )
    elif (
        appointment_request.status
        == AppointmentRequestStatus.REJECTED.value
    ):
        subject = (
            f"Terminvorschläge abgelehnt – Auftrag "
            f"{safe_order_number}"
        )

        response_text = (
            "der Bewohner hat alle Terminvorschläge abgelehnt."
        )
    else:
        raise RuntimeError(
            "Der Terminstatus kann nicht benachrichtigt werden."
        )

    detail_lines = [
        salutation,
        "",
        response_text,
        "",
        f"Auftrag: {safe_order_number}",
    ]

    if commission.apartment_address:
        detail_lines.append(
            f"Adresse: {commission.apartment_address}"
        )

    if appointment_request.tenant_comment:
        detail_lines.extend([
            "",
            "Kommentar des Bewohners:",
            appointment_request.tenant_comment,
        ])

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = smtp_sender
    message["To"] = recipient_email
    message.set_content(
        "\n".join(detail_lines)
    )

    send_email(
        message,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
    )
