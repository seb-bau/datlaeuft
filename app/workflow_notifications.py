from __future__ import annotations

from datetime import timedelta
from email.message import EmailMessage
from hashlib import sha256
import json
import smtplib

from flask import current_app
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .extensions import db
from .models import (
    Commission, CommissionLocalStatus, CommissionSyncState, ExternalCraftsman,
    PushEventType, TicketMirror, User, WorkflowNotification, WowiResponsibleOfficial, utc_now,
)
from .settings import SettingKeys, settings, validate_debug_email_address
from .email_templates import commission_email_context, render_email_template, set_email_content
from .mail_service import send_email
from .utils import ensure_utc, format_local_datetime


ACTIVE_COMMISSION_STATUSES = (
    CommissionLocalStatus.OPEN.value, CommissionLocalStatus.WAITING_FOR_ACCEPTANCE.value,
    CommissionLocalStatus.IN_PROGRESS.value,
)


def overdue_commission_filter():
    return db.and_(
        Commission.deleted_at.is_(None), Commission.is_retrospective.is_(False),
        Commission.erp_id.is_not(None), Commission.execution_to.is_not(None),
        Commission.execution_to < utc_now(), Commission.completed_at.is_(None),
        Commission.local_status.in_(ACTIVE_COMMISSION_STATUSES),
        db.or_(Commission.status_id.is_(None), Commission.status_id.notin_((9, 10, 11))),
    )


def commission_is_overdue(commission):
    return bool(
        not commission.is_retrospective and commission.deleted_at is None
        and commission.erp_id is not None and commission.execution_to is not None
        and ensure_utc(commission.execution_to) < utc_now() and commission.completed_at is None
        and commission.local_status in ACTIVE_COMMISSION_STATUSES
        and commission.status_id not in (9, 10, 11)
    )


def enqueue_notification(key, kind, *, commission=None, ticket=None, payload=None):
    existing = WorkflowNotification.query.filter_by(notification_key=key).one_or_none()
    if existing is not None:
        return existing
    try:
        with db.session.begin_nested():
            item = WorkflowNotification(
                notification_key=key, kind=kind, commission=commission, ticket=ticket, payload=payload or {},
            )
            db.session.add(item)
            db.session.flush()
    except IntegrityError:
        return WorkflowNotification.query.filter_by(notification_key=key).with_for_update().one()
    return item


def enqueue_priority_notification(commission, event_type):
    if commission.approval_state == "pending" or commission.is_retrospective or not commission.is_high_priority or commission.deleted_at is not None:
        return None
    if commission.id is None:
        db.session.flush()
    if event_type == PushEventType.NEW_COMMISSION:
        signature = "new"
    else:
        signature = sha256(json.dumps({
            "signature": (commission.raw_payload or {}).get("sync_signature"),
            "priority": commission.is_high_priority,
            "title": commission.display_title,
            "positions": [position.to_erp_payload() for position in commission.positions],
        }, sort_keys=True, default=str).encode()).hexdigest()
    key = f"priority:{commission.id}:{commission.craftsman_erp_id}:{event_type.value}:{signature}"
    return enqueue_notification(key, "priority", commission=commission, payload={
        "craftsman_erp_id": commission.craftsman_erp_id, "event": event_type.value,
    })


def ensure_overdue_notifications(limit=200):
    commissions = Commission.query.filter(overdue_commission_filter(), db.or_(
        Commission.overdue_notification_deadline.is_(None), Commission.overdue_notification_deadline != Commission.execution_to,
    )).order_by(Commission.id).limit(limit).with_for_update(skip_locked=True).all()
    created = 0
    for commission in commissions:
        deadline = ensure_utc(commission.execution_to).isoformat()
        office = office_user(commission)
        craftsman_email, _ = craftsman_contact(commission)
        same_recipient = office is not None and office.email and craftsman_email and office.email.strip().casefold() == craftsman_email.strip().casefold()
        for role in (("office",) if same_recipient else ("office", "craftsman")):
            identity = commission.craftsman_erp_id if role == "craftsman" else commission.created_by_user_id
            key = f"overdue:{commission.id}:{deadline}:{role}:{identity}"
            if WorkflowNotification.query.filter_by(notification_key=key).first() is not None:
                continue
            enqueue_notification(key, "overdue", commission=commission, payload={
                "deadline": deadline, "role": role, "craftsman_erp_id": commission.craftsman_erp_id,
            })
            created += 1
        commission.overdue_notification_deadline = commission.execution_to
    return created


def office_user(commission):
    if commission.created_by is not None:
        return commission.created_by
    if commission.local_official is not None:
        return commission.local_official
    official = db.session.get(WowiResponsibleOfficial, commission.responsible_official_repair_erp_id) if commission.responsible_official_repair_erp_id else None
    if official is not None and official.person_erp_id is not None:
        return User.query.filter_by(person_erp_id=official.person_erp_id, is_active_flag=True).first()
    return None


def craftsman_contact(commission):
    if commission.user is not None and commission.user.is_active:
        return commission.user.email, commission.user.display_name
    external = ExternalCraftsman.query.filter_by(craftsman_erp_id=commission.craftsman_erp_id).one_or_none()
    if external is not None and external.wowi_active:
        return external.email, external.display_name
    return "", ""


def notification_content(item):
    if item.kind == "appointment_calendar":
        from .appointment_calendar import appointment_calendar_content
        return appointment_calendar_content(item)
    payload = dict(item.payload or {})
    if item.kind == "ticket_reply":
        content = render_email_template("ticket_declined", {
            "ticket_number": item.ticket.id_num or str(item.ticket.erp_id),
            "ticket_subject": item.ticket.subject or "",
            "response_text": payload["response_text"],
        })
        if item.ticket.id_num:
            tag = f"(Ticket {item.ticket.id_num})"
            if tag not in content["subject"]:
                content["subject"] += " " + tag
        delimiter = settings.get(SettingKeys.TENANT_TICKET_REPLY_DELIMITER).strip()
        if delimiter:
            if any(ord(character) < 32 or ord(character) == 127 for character in delimiter):
                raise RuntimeError("Die Antwort-Trennzeile darf keine Zeilenumbrüche oder Steuerzeichen enthalten.")
            content["body"] = delimiter + "\n\n" + content["body"]
        return payload["recipient"], content
    commission = item.commission
    if item.kind == "approval_request":
        approver = db.session.get(User, payload["approver_user_id"])
        recipient = validate_debug_email_address(approver.email if approver is not None and approver.is_active else "")
        if not recipient:
            raise RuntimeError("Für die Freigabe ist keine gültige Empfängeradresse hinterlegt.")
        context = commission_email_context(commission)
        context["commission_number"] = f"#{commission.id}"
        return recipient, render_email_template("approval_request", context)
    if item.kind == "overdue" and payload["role"] == "office":
        recipient = office_user(commission)
        email = recipient.email if recipient is not None and recipient.is_active else ""
    else:
        email, _ = craftsman_contact(commission)
    recipient = validate_debug_email_address(email or "")
    if not recipient:
        raise RuntimeError("Für die Benachrichtigung ist keine gültige Empfängeradresse hinterlegt.")
    context = commission_email_context(commission)
    context.update({
        "execution_from": format_local_datetime(commission.execution_from),
        "execution_to": format_local_datetime(commission.execution_to),
        "craftsman_name": craftsman_contact(commission)[1],
    })
    return recipient, render_email_template("overdue" if item.kind == "overdue" else "priority", context)


def notification_is_current(item):
    if item.kind == "appointment_calendar":
        from .appointment_calendar import appointment_calendar_is_current
        return appointment_calendar_is_current(item)
    if item.kind == "ticket_reply":
        return item.ticket is not None and item.ticket.deleted_at is None
    commission = item.commission
    if item.kind == "approval_request":
        approver = db.session.get(User, item.payload.get("approver_user_id"))
        return bool(
            commission is not None and commission.approval_state == "pending" and commission.deleted_at is None
            and commission.approval_approver_user_id == item.payload.get("approver_user_id")
            and approver is not None and approver.is_active and approver.can_review_work
        )
    if commission is not None and commission.approval_state == "pending":
        return False
    if commission is None or commission.is_retrospective or commission.deleted_at is not None:
        return False
    if item.kind == "overdue":
        return (commission_is_overdue(commission) and ensure_utc(commission.execution_to).isoformat() == item.payload["deadline"]
                and (item.payload["role"] != "craftsman" or commission.craftsman_erp_id == item.payload.get("craftsman_erp_id")))
    return (commission.is_high_priority and commission.craftsman_erp_id == item.payload.get("craftsman_erp_id")
            and commission.local_status in (*ACTIVE_COMMISSION_STATUSES, CommissionLocalStatus.RECORDED.value))


def document_ticket_reply(item):
    from .ticket_service import fetch_ticket_row, value
    from .wowiport import WowiportService
    from .tenant_notifications import read_value

    service = WowiportService()
    row = fetch_ticket_row(item.ticket.erp_id, service)
    marker = f"[datlaeuft-ticket-reply:{item.id}]"
    comments = value(row, "Comments") or []
    existing = next((comment for comment in comments if value(comment, "CommentFromApi") is True and marker in str(value(comment, "Content") or "")), None)
    if existing is not None:
        comment_id = value(existing, "Id")
    else:
        source = value(row, "Source") or {}
        source_id = value(source, "Id")
        if type(source_id) is not int or source_id <= 0:
            raise RuntimeError("Die Ticketquelle konnte nicht ermittelt werden.")
        content = (
            "Per E-Mail versendete Antwort: Kein Auftrag\n"
            f"An: {item.payload.get('delivery_recipient', item.payload['recipient'])}\n"
            f"Betreff: {item.payload['subject']}\n\n{item.payload['body']}\n\n{marker}"
        )
        result = service.client.create_ticket_comment(ticket_id=item.ticket.erp_id, content=content, ticket_source_id=source_id)
        if result.status_code not in (200, 201):
            raise RuntimeError("Die Ticketantwort konnte in Wowiport nicht gespeichert werden.")
        data = result.data[0] if isinstance(result.data, list) and len(result.data) == 1 else result.data
        comment_id = read_value(data, "comment_id")
    if type(comment_id) is not int or comment_id <= 0:
        raise RuntimeError("Wowiport hat keine gültige Kommentar-ID zurückgegeben.")
    item.payload = {**item.payload, "comment_id": comment_id}
    db.session.commit()


def claim_workflow_notification():
    item = WorkflowNotification.query.filter(
        WorkflowNotification.state.in_(("pending", "failed")), WorkflowNotification.available_at <= utc_now(),
    ).order_by(WorkflowNotification.available_at, WorkflowNotification.id).with_for_update(skip_locked=True).first()
    if item is None:
        db.session.rollback()
        return None
    item_id = item.id
    updated = WorkflowNotification.query.filter(WorkflowNotification.id == item_id, WorkflowNotification.state.in_(("pending", "failed"))).update({
        WorkflowNotification.state: "sending", WorkflowNotification.locked_at: utc_now(),
        WorkflowNotification.attempt_count: WorkflowNotification.attempt_count + 1,
    }, synchronize_session=False)
    if updated != 1:
        db.session.rollback()
        return None
    db.session.commit()
    return item_id


def execute_workflow_notification(item_id):
    item = db.session.get(WorkflowNotification, item_id)
    if item is None or item.state != "sending":
        return
    if not notification_is_current(item):
        item.state = "discarded"
        item.locked_at = None
        db.session.commit()
        return
    if item.kind != "approval_request" and item.commission is not None and (item.commission.erp_sync_state != CommissionSyncState.SYNCED.value or not item.commission.erp_idnum):
        item.state = "pending"
        item.locked_at = None
        item.last_error = None
        delay_seconds = min(60 * 2 ** min(max(item.attempt_count - 1, 0), 6), 3600)
        item.available_at = utc_now() + timedelta(seconds=delay_seconds)
        db.session.commit()
        next_attempt_at = ensure_utc(item.available_at).isoformat()
        current_app.logger.warning(
            "workflow_notification_waiting_for_erp id=%s kind=%s state=%s commission_id=%s ticket_id=%s "
            "attempt=%s erp_sync_state=%s erp_id=%s erp_idnum=%s next_attempt_at=%s "
            "reason=Der Auftrag ist noch nicht vollständig mit Wowiport synchronisiert.",
            item.id, item.kind, item.state, item.commission_id, item.ticket_id, item.attempt_count,
            item.commission.erp_sync_state, item.commission.erp_id, item.commission.erp_idnum, next_attempt_at,
            extra={"notification_id": item.id, "notification_kind": item.kind, "notification_state": item.state,
                   "commission_id": item.commission_id, "ticket_id": item.ticket_id, "attempt_count": item.attempt_count,
                   "erp_sync_state": item.commission.erp_sync_state, "erp_id": item.commission.erp_id,
                   "erp_idnum": item.commission.erp_idnum, "erp_sync_message": item.commission.erp_sync_message,
                   "next_attempt_at": next_attempt_at},
        )
        return
    if not item.payload.get("mail_sent"):
        if "subject" not in item.payload:
            recipient, content = notification_content(item)
            item.payload = {**item.payload, "recipient": recipient, **content}
            db.session.commit()
        host = settings.get(SettingKeys.SMTP_HOST).strip()
        sender = settings.get(SettingKeys.SMTP_SENDER).strip()
        if not host or not sender:
            raise RuntimeError("SMTP-Server oder Absenderadresse fehlen.")
        message = EmailMessage()
        message["From"] = sender
        message["To"] = item.payload["recipient"]
        if item.kind == "ticket_reply":
            reply_to = settings.get(SettingKeys.TENANT_TICKET_REPLY_TO).strip()
            if reply_to:
                message["Reply-To"] = validate_debug_email_address(reply_to)
        set_email_content(message, item.payload["subject"], item.payload["body"])
        if item.kind == "appointment_calendar":
            from .appointment_calendar import attach_appointment_calendar
            attach_appointment_calendar(message, item)
        item.payload = {**item.payload, "submission_started": True}
        db.session.commit()
        if not notification_is_current(item):
            item.state = "discarded"
            item.locked_at = None
            db.session.commit()
            return
        try:
            debug_recipient = send_email(message, smtp_host=host, smtp_port=settings.get(SettingKeys.SMTP_PORT))
        except RuntimeError as exc:
            if isinstance(exc.__cause__, (SQLAlchemyError, ValueError, smtplib.SMTPConnectError, smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError, ConnectionRefusedError)):
                item.payload = {**item.payload, "submission_started": False}
                db.session.commit()
            raise
        item.payload = {**item.payload, "mail_sent": True, "submission_started": False, "delivery_recipient": debug_recipient or item.payload["recipient"]}
        item.sent_at = utc_now()
        db.session.commit()
    if item.kind == "ticket_reply" and not item.payload.get("comment_id"):
        document_ticket_reply(item)
    item.state = "sent"
    item.locked_at = None
    item.last_error = None
    db.session.commit()


def process_workflow_notification_batch(limit=20):
    now = utc_now()
    for item in WorkflowNotification.query.filter(
        WorkflowNotification.state == "sending", WorkflowNotification.locked_at < now - timedelta(minutes=15),
    ).with_for_update(skip_locked=True).all():
        item.state = "uncertain" if item.payload.get("submission_started") and not item.payload.get("mail_sent") else "failed"
        item.last_error = "Ein vorheriger Versand wurde unterbrochen."
        item.locked_at = None
        item.available_at = now
    try:
        ensure_overdue_notifications()
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
    processed = 0
    while processed < limit:
        item_id = claim_workflow_notification()
        if item_id is None:
            break
        try:
            execute_workflow_notification(item_id)
        except Exception as exc:
            db.session.rollback()
            item = db.session.get(WorkflowNotification, item_id)
            if item is not None:
                item.state = "uncertain" if item.payload.get("submission_started") and not item.payload.get("mail_sent") else "failed"
                item.last_error = str(exc)[:4000]
                item.locked_at = None
                item.available_at = utc_now() + timedelta(seconds=min(60 * 2 ** min(item.attempt_count - 1, 6), 3600))
                db.session.commit()
                current_app.logger.exception(
                    "workflow_notification_failed id=%s kind=%s state=%s commission_id=%s ticket_id=%s attempt=%s error=%s",
                    item.id, item.kind, item.state, item.commission_id, item.ticket_id, item.attempt_count, exc,
                    extra={"notification_id": item.id, "notification_kind": item.kind, "notification_state": item.state,
                           "commission_id": item.commission_id, "ticket_id": item.ticket_id,
                           "attempt_count": item.attempt_count, "error_type": type(exc).__name__, "error": str(exc)},
                )
        processed += 1
    return processed

