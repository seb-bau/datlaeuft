from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from uuid import NAMESPACE_URL, uuid4, uuid5

from .appointment_service import appointment_timezone
from .email_templates import commission_email_context, render_email_template
from .extensions import db
from .models import WorkflowNotification, utc_now
from .settings import SettingKeys, settings, validate_debug_email_address


def appointment_calendar_values(commission):
    start = commission.accepted_appointment_from
    if start is None:
        return None
    zone = appointment_timezone()
    source_zone = timezone.utc if commission.accepted_appointment_is_manual else zone
    if start.tzinfo is None:
        start = start.replace(tzinfo=source_zone)
    end = commission.accepted_appointment_to
    if end is not None and end.tzinfo is None:
        end = end.replace(tzinfo=source_zone)
    return {
        "start": start.astimezone(timezone.utc).isoformat(),
        "end": end.astimezone(timezone.utc).isoformat() if end else None,
        "has_time": bool(commission.accepted_appointment_has_time),
        "manual": bool(commission.accepted_appointment_is_manual),
        "comment": commission.accepted_appointment_comment or "",
    }


def appointment_calendar_signature(commission):
    return sha256(json.dumps(appointment_calendar_values(commission), sort_keys=True).encode()).hexdigest()


def enqueue_appointment_calendar(commission, event_key=None):
    from .workflow_notifications import enqueue_notification

    user = commission.user
    event_key = event_key or str(uuid4())
    key = f"appointment-calendar:{commission.id}:{user.id if user else 0}:{event_key}"
    existing = WorkflowNotification.query.filter_by(notification_key=key).one_or_none()
    if existing is not None:
        return existing
    WorkflowNotification.query.filter(
        WorkflowNotification.commission_id == commission.id,
        WorkflowNotification.kind == "appointment_calendar",
        WorkflowNotification.state.in_(("pending", "failed")),
    ).update({WorkflowNotification.state: "discarded"}, synchronize_session="fetch")
    if (user is None or not user.is_active or not user.appointment_calendar_email_enabled
            or not commission.is_actionable or commission.is_retrospective
            or commission.accepted_appointment_from is None):
        return None
    signature = appointment_calendar_signature(commission)
    return enqueue_notification(
        key,
        "appointment_calendar", commission=commission,
        payload={"user_id": user.id, "appointment_signature": signature},
    )


def appointment_calendar_is_current(item):
    commission = item.commission
    user = commission.user if commission else None
    return bool(
        user is not None and user.is_active and user.appointment_calendar_email_enabled
        and user.id == item.payload.get("user_id") and commission.is_actionable
        and commission.deleted_at is None and not commission.is_retrospective
        and commission.accepted_appointment_from is not None
        and appointment_calendar_signature(commission) == item.payload.get("appointment_signature")
    )


def appointment_calendar_content(item):
    commission = item.commission
    recipient = validate_debug_email_address(commission.user.email or "")
    if not recipient:
        raise RuntimeError("Für die Kalendereinladung ist keine gültige E-Mail-Adresse hinterlegt.")
    context = commission_email_context(commission)
    values = appointment_calendar_values(commission)
    start = datetime.fromisoformat(values["start"]).astimezone(appointment_timezone())
    appointment_text = start.strftime("%d.%m.%Y %H:%M Uhr" if values["has_time"] else "%d.%m.%Y (ganztägig)")
    if values["end"] and values["has_time"]:
        appointment_text += " bis " + datetime.fromisoformat(values["end"]).astimezone(appointment_timezone()).strftime("%H:%M Uhr")
    context.update({
        "recipient_name": commission.user.display_name,
        "appointment_text": appointment_text,
        "appointment_comment": commission.accepted_appointment_comment or "",
    })
    return recipient, render_email_template("appointment_worker_calendar", context)


def attach_appointment_calendar(message, item):
    from icalendar import Calendar, Event

    commission = item.commission
    values = appointment_calendar_values(commission)
    start = commission.accepted_appointment_from
    source_zone = timezone.utc if values["manual"] else appointment_timezone()
    if start.tzinfo is None:
        start = start.replace(tzinfo=source_zone)
    calendar = Calendar()
    calendar.add("prodid", "-//WattNu//Termine//DE")
    calendar.add("version", "2.0")
    calendar.add("method", "REQUEST")
    event = Event()
    event.add("uid", str(uuid5(NAMESPACE_URL, f"wattnu:commission:{commission.public_id}:{item.payload['appointment_signature']}")))
    event.add("dtstamp", utc_now())
    if values["has_time"]:
        end = commission.accepted_appointment_to
        if end is None:
            end = start + timedelta(minutes=max(1, settings.get(SettingKeys.APPOINTMENT_CUSTOM_TIME_DURATION_MINUTES)))
        elif end.tzinfo is None:
            end = end.replace(tzinfo=source_zone)
        event.add("dtstart", start.astimezone(timezone.utc))
        event.add("dtend", end.astimezone(timezone.utc))
    else:
        day = start.astimezone(appointment_timezone()).date()
        event.add("dtstart", day)
        event.add("dtend", day + timedelta(days=1))
    event.add("summary", f"Auftrag {commission.erp_idnum} – {commission.display_title}")
    event.add("description", item.payload["body"])
    event.add("location", commission.apartment_address or "")
    event.add("organizer", "mailto:" + str(message["From"]))
    event.add("attendee", "mailto:" + item.payload["recipient"], parameters={"CN": commission.user.display_name, "RSVP": "FALSE"})
    event.add("status", "CONFIRMED")
    event.add("sequence", 0)
    calendar.add_component(event)
    message.add_attachment(calendar.to_ical(), maintype="text", subtype="calendar", filename="termin.ics", params={"charset": "utf-8", "method": "REQUEST"})
