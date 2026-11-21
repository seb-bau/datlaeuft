from __future__ import annotations

from datetime import timedelta
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5

from flask import current_app
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.exc import OperationalError

from .extensions import db
from .models import Commission, CommissionLocalStatus, ExternalCommissionAssignmentMail, ExternalCraftsman, User, WorkflowNotification, utc_now
from .settings import SettingKeys, settings
from .utils import ensure_utc


RESPONSE_LINK_MAX_AGE = 90 * 24 * 60 * 60


def portal_light_enabled(craftsman):
    return bool(craftsman.portal_light_enabled if craftsman.portal_light_enabled is not None
                else settings.get(SettingKeys.EXTERNAL_PORTAL_LIGHT_DEFAULT))


def automatic_external_mails_enabled(craftsman):
    return bool(craftsman.portal_enabled or portal_light_enabled(craftsman)
                or craftsman.reminder_first_days or craftsman.reminder_second_days or craftsman.cancel_unanswered_days)


def validate_external_communication(form):
    mode = form.get("portal_light_enabled", "default")
    if mode not in {"default", "enabled", "disabled"}:
        raise ValueError("Die Einstellung für Portal light ist ungültig.")
    values = {"portal_light_enabled": {"default": None, "enabled": True, "disabled": False}[mode]}
    for name in ("reminder_first_days", "reminder_second_days", "cancel_unanswered_days"):
        raw_value = str(form.get(name, "0")).strip()
        if not raw_value.isascii() or not raw_value.isdecimal() or len(raw_value) > 4:
            raise ValueError("Bitte die Fristen als ganze Tage zwischen 0 und 3.650 eingeben.")
        value = int(raw_value)
        if value > 3650:
            raise ValueError("Die Fristen dürfen höchstens 3.650 Tage betragen.")
        values[name] = value
    first, second, cancellation = (values[name] for name in (
        "reminder_first_days", "reminder_second_days", "cancel_unanswered_days",
    ))
    if second and second <= first:
        raise ValueError("Die zweite Erinnerung muss später als die erste Erinnerung erfolgen.")
    if cancellation and cancellation <= max(first, second):
        raise ValueError("Die Stornierung muss später als beide Erinnerungen erfolgen.")
    return values


def assignment_is_current(item):
    commission, craftsman = item.commission, item.external_craftsman
    return bool(commission is not None and craftsman is not None and craftsman.wowi_active and str(craftsman.email or "").strip()
                and commission.craftsman_erp_id == craftsman.craftsman_erp_id
                and commission.external_assignment_version == item.assignment_version
                and commission.deleted_at is None and not commission.is_retrospective
                and commission.approval_state != "pending" and commission.user_id is None
                and User.query.filter_by(craftsman_erp_id=craftsman.craftsman_erp_id, is_active_flag=True).first() is None)


def assignment_is_unanswered(item):
    commission = item.commission
    return bool(assignment_is_current(item) and item.response_at is None and item.auto_canceled_at is None
                and commission.local_status == CommissionLocalStatus.WAITING_FOR_ACCEPTANCE.value
                and commission.rejected_at is None and commission.completed_at is None
                and commission.status_id not in (9, 10, 11))


def response_serializer():
    if not current_app.secret_key or current_app.secret_key == "bootstrap":
        raise RuntimeError("Für Portal light muss ein eigener Anwendungsschlüssel eingerichtet sein.")
    return URLSafeTimedSerializer(current_app.secret_key, salt="external-order-response-v1")


def response_identity(item):
    return {"mail": item.id, "craftsman": item.external_craftsman_id, "version": item.assignment_version,
            "email": sha256(item.external_craftsman.email.strip().casefold().encode()).hexdigest()}


def response_url(item, decision=None):
    base_url = settings.get(SettingKeys.PUBLIC_BASE_URL).strip().rstrip("/")
    if not base_url:
        raise RuntimeError("Die öffentliche Basis-URL fehlt.")
    token = response_serializer().dumps(response_identity(item))
    url = f"{base_url}/auftragnehmer/antwort/{token}"
    return f"{url}?decision={decision}" if decision else url


def locked_assignment(mail_id):
    for attempt in range(3):
        try:
            item = db.session.get(ExternalCommissionAssignmentMail, mail_id)
            if item is not None:
                db.session.refresh(item.commission, with_for_update=True)
                db.session.refresh(item, with_for_update=True)
                db.session.refresh(item.external_craftsman, with_for_update=True)
            return item
        except OperationalError as exc:
            db.session.rollback()
            error_code = exc.orig.args[0] if exc.orig.args else None
            if error_code not in (1020, 1205, 1213) or attempt == 2:
                raise


def response_assignment(raw_token, *, lock=False):
    try:
        identity = response_serializer().loads(raw_token, max_age=RESPONSE_LINK_MAX_AGE)
    except BadSignature as exc:
        raise ValueError("Dieser Antwort-Link ist ungültig oder abgelaufen.") from exc
    if not isinstance(identity, dict) or type(identity.get("mail")) is not int:
        raise ValueError("Dieser Antwort-Link ist ungültig oder abgelaufen.")
    item = locked_assignment(identity["mail"]) if lock else db.session.get(ExternalCommissionAssignmentMail, identity["mail"])
    if (item is None or not assignment_is_current(item) or item.sent_at is None
            or not portal_light_enabled(item.external_craftsman) or identity != response_identity(item)):
        raise ValueError("Dieser Antwort-Link ist nicht mehr gültig. Bitte wenden Sie sich an die Sachbearbeitung.")
    return item


def submit_response(item, decision, text):
    from .audit import audit
    from .commission_workflow_service import accept_commission, reject_commission
    from .process_info_service import create_external_outgoing_info

    if item.response_at is not None:
        return False
    if not assignment_is_unanswered(item):
        raise ValueError("Für diesen Auftrag kann keine Entscheidung mehr abgegeben werden.")
    if decision not in {"accept", "reject"}:
        raise ValueError("Bitte Annahme oder Ablehnung auswählen.")
    text = str(text or "").strip()
    if len(text) > 2000 or "\x00" in text:
        raise ValueError("Die Mitteilung darf höchstens 2.000 Zeichen ohne Nullzeichen enthalten.")
    if decision == "accept":
        accept_commission(item.commission, external_craftsman=item.external_craftsman)
        if text:
            create_external_outgoing_info(
                item.commission, item.external_craftsman,
                str(uuid5(NAMESPACE_URL, f"external-order-response:{item.id}")), text, commit=False,
            )
    else:
        reject_commission(item.commission, text, external_craftsman=item.external_craftsman, allow_empty_reason=True)
    item.response_at = utc_now()
    item.response_decision = decision
    item.response_text = text or None
    audit("external.commission.response", "commission", item.commission_id,
          {"decision": decision, "assignment_version": item.assignment_version},
          actor_external_craftsman_id=item.external_craftsman_id)
    return True


def can_create_portal_password(item):
    return bool(item.response_at is not None and ensure_utc(item.response_at) + timedelta(hours=48) > utc_now()
                and item.external_craftsman.password_hash is None)


def set_external_order_content(message, subject, body, item, *, include_actions=True):
    from .external_portal_texts import portal_text_context, render_portal_texts, set_response_email_content

    accept_url = reject_url = texts = None
    if include_actions and portal_light_enabled(item.external_craftsman) and assignment_is_unanswered(item):
        accept_url, reject_url = response_url(item, "accept"), response_url(item, "reject")
        texts = render_portal_texts(portal_text_context(item.commission, item.external_craftsman))
    set_response_email_content(message, subject, body, accept_url=accept_url, reject_url=reject_url, texts=texts)


def ensure_external_followups(limit=200):
    from .commission_workflow_service import cancel_commission
    from .workflow_notifications import enqueue_notification

    now = utc_now()
    candidates = (ExternalCommissionAssignmentMail.query
                  .join(Commission, Commission.id == ExternalCommissionAssignmentMail.commission_id)
                  .join(ExternalCraftsman, ExternalCraftsman.id == ExternalCommissionAssignmentMail.external_craftsman_id)
                  .filter(ExternalCommissionAssignmentMail.sent_at.is_not(None),
                          ExternalCommissionAssignmentMail.response_at.is_(None),
                          ExternalCommissionAssignmentMail.auto_canceled_at.is_(None),
                          Commission.local_status == CommissionLocalStatus.WAITING_FOR_ACCEPTANCE.value,
                          db.or_(ExternalCraftsman.reminder_first_days > 0, ExternalCraftsman.reminder_second_days > 0,
                                 ExternalCraftsman.cancel_unanswered_days > 0))
                  .order_by(ExternalCommissionAssignmentMail.id).all())
    created = 0
    for item in candidates:
        craftsman = item.external_craftsman
        age = now - ensure_utc(item.sent_at)
        if not any(days and age >= timedelta(days=days) for days in (
            craftsman.reminder_first_days, craftsman.reminder_second_days, craftsman.cancel_unanswered_days,
        )):
            continue
        due_events = (["cancel"] if craftsman.cancel_unanswered_days and age >= timedelta(days=craftsman.cancel_unanswered_days)
                      else [event for event, days in (("first", craftsman.reminder_first_days), ("second", craftsman.reminder_second_days))
                            if days and age >= timedelta(days=days)])
        due_keys = [f"external-followup:{item.id}:{event}" for event in due_events]
        if len(WorkflowNotification.query.filter(WorkflowNotification.notification_key.in_(due_keys)).all()) == len(due_keys):
            continue
        item = locked_assignment(item.id)
        if item is None or item.sent_at is None or not assignment_is_unanswered(item):
            db.session.commit()
            continue
        craftsman = item.external_craftsman
        age = now - ensure_utc(item.sent_at)
        cancellation = craftsman.cancel_unanswered_days
        if cancellation and age >= timedelta(days=cancellation) and item.commission.can_be_canceled:
            cancel_commission(item.commission, automatic=True)
            item.auto_canceled_at = now
            enqueue_notification(f"external-followup:{item.id}:cancel", "external_followup", commission=item.commission,
                                 payload={"assignment_mail_id": item.id, "event": "cancel", "days": cancellation})
            created += 1
        else:
            for event, days in (("first", craftsman.reminder_first_days), ("second", craftsman.reminder_second_days)):
                if days and age >= timedelta(days=days):
                    notification = enqueue_notification(
                        f"external-followup:{item.id}:{event}", "external_followup", commission=item.commission,
                        payload={"assignment_mail_id": item.id, "event": event, "days": days},
                    )
                    if notification.state == "pending":
                        created += 1
        db.session.commit()
        if created >= limit:
            break
    return created


def followup_assignment(notification):
    return db.session.get(ExternalCommissionAssignmentMail, notification.payload.get("assignment_mail_id"))


def followup_is_current(notification):
    item = followup_assignment(notification)
    if item is None or not assignment_is_current(item) or item.sent_at is None:
        return False
    if notification.payload.get("recipient") and notification.payload["recipient"].strip().casefold() != item.external_craftsman.email.strip().casefold():
        return False
    event = notification.payload.get("event")
    if event == "cancel":
        return bool(item.auto_canceled_at is not None and item.commission.local_status == CommissionLocalStatus.CANCELED.value)
    days = {"first": item.external_craftsman.reminder_first_days,
            "second": item.external_craftsman.reminder_second_days}.get(event, 0)
    return bool(assignment_is_unanswered(item) and days and utc_now() >= ensure_utc(item.sent_at) + timedelta(days=days))


def followup_content(notification):
    from .email_templates import commission_email_context, render_email_template

    item = followup_assignment(notification)
    craftsman = item.external_craftsman
    context = commission_email_context(item.commission)
    event = notification.payload["event"]
    days = {"first": craftsman.reminder_first_days, "second": craftsman.reminder_second_days}.get(event, notification.payload["days"])
    context.update(recipient_name=craftsman.display_name, reminder_number="1" if notification.payload["event"] == "first" else "2",
                   days=str(days), commission_url=(response_url(item) if portal_light_enabled(craftsman) and event != "cancel"
                   else f"{settings.get(SettingKeys.PUBLIC_BASE_URL).strip().rstrip('/')}/auftragnehmer/auftraege/{item.commission.public_id}"
                   if craftsman.portal_enabled else ""))
    key = {"cancel": "external_cancellation", "first": "external_reminder", "second": "external_reminder_second"}[event]
    content = render_email_template(key, context)
    if item.commission.contact_information_text:
        content["body"] = item.commission.with_contact_information(content["body"]) + "\n"
    return craftsman.email.strip(), content
