from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from email.headerregistry import Address
from hashlib import sha256
from urllib.parse import urlsplit

from flask import current_app
from pywebpush import WebPushException, webpush
from sqlalchemy import func, or_

from .extensions import db
from .models import (
    BrowserPushSubscription, Commission, CommissionSource, ProcessInfo,
    ProcessInfoDelivery, ProcessInfoExternalRead, ProcessInfoRead,
    User, UserJurisdictionPersonMapping, utc_now,
)
from .process_info_service import (
    CRAFTSMAN_DIRECTION, OFFICE_DIRECTION, _integer_value, _row_value,
    _send_process_info_email, commissions_for_process,
    process_external_craftsmen, process_users,
)
from .settings import SettingKeys, settings
from .utils import ensure_utc, iso_datetime
from .wowiport import WowiportService


@dataclass(frozen=True)
class OfficeRecipient:
    key: str
    email: str
    name: str
    user_id: int | None
    commission_id: int


def office_user(user: User | None) -> bool:
    return bool(
        user and user.is_active_flag and (user.is_admin or user.is_foreman)
    )


def office_recipients(commissions: list[Commission]) -> list[OfficeRecipient]:
    result: dict[str, OfficeRecipient] = {}
    service = None
    officials: dict[int, dict] = {}

    for commission in commissions:
        user = None
        email = ""

        if commission.local_official_user_id is not None:
            user = db.session.get(User, commission.local_official_user_id)
            if not office_user(user):
                continue
            email = user.email.strip()
        else:
            official_id = commission.responsible_official_repair_erp_id
            if official_id is None and commission.erp_id is not None:
                service = service or WowiportService()
                official_id = service.get_commission_responsible_official_id(
                    commission.erp_id
                )
                commission.responsible_official_repair_erp_id = official_id

            if official_id is not None:
                service = service or WowiportService()
                if official_id not in officials:
                    officials[official_id] = service.get_responsible_official(
                        official_id
                    )

                person = _row_value(officials[official_id], "Person") or {}
                person_id = _integer_value(_row_value(person, "Id"))
                communication = (
                    _row_value(person, "FirstEmailCommunication") or {}
                )
                email = str(
                    _row_value(communication, "Content") or ""
                ).strip()

                if person_id is not None:
                    mapping = UserJurisdictionPersonMapping.query.filter_by(
                        jurisdiction_person_erp_id=person_id, active=True,
                    ).one_or_none()
                    user = (
                        db.session.get(User, mapping.user_id)
                        if mapping
                        else User.query.filter_by(
                            person_erp_id=person_id
                        ).one_or_none()
                    )

                if not office_user(user) and email:
                    user = User.query.filter(
                        func.lower(User.email) == email.lower(),
                        User.is_active_flag.is_(True),
                        or_(
                            User.is_admin.is_(True),
                            User.is_foreman.is_(True),
                        ),
                    ).one_or_none()

                if office_user(user):
                    email = user.email.strip()
                else:
                    user = None

            elif (
                commission.source == CommissionSource.WEB.value
                and commission.created_by
            ):
                user = (
                    commission.created_by
                    if office_user(commission.created_by)
                    else None
                )
                email = commission.created_by.email.strip()

        if not email:
            raise ValueError(
                "Für einen zuständigen Sachbearbeiter fehlt die E-Mail-Adresse."
            )

        Address(addr_spec=email)
        key = (
            f"user:{user.id}"
            if user
            else "email:" + sha256(email.lower().encode()).hexdigest()
        )
        result.setdefault(
            key,
            OfficeRecipient(
                key, email,
                user.display_name
                if user else "Sachbearbeitung ohne Benutzerzuordnung",
                user.id if user else None,
                commission.id,
            ),
        )

    return list(result.values())


def visible_items(data: object) -> dict[str, int]:
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or len(items) > 250:
        raise ValueError(
            "items muss eine Liste mit höchstens 250 Nachrichten sein."
        )

    result = {}
    for item in items:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or type(item.get("version")) is not int
            or item["version"] < 1
        ):
            raise ValueError(
                "Jede Nachricht benötigt eine ID und eine positive Version."
            )
        result[item["id"]] = max(
            result.get(item["id"], 0), item["version"],
        )

    return result


def record_visible_reads(
    process_id: int | None,
    items: dict[str, int],
    direction: str,
    *,
    user_id: int | None = None,
    external_id: int | None = None,
) -> None:
    if (user_id is None) == (external_id is None):
        raise ValueError("Der Leser ist nicht eindeutig.")

    messages = ProcessInfo.query.filter(
        ProcessInfo.process_erp_id == process_id,
        ProcessInfo.id.in_(list(items)),
        ProcessInfo.direction == direction,
        ProcessInfo.state == "synced",
    ).order_by(
        ProcessInfo.id.asc()
    ).with_for_update().all()

    for message in messages:
        version = items[message.id]
        if version > message.version:
            raise ValueError(
                "Die gelesene Nachrichtenversion ist ungültig."
            )

        model = (
            ProcessInfoRead
            if user_id is not None else ProcessInfoExternalRead
        )
        reader_id = user_id if user_id is not None else external_id
        receipt = db.session.get(model, (message.id, reader_id))
        verified = bool(
            receipt and (user_id is None or receipt.read_verified)
        )

        if receipt is None:
            values = {
                "process_info_id": message.id,
                "read_version": version,
                "read_at": utc_now(),
            }
            values[
                "user_id"
                if user_id is not None else "external_craftsman_id"
            ] = reader_id

            if user_id is not None:
                values["read_verified"] = True

            receipt = model(**values)
            db.session.add(receipt)

        elif not verified or receipt.read_version < version:
            receipt.read_version = version
            receipt.read_at = utc_now()
            if user_id is not None:
                receipt.read_verified = True

        if (
            user_id is not None
            and direction == CRAFTSMAN_DIRECTION
        ):
            ProcessInfoDelivery.query.filter(
                ProcessInfoDelivery.process_info_id == message.id,
                ProcessInfoDelivery.user_id == user_id,
                ProcessInfoDelivery.version <= receipt.read_version,
                ProcessInfoDelivery.state.in_(("pending", "failed")),
            ).update(
                {ProcessInfoDelivery.state: "read"},
                synchronize_session=False,
            )
            settle_mail_state(message)


def receipt_status(
    message: ProcessInfo,
    reader_id: int | None,
    name: str,
    external=False,
) -> dict:
    model = ProcessInfoExternalRead if external else ProcessInfoRead
    receipt = (
        db.session.get(model, (message.id, reader_id))
        if reader_id is not None else None
    )
    verified = bool(
        receipt and (external or receipt.read_verified)
    )
    is_read = bool(
        verified and receipt.read_version >= message.version
    )

    return {
        "reader_name": name,
        "reader_kind": "external" if external else "user",
        "reader_id": reader_id,
        "is_read": is_read,
        "read_at": (
            iso_datetime(ensure_utc(receipt.read_at))
            if is_read else None
        ),
    }


def read_receipts(message: ProcessInfo) -> list[dict]:
    if message.direction == CRAFTSMAN_DIRECTION:
        deliveries = ProcessInfoDelivery.query.filter(
            ProcessInfoDelivery.process_info_id == message.id,
            ProcessInfoDelivery.version == message.version,
            ProcessInfoDelivery.channel == "email",
            ProcessInfoDelivery.state.in_(
                ("pending", "failed", "sent", "read")
            ),
        ).order_by(ProcessInfoDelivery.id.asc()).all()

        return [
            receipt_status(message, row.user_id, row.recipient_name)
            for row in deliveries
        ]

    result = [
        receipt_status(message, user.id, user.display_name)
        for user, _ in process_users(message.process_erp_id)
        if user.id != message.sender_user_id
    ]
    result.extend(
        receipt_status(
            message, craftsman.id,
            craftsman.display_name, external=True,
        )
        for craftsman, _ in process_external_craftsmen(
            message.process_erp_id
        )
        if craftsman.id != message.sender_external_craftsman_id
    )
    return result


def settle_mail_state(message: ProcessInfo) -> None:
    db.session.flush()
    deliveries = ProcessInfoDelivery.query.filter_by(
        process_info_id=message.id,
        version=message.version,
        channel="email",
    ).all()

    if deliveries and all(
        row.state not in ("pending", "failed")
        for row in deliveries
    ):
        message.mail_state = (
            "sent"
            if any(row.state == "sent" for row in deliveries)
            else "not_required"
        )


def queue_office_notifications(
    message: ProcessInfo,
    recipients: list[OfficeRecipient],
) -> None:
    now = utc_now()
    delay = timedelta(
        minutes=max(
            1,
            settings.get(SettingKeys.PROCESS_INFO_EMAIL_DELAY_MINUTES),
        )
    )
    active_keys = {recipient.key for recipient in recipients}
    existing = ProcessInfoDelivery.query.filter_by(
        process_info_id=message.id, version=message.version,
    ).all()

    for delivery in existing:
        if (
            delivery.recipient_key not in active_keys
            and delivery.state in ("pending", "failed", "read")
        ):
            delivery.state = "reassigned"

    by_destination = {
        (row.channel, row.destination_key): row
        for row in existing
    }

    for recipient in recipients:
        targets = [("email", recipient.key, None)]
        if recipient.user_id is not None:
            targets.extend(
                ("webpush", str(subscription.id), subscription.id)
                for subscription in BrowserPushSubscription.query.filter_by(
                    user_id=recipient.user_id, active=True,
                ).all()
            )

        for channel, destination, subscription_id in targets:
            delivery = by_destination.get((channel, destination))

            if delivery is None:
                delivery = ProcessInfoDelivery(
                    process_info_id=message.id,
                    version=message.version,
                    channel=channel,
                    destination_key=destination,
                    subscription_id=subscription_id,
                    recipient_key=recipient.key,
                    user_id=recipient.user_id,
                    recipient_email=recipient.email,
                    recipient_name=recipient.name,
                    commission_id=recipient.commission_id,
                    available_at=(
                        now + delay if channel == "email" else now
                    ),
                )
                db.session.add(delivery)

            elif (
                delivery.state == "reassigned"
                or delivery.recipient_key != recipient.key
            ):
                delivery.recipient_key = recipient.key
                delivery.user_id = recipient.user_id
                delivery.recipient_email = recipient.email
                delivery.recipient_name = recipient.name
                delivery.commission_id = recipient.commission_id
                delivery.state = "pending"
                delivery.attempt_count = 0
                delivery.available_at = (
                    now + delay if channel == "email" else now
                )

    message.mail_state = "queued" if recipients else "not_required"
    message.mail_available_at = None
    message.last_error = None
    db.session.flush()


def queue_next_office_info() -> int:
    message = ProcessInfo.query.filter(
        ProcessInfo.direction == CRAFTSMAN_DIRECTION,
        ProcessInfo.state == "synced",
        ProcessInfo.mail_state.in_(("pending", "failed")),
        ProcessInfo.mail_attempt_count < 5,
        ProcessInfo.mail_available_at <= utc_now(),
    ).order_by(
        ProcessInfo.mail_available_at.asc(),
        ProcessInfo.id.asc(),
    ).with_for_update(skip_locked=True).first()

    if message is None:
        db.session.rollback()
        return 0

    message_id = message.id
    try:
        recipients = office_recipients(
            commissions_for_process(message.process_erp_id)
        )
        queue_office_notifications(message, recipients)
        db.session.commit()

    except Exception as exc:
        db.session.rollback()
        message = db.session.get(ProcessInfo, message_id)
        message.mail_attempt_count += 1
        message.mail_state = "failed"
        message.mail_available_at = utc_now() + timedelta(minutes=1)
        message.last_error = str(exc)[:4000]
        db.session.commit()
        current_app.logger.exception(
            "office_info_queue_failed info_id=%s", message_id,
        )

    return 1


def process_one_office_delivery() -> int:
    candidate = ProcessInfoDelivery.query.filter(
        ProcessInfoDelivery.state.in_(("pending", "failed")),
        ProcessInfoDelivery.attempt_count < 5,
        ProcessInfoDelivery.available_at <= utc_now(),
    ).order_by(
        ProcessInfoDelivery.available_at.asc(),
        ProcessInfoDelivery.id.asc(),
    ).first()

    if candidate is None:
        db.session.rollback()
        return 0

    delivery_id = candidate.id
    message = ProcessInfo.query.filter_by(
        id=candidate.process_info_id,
    ).with_for_update(skip_locked=True).first()

    if message is None:
        db.session.rollback()
        return 0

    db.session.refresh(candidate)
    if (
        candidate.state not in ("pending", "failed")
        or candidate.attempt_count >= 5
        or ensure_utc(candidate.available_at) > utc_now()
    ):
        db.session.rollback()
        return 1

    try:
        if (
            message.state != "synced"
            or message.version != candidate.version
        ):
            candidate.state = "cancelled"
        else:
            recipients = office_recipients(
                commissions_for_process(message.process_erp_id)
            )
            queue_office_notifications(message, recipients)
            recipient = next(
                (
                    item for item in recipients
                    if item.key == candidate.recipient_key
                ),
                None,
            )

            if recipient is None:
                candidate.state = "reassigned"

            elif receipt_status(
                message, recipient.user_id, recipient.name,
            )["is_read"]:
                candidate.state = "read"

            elif candidate.channel == "email":
                candidate.user_id = recipient.user_id
                candidate.recipient_email = recipient.email
                candidate.attempt_count += 1
                _send_process_info_email(message, [recipient.email])
                candidate.state = "sent"
                candidate.sent_at = utc_now()

            else:
                subscription = db.session.get(
                    BrowserPushSubscription,
                    candidate.subscription_id,
                )

                if (
                    not subscription
                    or not subscription.active
                    or subscription.user_id != recipient.user_id
                ):
                    candidate.state = "cancelled"

                elif not settings.get(SettingKeys.WEB_PUSH_ENABLED):
                    candidate.state = "cancelled"

                else:
                    candidate.attempt_count += 1
                    webpush(
                        subscription_info=subscription.subscription,
                        data=json.dumps({
                            "title": "Neue Zusatzinformation",
                            "body": (
                                "Im Auftragschat liegt eine neue Nachricht vor."
                            ),
                            "url": (
                                f"/admin/commissions/{recipient.commission_id}"
                                "#zusatzinformationen"
                            ),
                            "tag": (
                                f"process-info-{message.id}-{message.version}"
                            ),
                        }),
                        vapid_private_key=settings.get(
                            SettingKeys.WEB_PUSH_VAPID_PRIVATE_KEY
                        ),
                        vapid_claims={
                            "sub": settings.get(
                                SettingKeys.WEB_PUSH_VAPID_SUBJECT
                            ),
                        },
                        headers=(
                            {"X-WNS-Type": "wns/raw"}
                            if (
                                urlsplit(
                                    subscription.subscription["endpoint"]
                                ).hostname or ""
                            ).endswith(".notify.windows.com")
                            else None
                        ),
                        ttl=3600,
                        timeout=10,
                    )
                    candidate.state = "sent"
                    candidate.sent_at = utc_now()

        candidate.last_error = None
        settle_mail_state(message)
        db.session.commit()

    except Exception as exc:
        db.session.rollback()
        candidate = db.session.get(ProcessInfoDelivery, delivery_id)
        candidate.attempt_count += 1
        candidate.state = "failed"
        candidate.available_at = utc_now() + timedelta(
            seconds=min(
                60 * 2 ** (candidate.attempt_count - 1), 3600,
            )
        )
        candidate.last_error = str(exc)[:4000]

        if (
            isinstance(exc, WebPushException)
            and exc.response is not None
            and exc.response.status_code in (404, 410)
        ):
            subscription = db.session.get(
                BrowserPushSubscription,
                candidate.subscription_id,
            )
            if subscription:
                subscription.active = False
            candidate.state = "cancelled"

        db.session.commit()
        current_app.logger.exception(
            "office_info_delivery_failed delivery_id=%s", delivery_id,
        )

    return 1


def process_office_info_work(limit: int = 50) -> int:
    count = queue_next_office_info()

    for _ in range(limit):
        processed = process_one_office_delivery()
        count += processed
        if not processed:
            break

    return count


def notify_craftsmen(message: ProcessInfo) -> None:
    from .process_info_service import unread_count_for_user
    from .push_service import enqueue_process_info_push

    db.session.flush()
    message.mail_state = (
        "pending"
        if process_external_craftsmen(message.process_erp_id)
        else "not_required"
    )
    message.mail_available_at = (
        utc_now() if message.mail_state == "pending" else None
    )

    for user, commission in process_users(message.process_erp_id):
        if user.id != message.sender_user_id:
            enqueue_process_info_push(
                user=user,
                commission=commission,
                info=message,
                badge=unread_count_for_user(user.id),
            )