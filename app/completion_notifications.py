from __future__ import annotations

from datetime import timedelta
from email.message import EmailMessage
from typing import Any

from flask import current_app
from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError

from .extensions import db
from .mail_service import send_email
from .models import (
    Commission, CommissionCompletionNotification, CommissionSource,
    CommissionSyncState, utc_now,
)
from .settings import SettingKeys, settings, validate_debug_email_address


def resolve_completion_notification_choice(source: CommissionSource, value: Any) -> bool:
    if source != CommissionSource.WEB or value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().casefold() in {"true", "false"}:
        return value.strip().casefold() == "true"
    raise ValueError("Die Auswahl zur Abschlussbenachrichtigung ist ungültig.")


def completion_is_reported(commission: Commission) -> bool:
    return commission.status_id == 9 or (
        commission.status_id == 11 and commission.completion_successful is True
    )


def ensure_completion_notifications(limit: int = 200) -> int:
    commissions = (
        Commission.query.outerjoin(
            CommissionCompletionNotification,
            CommissionCompletionNotification.commission_id == Commission.id,
        )
        .filter(
            Commission.source == CommissionSource.WEB.value,
            Commission.notify_on_completion.is_(True),
            Commission.erp_id.is_not(None),
            Commission.erp_sync_state == CommissionSyncState.SYNCED.value,
            Commission.deleted_at.is_(None),
            or_(Commission.status_id == 9, and_(
                Commission.status_id == 11,
                Commission.completion_successful.is_(True),
            )),
            CommissionCompletionNotification.id.is_(None),
        )
        .order_by(Commission.id.asc()).limit(limit).all()
    )
    for commission in commissions:
        db.session.add(CommissionCompletionNotification(commission=commission))
    return len(commissions)


def claim_completion_notification() -> int | None:
    item = (
        CommissionCompletionNotification.query.filter(
            CommissionCompletionNotification.state.in_(("pending", "failed")),
            CommissionCompletionNotification.available_at <= utc_now(),
        )
        .order_by(CommissionCompletionNotification.available_at.asc(), CommissionCompletionNotification.id.asc())
        .with_for_update(skip_locked=True).first()
    )
    if item is None:
        db.session.rollback()
        return None
    item_id = item.id
    updated = CommissionCompletionNotification.query.filter(
        CommissionCompletionNotification.id == item_id,
        CommissionCompletionNotification.state.in_(("pending", "failed")),
    ).update({
        CommissionCompletionNotification.state: "sending",
        CommissionCompletionNotification.locked_at: utc_now(),
        CommissionCompletionNotification.attempt_count: CommissionCompletionNotification.attempt_count + 1,
    }, synchronize_session=False)
    if updated != 1:
        db.session.rollback()
        return None
    db.session.commit()
    return item_id


def completion_notification_payload(commission: Commission) -> dict[str, str]:
    creator = commission.created_by
    try:
        recipient = validate_debug_email_address(creator.email if creator is not None else "")
    except ValueError as exc:
        raise RuntimeError("Für den anlegenden Sachbearbeiter ist keine gültige E-Mail-Adresse hinterlegt.") from exc
    if not recipient:
        raise RuntimeError("Für den anlegenden Sachbearbeiter ist keine E-Mail-Adresse hinterlegt.")
    reference = " ".join(str(commission.erp_idnum or commission.erp_id or commission.id).split())
    lines = [
        "Guten Tag,", "",
        f"der von Ihnen angelegte Auftrag {reference} wurde als abgeschlossen gemeldet.", "",
        f"Auftrag: {commission.display_title}",
    ]
    if commission.apartment_address:
        lines.append(f"Objekt: {commission.apartment_address}")
    if commission.status_id == 11:
        lines.extend(("", "Die Mieterbelastung befindet sich noch in Prüfung."))
    base_url = settings.get(SettingKeys.PUBLIC_BASE_URL).strip().rstrip("/")
    if base_url:
        lines.extend(("", f"{base_url}/admin/commissions/{commission.id}"))
    lines.extend(("", "Freundliche Grüße", ""))
    return {
        "recipient": recipient,
        "subject": f"Auftrag {reference} abgeschlossen",
        "body": "\n".join(lines),
    }


def execute_completion_notification(item_id: int) -> None:
    item = db.session.get(CommissionCompletionNotification, item_id)
    if item is None or item.state != "sending":
        return
    commission = item.commission
    if (
        commission.source != CommissionSource.WEB.value
        or not commission.notify_on_completion
        or commission.deleted_at is not None
        or not completion_is_reported(commission)
    ):
        item.state = "discarded"
        item.locked_at = None
        db.session.commit()
        return
    if commission.erp_sync_state != CommissionSyncState.SYNCED.value:
        raise RuntimeError("Der Abschluss wurde noch nicht vollständig an Wowiport übertragen.")
    if item.payload is None:
        item.payload = completion_notification_payload(commission)
        db.session.commit()
    smtp_host = settings.get(SettingKeys.SMTP_HOST).strip()
    smtp_sender = settings.get(SettingKeys.SMTP_SENDER).strip()
    if not smtp_host or not smtp_sender:
        raise RuntimeError("SMTP-Server oder Absenderadresse fehlen.")
    message = EmailMessage()
    message["From"] = smtp_sender
    message["To"] = item.payload["recipient"]
    message["Subject"] = item.payload["subject"]
    message.set_content(item.payload["body"])
    send_email(message, smtp_host=smtp_host, smtp_port=settings.get(SettingKeys.SMTP_PORT))
    item.state = "sent"
    item.sent_at = utc_now()
    item.locked_at = None
    item.last_error = None
    db.session.commit()


def fail_completion_notification(item_id: int, error: Exception) -> None:
    db.session.rollback()
    item = db.session.get(CommissionCompletionNotification, item_id)
    if item is None:
        return
    item.state = "failed"
    item.available_at = utc_now() + timedelta(
        seconds=min(60 * (2 ** min(max(item.attempt_count - 1, 0), 6)), 3600)
    )
    item.locked_at = None
    item.last_error = str(error)[:4000]
    db.session.commit()
    current_app.logger.exception("completion_notification_failed commission_id=%s", item.commission_id)


def process_completion_notification_batch(limit: int = 20) -> int:
    now = utc_now()
    CommissionCompletionNotification.query.filter(
        CommissionCompletionNotification.state == "sending",
        CommissionCompletionNotification.locked_at < now - timedelta(minutes=15),
    ).update({
        CommissionCompletionNotification.state: "failed",
        CommissionCompletionNotification.available_at: now,
        CommissionCompletionNotification.locked_at: None,
    }, synchronize_session=False)
    try:
        ensure_completion_notifications()
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
    processed = 0
    while processed < limit:
        item_id = claim_completion_notification()
        if item_id is None:
            break
        try:
            execute_completion_notification(item_id)
        except Exception as exc:
            fail_completion_notification(item_id, exc)
        processed += 1
    return processed