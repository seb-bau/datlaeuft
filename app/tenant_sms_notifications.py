from __future__ import annotations

from datetime import timedelta
from hashlib import sha256
from typing import Any

from flask import current_app

from .extensions import db
from .models import Commission, CommissionSyncState, CommissionTenantSmsNotification, utc_now
from .settings import SMS_DEBUG_SUPPRESSED_COMMISSION_ID, SettingKeys, settings
from .sms_service import (
    SmsDeliveryUncertain, SmsDisabled, SmsSendError, SmsSuppressed,
    normalize_phone_number, send_sms, sms_is_suppressed,
)
from .tenant_notifications import current_tenant_contractors, read_value, tenant_notification_context


TERMINAL_RECIPIENT_STATES = {"sent", "skipped", "simulated", "uncertain"}
PHONE_TYPES = {"handynummer", "mobil", "mobiltelefon", "mobilfunk", "mobile", "festnetz", "telefon"}


def discard_debug_tenant_sms_notifications() -> None:
    query = CommissionTenantSmsNotification.query.filter(
        CommissionTenantSmsNotification.state.in_(("pending", "failed", "sending")),
    )
    if not settings.get(SettingKeys.SMTP_DEBUG_RECIPIENT).strip():
        commission_id = settings.get(SMS_DEBUG_SUPPRESSED_COMMISSION_ID)
        if not commission_id:
            return
        query = query.filter(CommissionTenantSmsNotification.commission_id <= commission_id)
    query.update({
        CommissionTenantSmsNotification.state: "discarded",
        CommissionTenantSmsNotification.locked_at: None,
        CommissionTenantSmsNotification.last_error: "SMS wegen gesetzter Debug-E-Mail-Adresse dauerhaft verworfen.",
    }, synchronize_session="fetch")
    db.session.commit()


def tenant_phone_numbers(commission: Commission, service: Any) -> list[str]:
    numbers = set()
    for contractor in current_tenant_contractors(commission, service):
        person = read_value(contractor, "person")
        communications = list(read_value(person, "communications") or [])
        preferred = [read_value(person, name) for name in ("first_mobile_phone_communication", "first_phone_communication")]
        communications.extend(item for item in preferred if item is not None)
        for communication in communications:
            communication_type = read_value(communication, "communication_type")
            type_id = read_value(communication_type, "id_") or read_value(communication, "communication_type_id")
            type_name = communication_type if isinstance(communication_type, str) else read_value(communication_type, "name")
            if (communication not in preferred and str(type_id) not in {"1", "3"}
                    and str(type_name or "").strip().casefold() not in PHONE_TYPES):
                continue
            number = normalize_phone_number(read_value(communication, "content"))
            if number:
                numbers.add(number)
    return sorted(numbers)


def tenant_sms_text(commission: Commission, service: Any) -> str:
    context = tenant_notification_context(commission, service)
    return (
        "Für Ihr Mietverhältnis wurde eine Beauftragung erteilt.\n"
        f"Gewerk: {'; '.join(context['craft_names'])}\n"
        f"Beauftragt: {context['craftsman_name']}\n"
        f"Auftrag: {context['commission_number']}"
    )


def claim_tenant_sms_notification() -> int | None:
    item = CommissionTenantSmsNotification.query.filter(
        CommissionTenantSmsNotification.state.in_(("pending", "failed")),
        CommissionTenantSmsNotification.available_at <= utc_now(),
    ).order_by(
        CommissionTenantSmsNotification.available_at.asc(), CommissionTenantSmsNotification.id.asc(),
    ).with_for_update(skip_locked=True).first()
    if item is None:
        db.session.rollback()
        return None
    item_id = item.id
    updated = CommissionTenantSmsNotification.query.filter(
        CommissionTenantSmsNotification.id == item_id,
        CommissionTenantSmsNotification.state.in_(("pending", "failed")),
    ).update({
        CommissionTenantSmsNotification.state: "sending",
        CommissionTenantSmsNotification.locked_at: utc_now(),
        CommissionTenantSmsNotification.attempt_count: CommissionTenantSmsNotification.attempt_count + 1,
    }, synchronize_session=False)
    if updated != 1:
        db.session.rollback()
        return None
    db.session.commit()
    return item_id


def recover_stale_tenant_sms_notifications() -> None:
    now = utc_now()
    items = CommissionTenantSmsNotification.query.filter(
        CommissionTenantSmsNotification.state == "sending",
        CommissionTenantSmsNotification.locked_at < now - timedelta(minutes=15),
    ).with_for_update(skip_locked=True).all()
    for item in items:
        payload = dict(item.payload or {})
        number = payload.get("submitting")
        if number:
            results = dict(payload.get("results", {}))
            results[number] = {"state": "uncertain"}
            payload = {**payload, "results": results, "submitting": None}
            item.payload = payload
        pending = [number for number in payload.get("recipients", [])
                   if payload.get("results", {}).get(number, {}).get("state") not in TERMINAL_RECIPIENT_STATES]
        item.state = "uncertain" if number and not pending else "failed"
        item.last_error = "Ein vorheriger SMS-Versand wurde unterbrochen. Eine unklare Übergabe wird nicht automatisch wiederholt."
        item.available_at = now
        item.locked_at = None
    db.session.commit()


def finish_tenant_sms_notification(item: CommissionTenantSmsNotification) -> None:
    results = (item.payload or {}).get("results", {})
    states = {value.get("state") for value in results.values()}
    if "failed" in states:
        errors = list(dict.fromkeys(value.get("error", "SMS-Versand fehlgeschlagen.")
                                   for value in results.values() if value.get("state") == "failed"))
        raise SmsSendError("; ".join(errors))
    if "uncertain" in states:
        item.state = "uncertain"
        item.last_error = "Mindestens eine SMS-Übergabe ist unklar. Bitte vor einem erneuten Versand im seven-Konto prüfen."
        current_app.logger.warning("tenant_sms_delivery_uncertain notification_id=%s commission_id=%s", item.id, item.commission_id)
    else:
        item.state = "sent" if "sent" in states else ("simulated" if "simulated" in states else "no_recipients")
        item.last_error = None
    item.locked_at = None
    db.session.commit()


def execute_tenant_sms_notification(item_id: int) -> None:
    item = db.session.get(CommissionTenantSmsNotification, item_id)
    if item is None or item.state != "sending":
        return
    if sms_is_suppressed(item.commission_id):
        item.state = "discarded"
        item.locked_at = None
        item.last_error = "SMS wegen gesetzter Debug-E-Mail-Adresse dauerhaft verworfen."
        db.session.commit()
        return
    if not settings.get(SettingKeys.SMS_ENABLED):
        item.state = "pending"
        item.available_at = utc_now() + timedelta(seconds=60)
        item.locked_at = None
        db.session.commit()
        return
    commission = item.commission
    if (commission.approval_state == "pending" or commission.is_retrospective or not commission.notify_tenants or commission.license_agreement_id is None
            or commission.deleted_at is not None or commission.status_id not in (7, 12)):
        item.state = "discarded"
        item.locked_at = None
        db.session.commit()
        return
    if commission.erp_sync_state != CommissionSyncState.SYNCED.value:
        raise SmsSendError("Die Übertragung des Auftrags an Wowiport ist noch nicht abgeschlossen.")
    pending = [number for number in (item.payload or {}).get("recipients", [])
               if (item.payload or {}).get("results", {}).get(number, {}).get("state") not in TERMINAL_RECIPIENT_STATES]
    if item.payload is not None and not pending:
        finish_tenant_sms_notification(item)
        return

    from .wowiport import WowiportService

    service = WowiportService()
    eligible = set(tenant_phone_numbers(commission, service))
    if item.payload is None:
        if not eligible:
            item.state = "no_recipients"
            item.locked_at = None
            item.last_error = None
            db.session.commit()
            return
        item.payload = {"recipients": sorted(eligible), "text": tenant_sms_text(commission, service), "results": {}, "submitting": None}
        db.session.commit()
        pending = list(item.payload["recipients"])

    for number in pending:
        if number not in eligible:
            result = {"state": "skipped"}
        else:
            def before_send():
                db.session.commit()
                if sms_is_suppressed(item.commission_id):
                    raise SmsSuppressed("SMS wegen gesetzter Debug-E-Mail-Adresse dauerhaft verworfen.")
                item.payload = {**item.payload, "submitting": number}
                item.locked_at = utc_now()
                db.session.commit()

            try:
                outcome = send_sms(
                    number, item.payload["text"],
                    foreign_id=f"datlaeuft-{item.id}-{sha256(number.encode()).hexdigest()[:16]}",
                    before_send=before_send,
                )
                result = {"state": outcome.state, "message_id": outcome.message_id}
            except SmsSuppressed:
                item.payload = {**item.payload, "submitting": None}
                item.state = "discarded"
                item.last_error = "SMS wegen gesetzter Debug-E-Mail-Adresse dauerhaft verworfen."
                item.locked_at = None
                db.session.commit()
                return
            except SmsDisabled:
                item.payload = {**item.payload, "submitting": None}
                item.state = "pending"
                item.available_at = utc_now() + timedelta(seconds=60)
                item.locked_at = None
                db.session.commit()
                return
            except SmsDeliveryUncertain as exc:
                result = {"state": "uncertain", "error": str(exc)}
            except SmsSendError as exc:
                result = {"state": "failed", "error": str(exc)}
        results = {**item.payload["results"], number: result}
        item.payload = {**item.payload, "results": results, "submitting": None}
        item.locked_at = utc_now()
        if result["state"] == "sent" and item.sent_at is None:
            item.sent_at = item.locked_at
        db.session.commit()
    finish_tenant_sms_notification(item)


def fail_tenant_sms_notification(item_id: int, error: Exception) -> None:
    db.session.rollback()
    item = db.session.get(CommissionTenantSmsNotification, item_id)
    if item is None:
        return
    if sms_is_suppressed(item.commission_id) or item.state == "discarded":
        item.state = "discarded"
        item.last_error = "SMS wegen gesetzter Debug-E-Mail-Adresse dauerhaft verworfen."
        item.locked_at = None
        db.session.commit()
        return
    if (item.payload or {}).get("submitting"):
        payload = dict(item.payload)
        number = payload["submitting"]
        results = {**payload.get("results", {}), number: {"state": "uncertain"}}
        item.payload = {**payload, "results": results, "submitting": None}
        pending = [recipient for recipient in payload.get("recipients", [])
                   if results.get(recipient, {}).get("state") not in TERMINAL_RECIPIENT_STATES]
        item.state = "failed" if pending else "uncertain"
        item.last_error = "Die SMS-Übergabe ist unklar. Bitte den Versand im seven-Konto prüfen."
    else:
        item.state = "failed"
        item.last_error = str(error)[:4000] if isinstance(error, SmsSendError) else "Die SMS-Verarbeitung ist fehlgeschlagen."
    item.available_at = utc_now() + timedelta(seconds=min(60 * (2 ** min(max(item.attempt_count - 1, 0), 6)), 3600))
    item.locked_at = None
    db.session.commit()
    current_app.logger.error("tenant_sms_notification_failed notification_id=%s commission_id=%s state=%s", item.id, item.commission_id, item.state)


def process_tenant_sms_notification_batch(limit: int = 20) -> int:
    discard_debug_tenant_sms_notifications()
    if sms_is_suppressed():
        return 0
    if not settings.get(SettingKeys.SMS_ENABLED):
        return 0
    recover_stale_tenant_sms_notifications()
    processed = 0
    while processed < limit and settings.get(SettingKeys.SMS_ENABLED):
        item_id = claim_tenant_sms_notification()
        if item_id is None:
            break
        try:
            execute_tenant_sms_notification(item_id)
        except Exception as exc:
            fail_tenant_sms_notification(item_id, exc)
        processed += 1
    return processed
