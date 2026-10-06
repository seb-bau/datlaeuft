from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.errors import HeaderParseError
from email.headerregistry import Address
from email.message import EmailMessage
from typing import Any

from flask import current_app
from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError

from .extensions import db
from .mail_service import send_email
from .models import (
    AppSetting, Commission, CommissionSource, CommissionSyncState,
    CommissionTenantNotification, CraftActivity, ExternalCraftsman, User, utc_now,
)
from .settings import SettingKeys, settings
from .user_sync import extract_craftsman_display_name


def resolve_tenant_notification_choice(value: Any) -> bool:
    if value is None:
        return settings.get(SettingKeys.COMMISSION_NOTIFY_TENANTS_DEFAULT)
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().casefold() in {"true", "false"}:
        return value.strip().casefold() == "true"
    raise ValueError("Bitte auswählen, ob die Mieter informiert werden sollen.")


def read_value(item: Any, name: str) -> Any:
    if isinstance(item, dict):
        target = name.replace("_", "").casefold()
        return next((value for key, value in item.items()
                     if str(key).replace("_", "").casefold() == target), None)
    return getattr(item, name, None)


def normalize_tenant_email(value: Any) -> str | None:
    if not isinstance(value, str) or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    value = value.strip()
    try:
        address = Address(addr_spec=value)
    except (ValueError, HeaderParseError):
        return None
    if not address.username or not address.domain or "." not in address.domain or len(value) > 254:
        return None
    if address.addr_spec != value:
        return None
    return address.addr_spec


def tenant_email_addresses(commission: Commission, service: Any) -> list[str]:
    if commission.license_agreement_id is None:
        return []
    contractors = service.client.get_contractors(
        license_agreement_id=commission.license_agreement_id,
        fetch_all=True,
        add_args={
            "includeMainCommunication": "true",
            "includePersonCommunications": "true",
            "includePersonBankAccounts": "false",
        },
    )
    addresses = {}
    for contractor in contractors or []:
        person = read_value(contractor, "person")
        communications = list(read_value(person, "communications") or [])
        preferred = read_value(person, "first_email_communication")
        if preferred is not None:
            communications.append(preferred)
        for communication in communications:
            if communication is not preferred:
                communication_type = read_value(communication, "communication_type")
                if str(read_value(communication_type, "id_")) != "5":
                    continue
            address = normalize_tenant_email(read_value(communication, "content"))
            if address:
                addresses.setdefault(address.casefold(), address)
    return list(addresses.values())


def tenant_notification_body(commission: Commission, service: Any) -> str:
    craft_ids = {position.craft_activity_erp_id for position in commission.positions
                 if position.craft_activity_erp_id is not None}
    craft_names = {item.erp_id: item.name for item in
                   CraftActivity.query.filter(CraftActivity.erp_id.in_(craft_ids)).all()}
    if craft_ids - craft_names.keys():
        for item in service.get_craft_activities():
            erp_id = read_value(item, "id_")
            name = read_value(item, "name") or read_value(item, "code")
            if erp_id in craft_ids and name:
                craft_names.setdefault(erp_id, str(name))
    if not craft_ids or craft_ids - craft_names.keys():
        raise RuntimeError("Das Gewerk für die Mieter-Mitteilung konnte nicht ermittelt werden.")

    user = User.query.filter_by(craftsman_erp_id=commission.craftsman_erp_id).one_or_none()
    external = ExternalCraftsman.query.filter_by(
        craftsman_erp_id=commission.craftsman_erp_id,
    ).one_or_none() if user is None else None
    assignee_name = user.display_name if user is not None else (
        external.display_name if external is not None else ""
    )
    if not assignee_name:
        rows = service.client.get_commissioning_craftsmen(
            craftsman_id=commission.craftsman_erp_id,
            show_null_values=True,
        )
        if rows:
            assignee_name = extract_craftsman_display_name(rows[0], fallback="")
    if not assignee_name:
        raise RuntimeError("Der beauftragte Handwerker konnte nicht ermittelt werden.")

    names = sorted({" ".join(str(name).split()) for name in craft_names.values()})
    return "\n".join((
        "Guten Tag,", "", "für Ihr Mietverhältnis wurde eine Beauftragung erteilt.", "",
        f"Gewerk: {'; '.join(names)}",
        f"Beauftragt: {' '.join(str(assignee_name).split())}",
        "", "Freundliche Grüße", "",
    ))


def ensure_tenant_notifications(limit: int = 200) -> int:
    marker = AppSetting.query.filter_by(key="commissions.tenant_notifications_start_at").one()
    start_at = datetime.fromisoformat(marker.value_text)
    if start_at.tzinfo is None:
        start_at = start_at.replace(tzinfo=timezone.utc)
    commissions = (
        Commission.query.outerjoin(
            CommissionTenantNotification,
            CommissionTenantNotification.commission_id == Commission.id,
        )
        .filter(
            Commission.notify_tenants.is_(True),
            Commission.erp_id.is_not(None),
            Commission.craftsman_erp_id.is_not(None),
            Commission.erp_sync_state == CommissionSyncState.SYNCED.value,
            Commission.deleted_at.is_(None),
            Commission.status_id.in_((7, 12)),
            CommissionTenantNotification.id.is_(None),
            or_(
                Commission.source != CommissionSource.WOWIPORT.value,
                Commission.release_date >= start_at,
                and_(Commission.release_date.is_(None), Commission.recording_date >= start_at),
            ),
        )
        .order_by(Commission.id.asc()).limit(limit).all()
    )
    for commission in commissions:
        db.session.add(CommissionTenantNotification(commission=commission))
    return len(commissions)


def claim_tenant_notification() -> int | None:
    item = (
        CommissionTenantNotification.query.filter(
            CommissionTenantNotification.state.in_(("pending", "failed")),
            CommissionTenantNotification.available_at <= utc_now(),
        )
        .order_by(CommissionTenantNotification.available_at.asc(), CommissionTenantNotification.id.asc())
        .with_for_update(skip_locked=True).first()
    )
    if item is None:
        db.session.rollback()
        return None
    item_id = item.id
    updated = CommissionTenantNotification.query.filter(
        CommissionTenantNotification.id == item_id,
        CommissionTenantNotification.state.in_(("pending", "failed")),
    ).update({
        CommissionTenantNotification.state: "sending",
        CommissionTenantNotification.locked_at: utc_now(),
        CommissionTenantNotification.attempt_count: CommissionTenantNotification.attempt_count + 1,
    }, synchronize_session=False)
    if updated != 1:
        db.session.rollback()
        return None
    db.session.commit()
    return item_id


def execute_tenant_notification(item_id: int) -> None:
    item = db.session.get(CommissionTenantNotification, item_id)
    if item is None or item.state != "sending":
        return
    commission = item.commission
    if not commission.notify_tenants or commission.deleted_at is not None or commission.status_id not in (7, 12):
        item.state = "discarded"
        item.locked_at = None
        db.session.commit()
        return
    if commission.erp_sync_state != CommissionSyncState.SYNCED.value:
        raise RuntimeError("Die Übertragung des Auftrags an Wowiport ist noch nicht abgeschlossen.")

    if item.payload is None:
        from .wowiport import WowiportService

        service = WowiportService()
        addresses = tenant_email_addresses(commission, service)
        if not addresses:
            item.state = "no_recipients"
            item.locked_at = None
            item.last_error = None
            db.session.commit()
            return
        item.payload = {
            "recipients": addresses,
            "body": tenant_notification_body(commission, service),
            "delivered": [],
        }
        db.session.commit()

    smtp_host = settings.get(SettingKeys.SMTP_HOST).strip()
    smtp_sender = settings.get(SettingKeys.SMTP_SENDER).strip()
    if not smtp_host or not smtp_sender:
        raise RuntimeError("SMTP-Server oder Absenderadresse fehlen.")
    for address in item.payload["recipients"]:
        if address in item.payload["delivered"]:
            continue
        message = EmailMessage()
        message["From"] = smtp_sender
        message["To"] = address
        message["Subject"] = "Information zur Beauftragung"
        message.set_content(item.payload["body"])
        send_email(message, smtp_host=smtp_host, smtp_port=settings.get(SettingKeys.SMTP_PORT))
        item.payload = {**item.payload, "delivered": [*item.payload["delivered"], address]}
        item.locked_at = utc_now()
        db.session.commit()
    item.state = "sent"
    item.sent_at = utc_now()
    item.locked_at = None
    item.last_error = None
    db.session.commit()


def fail_tenant_notification(item_id: int, error: Exception) -> None:
    db.session.rollback()
    item = db.session.get(CommissionTenantNotification, item_id)
    if item is None:
        return
    item.state = "failed"
    item.available_at = utc_now() + timedelta(
        seconds=min(60 * (2 ** min(max(item.attempt_count - 1, 0), 6)), 3600)
    )
    item.locked_at = None
    item.last_error = str(error)[:4000]
    db.session.commit()
    current_app.logger.exception("tenant_notification_failed commission_id=%s", item.commission_id)


def process_tenant_notification_batch(limit: int = 20) -> int:
    now = utc_now()
    CommissionTenantNotification.query.filter(
        CommissionTenantNotification.state == "sending",
        CommissionTenantNotification.locked_at < now - timedelta(minutes=15),
    ).update({
        CommissionTenantNotification.state: "failed",
        CommissionTenantNotification.available_at: now,
        CommissionTenantNotification.locked_at: None,
    }, synchronize_session=False)
    try:
        ensure_tenant_notifications()
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
    processed = 0
    while processed < limit:
        item_id = claim_tenant_notification()
        if item_id is None:
            break
        try:
            execute_tenant_notification(item_id)
        except Exception as exc:
            fail_tenant_notification(item_id, exc)
        processed += 1
    return processed