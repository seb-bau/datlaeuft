from __future__ import annotations

import re
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
    CommissionTenantNotification, CommissionTicketLink, CraftActivity,
    ExternalCraftsman, TicketMirror, User, utc_now,
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


def tenant_notification_tickets(commission: Commission, service: Any) -> list[dict[str, Any]]:
    ticket_ids = {
        link.ticket_erp_id
        for link in CommissionTicketLink.query.filter_by(commission_id=commission.id).all()
    }
    ticket_ids.update(
        ticket.erp_id
        for ticket in TicketMirror.query.filter_by(commission_id=commission.id).all()
    )
    tickets = []
    for ticket_id in sorted(ticket_ids):
        rows = service.get_ticket_rows(ticket_erp_id=ticket_id)
        row = next((row for row in rows if read_value(row, "id_") == ticket_id), None)
        if row is None:
            raise RuntimeError(f"Das verknüpfte Ticket {ticket_id} konnte nicht gelesen werden.")
        ticket_number = str(read_value(row, "id_num") or "").strip()
        if re.fullmatch(r"[0-9]{5}", ticket_number) is None:
            raise RuntimeError(f"Die Ticketnummer '{ticket_number}' passt nicht zum mail2ticket-Format.")
        tickets.append({"ticket_id": ticket_id, "ticket_number": ticket_number, "comment_id": None})
    return tickets


def document_tenant_notification(item: CommissionTenantNotification, service: Any) -> None:
    if not settings.get(SettingKeys.TENANT_TICKET_WRITEBACK_ENABLED):
        return
    payload = item.payload
    if "tickets" not in payload:
        item.payload = {**payload, "tickets": tenant_notification_tickets(item.commission, service)}
        db.session.commit()
        payload = item.payload
    if not payload["tickets"]:
        return
    if "comment_content" not in payload:
        actual_recipients = payload.get("delivery_recipients")
        if actual_recipients is None:
            recipient_text = (
                "Debug-Adresse beim ursprünglichen Versand nicht gespeichert"
                if payload.get("debug_delivery") else "; ".join(payload["delivered"])
            )
        else:
            recipient_text = "; ".join(actual_recipients)
        content_lines = [
            "Per E-Mail versendete Beauftragungsinformation",
            f"Auftrag: {item.commission.erp_idnum}",
            f"An: {recipient_text}",
        ]
        if payload.get("debug_delivery"):
            content_lines.extend((
                "Debug-Umleitung: ja",
                f"Vorgesehene Empfänger: {'; '.join(payload['delivered'])}",
            ))
        content_lines.extend((
            f"Betreff: {payload.get('subject', 'Information zur Beauftragung')}",
            "", payload["body"],
        ))
        item.payload = {**payload, "comment_content": "\n".join(content_lines)}
        db.session.commit()
    for index in range(len(item.payload["tickets"])):
        ticket = item.payload["tickets"][index]
        if ticket.get("comment_id"):
            continue
        rows = service.get_ticket_rows(ticket_erp_id=ticket["ticket_id"], include_comments=True)
        row = next((row for row in rows if read_value(row, "id_") == ticket["ticket_id"]), None)
        if row is None:
            raise RuntimeError(f"Das Ticket {ticket['ticket_id']} konnte nicht dokumentiert werden.")
        content = item.payload["comment_content"]
        existing = next((
            comment for comment in read_value(row, "comments") or []
            if read_value(comment, "comment_from_api") is True
            and str(read_value(comment, "content") or "").replace("\r\n", "\n").rstrip()
            == content.replace("\r\n", "\n").rstrip()
        ), None)
        if existing is not None:
            comment_id = read_value(existing, "id_")
        else:
            result = service.client.create_ticket_comment(ticket_id=ticket["ticket_id"], content=content)
            if result.status_code not in (200, 201):
                raise RuntimeError(f"Die Ticketantwort konnte nicht gespeichert werden: {result.message}")
            data = result.data[0] if isinstance(result.data, list) and len(result.data) == 1 else result.data
            comment_id = read_value(data, "comment_id")
        if not isinstance(comment_id, int) or isinstance(comment_id, bool) or comment_id <= 0:
            raise RuntimeError("Wowiport hat keine gültige Kommentar-ID zurückgegeben.")
        tickets = [dict(entry) for entry in item.payload["tickets"]]
        tickets[index]["comment_id"] = comment_id
        item.payload = {**item.payload, "tickets": tickets}
        item.locked_at = utc_now()
        db.session.commit()


def execute_tenant_notification(item_id: int) -> None:
    item = db.session.get(CommissionTenantNotification, item_id)
    if item is None or item.state != "sending":
        return
    commission = item.commission
    mail_complete = item.payload is not None and bool(item.payload.get("recipients")) and all(
        address in item.payload.get("delivered", []) for address in item.payload["recipients"]
    )
    if not mail_complete and (
        not commission.notify_tenants or commission.deleted_at is not None or commission.status_id not in (7, 12)
    ):
        item.state = "discarded"
        item.locked_at = None
        db.session.commit()
        return
    if not mail_complete and commission.erp_sync_state != CommissionSyncState.SYNCED.value:
        raise RuntimeError("Die Übertragung des Auftrags an Wowiport ist noch nicht abgeschlossen.")

    from .wowiport import WowiportService

    service = WowiportService()
    if item.payload is None or ("tickets" not in item.payload and not item.payload.get("delivered")):
        previous_payload = item.payload
        addresses = previous_payload["recipients"] if previous_payload else tenant_email_addresses(commission, service)
        if not addresses:
            item.state = "no_recipients"
            item.locked_at = None
            item.last_error = None
            db.session.commit()
            return
        tickets = tenant_notification_tickets(commission, service)
        subject = "Information zur Beauftragung"
        reply_to = ""
        body = previous_payload["body"] if previous_payload else tenant_notification_body(commission, service)
        if tickets:
            reply_to = normalize_tenant_email(settings.get(SettingKeys.TENANT_TICKET_REPLY_TO))
            if not reply_to:
                raise RuntimeError("Bitte die mail2ticket-Antwortadresse in den E-Mail-Einstellungen hinterlegen.")
            delimiter = settings.get(SettingKeys.TENANT_TICKET_REPLY_DELIMITER).strip()
            if not delimiter or any(ord(character) < 32 or ord(character) == 127 for character in delimiter):
                raise RuntimeError("Bitte eine gültige Antwort-Trennzeile ohne Zeilenumbrüche hinterlegen.")
            subject += " " + " ".join(f"(Ticket {ticket['ticket_number']})" for ticket in tickets)
            body = f"{delimiter}\n\n{body}"
        item.payload = {
            "recipients": addresses,
            "body": body,
            "delivered": [],
            "subject": subject,
            "reply_to": reply_to,
            "tickets": tickets,
        }
        db.session.commit()

    pending = [address for address in item.payload["recipients"] if address not in item.payload["delivered"]]
    if pending:
        smtp_host = settings.get(SettingKeys.SMTP_HOST).strip()
        smtp_sender = settings.get(SettingKeys.SMTP_SENDER).strip()
        if not smtp_host or not smtp_sender:
            raise RuntimeError("SMTP-Server oder Absenderadresse fehlen.")
    for address in pending:
        message = EmailMessage()
        message["From"] = smtp_sender
        message["To"] = address
        message["Subject"] = item.payload.get("subject", "Information zur Beauftragung")
        if item.payload.get("reply_to"):
            message["Reply-To"] = item.payload["reply_to"]
        message.set_content(item.payload["body"])
        debug_recipient = send_email(message, smtp_host=smtp_host, smtp_port=settings.get(SettingKeys.SMTP_PORT))
        previous_recipients = item.payload.get("delivery_recipients")
        if previous_recipients is None:
            previous_recipients = [] if item.payload.get("debug_delivery") else item.payload["delivered"]
        item.payload = {
            **item.payload,
            "delivered": [*item.payload["delivered"], address],
            "debug_delivery": bool(item.payload.get("debug_delivery")) or bool(debug_recipient),
            "delivery_recipients": list(dict.fromkeys([*previous_recipients, debug_recipient or address])),
        }
        item.locked_at = utc_now()
        db.session.commit()
    document_tenant_notification(item, service)
    item.state = "sent"
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