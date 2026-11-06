from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from email.message import EmailMessage
from email.utils import parseaddr
from hashlib import sha256
import secrets
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .mail_service import send_email
from uuid import NAMESPACE_URL, uuid5
from icalendar import Calendar, Event
from .email_templates import apply_email_template, commission_email_context, render_email_template

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


def parse_manual_appointment(
    payload: dict,
) -> tuple[datetime, bool, str | None]:
    if not isinstance(payload, dict):
        raise ValueError("Die Termindaten sind ungültig.")

    raw_date = payload.get("date")
    try:
        if not isinstance(raw_date, str):
            raise ValueError
        appointment_date = date.fromisoformat(raw_date)
        if appointment_date.isoformat() != raw_date:
            raise ValueError
    except ValueError as exc:
        raise ValueError("Bitte ein gültiges Datum angeben.") from exc

    raw_time = payload.get("time")
    has_time = raw_time is not None and raw_time != ""
    appointment_time = time.min
    if has_time:
        try:
            if not isinstance(raw_time, str) or len(raw_time) != 5:
                raise ValueError
            appointment_time = time.fromisoformat(raw_time)
            if appointment_time.strftime("%H:%M") != raw_time:
                raise ValueError
        except ValueError as exc:
            raise ValueError("Bitte eine gültige Uhrzeit angeben.") from exc

    comment = payload.get("comment")
    if comment is not None and not isinstance(comment, str):
        raise ValueError("Die Notiz ist ungültig.")
    comment = (comment or "").strip()
    if len(comment) > 2000:
        raise ValueError("Die Notiz darf höchstens 2000 Zeichen enthalten.")

    appointment_zone = appointment_timezone()
    local_start = datetime.combine(
        appointment_date, appointment_time, tzinfo=appointment_zone
    )
    appointment_from = local_start.astimezone(timezone.utc)
    if has_time and appointment_from.astimezone(appointment_zone) != local_start:
        raise ValueError("Diese Uhrzeit existiert wegen der Zeitumstellung nicht.")

    return appointment_from, has_time, comment or None


def serialize_accepted_appointment_from(
    commission: Commission,
) -> str | None:
    appointment_from = commission.accepted_appointment_from
    if appointment_from is None:
        return None
    if appointment_from.tzinfo is None and commission.accepted_appointment_is_manual:
        appointment_from = appointment_from.replace(tzinfo=timezone.utc)
    if commission.accepted_appointment_has_time:
        return appointment_from.isoformat()
    if appointment_from.tzinfo is None:
        appointment_from = appointment_from.replace(tzinfo=timezone.utc)
    return appointment_from.astimezone(appointment_timezone()).date().isoformat()


def format_manual_appointment(commission: Commission) -> str | None:
    if not commission.accepted_appointment_is_manual:
        return None
    value = serialize_accepted_appointment_from(commission)
    if value is None:
        return None
    if not commission.accepted_appointment_has_time:
        return date.fromisoformat(value).strftime("%d.%m.%Y")
    return datetime.fromisoformat(value).astimezone(appointment_timezone()).strftime(
        "%d.%m.%Y %H:%M Uhr"
    )


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


def appointment_proposal_context(proposal: CommissionAppointmentProposal) -> dict:
    return {
        "date": (
            f"{GERMAN_WEEKDAYS[proposal.proposed_date.weekday()]}, "
            f"{proposal.proposed_date.strftime('%d.%m.%Y')}"
        ),
        "start": proposal.start_time.strftime("%H:%M"),
        "end": proposal.end_time.strftime("%H:%M") if proposal.end_time else "",
        "window_name": proposal.window_name or "",
        "custom": proposal.window_id is None,
    }


def appointment_email_context(appointment_request: CommissionAppointmentRequest) -> dict:
    commission = appointment_request.commission
    craftsman = commission.user or appointment_request.created_by
    context = commission_email_context(commission)
    context.update({
        "recipient_name": appointment_request.recipient_name or "",
        "craftsman_name": craftsman.display_name if craftsman else "",
        "topic": (
            commission.title or commission.display_title or ""
        ) if settings.get(SettingKeys.APPOINTMENT_EMAIL_INCLUDE_TOPIC) else "",
        "message": appointment_request.message or "",
        "tenant_comment": appointment_request.tenant_comment or "",
        "proposals": [
            appointment_proposal_context(proposal)
            for proposal in appointment_request.proposals
        ],
    })
    return context


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

    context = appointment_email_context(appointment_request)
    context["public_url"] = public_url
    message = EmailMessage()
    message["From"] = smtp_sender
    message["To"] = appointment_request.recipient_email
    apply_email_template(message, "appointment_request", context)

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
    context = appointment_email_context(appointment_request)
    context.update({
        "appointment": appointment_proposal_context(accepted_proposal),
        "custom_appointment": accepted_proposal.window_id is None,
        "calendar_duration_minutes": max(
            int((appointment_to - appointment_from).total_seconds() // 60), 1
        ),
    })
    message = EmailMessage()
    message["From"] = smtp_sender
    message["To"] = normalize_email_address(appointment_request.recipient_email)
    apply_email_template(message, "appointment_confirmation", context)
    calendar_content = render_email_template("appointment_calendar", context)

    calendar = Calendar()
    calendar.add("prodid", "-//MachMa//Terminvereinbarung//DE")
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
    event.add("summary", calendar_content["subject"])
    event.add("description", calendar_content["body"])
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

    if appointment_request.status == AppointmentRequestStatus.ACCEPTED.value:
        if accepted_proposal is None:
            raise RuntimeError("Der bestätigte Terminvorschlag fehlt.")
        template_key = "appointment_accepted"
    elif appointment_request.status == AppointmentRequestStatus.REJECTED.value:
        template_key = "appointment_rejected"
    else:
        raise RuntimeError("Der Terminstatus kann nicht benachrichtigt werden.")
    context = appointment_email_context(appointment_request)
    context["recipient_name"] = assigned_user.display_name or ""
    if accepted_proposal is not None:
        context["appointment"] = appointment_proposal_context(accepted_proposal)
    message = EmailMessage()
    message["From"] = smtp_sender
    message["To"] = recipient_email
    apply_email_template(message, template_key, context)

    send_email(
        message,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
    )
