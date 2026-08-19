from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import requests
from flask import current_app
from sqlalchemy import func, or_

from .extensions import db
from .models import (
    Commission,
    CommissionLocalStatus,
    PushDelivery,
    PushDeliveryState,
    PushDevice,
    PushEventType,
    User,
    utc_now,
    SafetyInspectionJob,
    SafetyInspectionJobStatus,
)
from .settings import SettingKeys, settings


DEVICE_TOKEN_PATTERN = re.compile(r"^[0-9a-fA-F]+$")
INVALID_DEVICE_REASONS = {
    "BadDeviceToken",
    "DeviceTokenNotForTopic",
    "Unregistered",
}


@dataclass(frozen=True)
class PushBatch:
    batch_id: str
    queued_count: int


def normalize_installation_id(value: str) -> str:
    try:
        return str(UUID(str(value).strip()))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("Die Installations-ID ist ungültig.") from exc


def normalize_device_token(value: str) -> str:
    token = str(value or "").strip().lower()
    if (
        len(token) < 32
        or len(token) > 512
        or len(token) % 2 != 0
        or DEVICE_TOKEN_PATTERN.fullmatch(token) is None
    ):
        raise ValueError("Das Geräte-Token ist ungültig.")
    return token


def register_push_device(
    user: User,
    installation_id: str,
    device_token: str,
    platform: str = "ios",
    app_version: str | None = None,
    device_name: str | None = None,
) -> PushDevice:
    normalized_installation_id = normalize_installation_id(
        installation_id
    )
    normalized_device_token = normalize_device_token(device_token)
    normalized_platform = str(platform or "ios").strip().lower()

    if normalized_platform != "ios":
        raise ValueError("Die Plattform wird nicht unterstützt.")

    device = PushDevice.query.filter_by(
        installation_id=normalized_installation_id
    ).one_or_none()

    if device is None:
        device = (
            PushDevice.query.filter_by(
                device_token=normalized_device_token,
                active=True,
            )
            .order_by(PushDevice.id.desc())
            .first()
        )

        if device is None:
            device = PushDevice(
                installation_id=normalized_installation_id,
                device_token=normalized_device_token,
                user=user,
            )
            db.session.add(device)
        else:
            device.installation_id = normalized_installation_id

    now = utc_now()
    device.user = user
    device.device_token = normalized_device_token
    device.platform = normalized_platform
    device.app_version = _optional_text(app_version, 80)
    device.device_name = _optional_text(device_name, 255)
    device.active = True
    device.last_registered_at = now
    device.updated_at = now

    db.session.flush()

    PushDevice.query.filter(
        PushDevice.device_token == normalized_device_token,
        PushDevice.id != device.id,
        PushDevice.active.is_(True),
    ).update(
        {
            PushDevice.active: False,
            PushDevice.updated_at: now,
        },
        synchronize_session=False,
    )

    return device


def unregister_push_device(
    user: User,
    installation_id: str,
) -> PushDevice | None:
    normalized_installation_id = normalize_installation_id(
        installation_id
    )
    device = PushDevice.query.filter_by(
        installation_id=normalized_installation_id,
        user_id=user.id,
    ).one_or_none()

    if device is None:
        return None

    device.active = False
    device.updated_at = utc_now()
    return device


def enqueue_commission_push(
    commission: Commission,
    event_type: PushEventType,
    user: User | None = None,
) -> PushBatch:
    target_user = user or commission.user

    if target_user is None or not target_user.is_active_flag:
        return PushBatch(batch_id=str(uuid4()), queued_count=0)

    if not _event_enabled(target_user, event_type):
        return PushBatch(batch_id=str(uuid4()), queued_count=0)

    if commission.id is None:
        db.session.flush()

    title, body = _commission_message(commission, event_type)
    payload = {
        "aps": {
            "alert": {
                "title": title,
                "body": body,
            },
            "sound": "default",
        },
        "event_type": event_type.value,
        "commission_id": commission.id,
        "commission_public_id": commission.public_id,
    }
    collapse_id = f"{event_type.value}-{commission.public_id}"

    return _enqueue_for_user(
        user=target_user,
        event_type=event_type,
        title=title,
        body=body,
        payload=payload,
        commission=commission,
        collapse_id=collapse_id,
    )


def enqueue_safety_inspection_push(
    job: SafetyInspectionJob,
    event_type: PushEventType,
    user: User | None = None,
) -> PushBatch:
    allowed_event_types = {
        PushEventType.NEW_SAFETY_INSPECTION,
        PushEventType.CHANGED_SAFETY_INSPECTION,
        PushEventType.URGENT_SAFETY_DEFECT,
    }

    if event_type not in allowed_event_types:
        raise ValueError(
            "Der Push-Ereignistyp ist für "
            "Prüfaufträge nicht zulässig."
        )

    target_user = (
        user or job.assigned_user
    )

    if (
        target_user is None
        or not target_user.is_active_flag
    ):
        return PushBatch(
            batch_id=str(uuid4()),
            queued_count=0,
        )

    if not _event_enabled(
        target_user,
        event_type,
    ):
        return PushBatch(
            batch_id=str(uuid4()),
            queued_count=0,
        )

    if job.id is None:
        db.session.flush()

    title, body = (
        _safety_inspection_message(
            job,
            event_type,
        )
    )
    payload = {
        "aps": {
            "alert": {
                "title": title,
                "body": body,
            },
            "sound": "default",
        },
        "event_type": event_type.value,
        "safety_inspection_job_id": (
            job.id
        ),
        "safety_inspection_job_public_id": (
            job.public_id
        ),
    }
    collapse_id = (
        f"{event_type.value}-"
        f"{job.public_id}"
    )

    return _enqueue_for_user(
        user=target_user,
        event_type=event_type,
        title=title,
        body=body,
        payload=payload,
        commission=None,
        collapse_id=collapse_id,
        safety_inspection_job=job,
    )


def enqueue_urgent_safety_defect_push(
    job: SafetyInspectionJob,
) -> int:
    reviewers = (
        User.query.filter(
            User.is_active_flag.is_(True),
            or_(
                User.is_admin.is_(True),
                User
                .can_review_safety_inspections
                .is_(True),
            ),
        )
        .order_by(User.id.asc())
        .all()
    )
    queued_count = 0

    for reviewer in reviewers:
        batch = (
            enqueue_safety_inspection_push(
                job,
                (
                    PushEventType
                    .URGENT_SAFETY_DEFECT
                ),
                user=reviewer,
            )
        )
        queued_count += batch.queued_count

    return queued_count


def enqueue_push_ping(
    user: User,
    message: str,
) -> PushBatch:
    title = "Push-Test"
    body = str(message or "Push-Verbindung erfolgreich getestet.").strip()
    body = body[:500] or "Push-Verbindung erfolgreich getestet."
    payload = {
        "aps": {
            "alert": {
                "title": title,
                "body": body,
            },
            "sound": "default",
        },
        "event_type": PushEventType.PING.value,
    }

    return _enqueue_for_user(
        user=user,
        event_type=PushEventType.PING,
        title=title,
        body=body,
        payload=payload,
        commission=None,
        collapse_id=None,
    )


def process_push_batch(
    limit: int = 50,
    batch_id: str | None = None,
) -> int:
    processed_count = 0

    for _ in range(max(limit, 0)):
        delivery_id = _claim_next_delivery(batch_id=batch_id)
        if delivery_id is None:
            break

        try:
            _deliver(delivery_id)
        except Exception as exc:
            db.session.rollback()
            current_app.logger.exception(
                "push_delivery_unexpected_failure delivery_id=%s",
                delivery_id,
            )
            delivery = db.session.get(PushDelivery, delivery_id)
            if (
                delivery is not None
                and delivery.state == PushDeliveryState.PROCESSING.value
            ):
                _fail_delivery(
                    delivery,
                    f"Unerwarteter Fehler bei der Push-Zustellung: {exc}",
                    retryable=True,
                )

        processed_count += 1

    return processed_count


def requeue_stale_push_deliveries() -> int:
    threshold = utc_now() - timedelta(minutes=15)
    updated_count = PushDelivery.query.filter(
        PushDelivery.state == PushDeliveryState.PROCESSING.value,
        PushDelivery.locked_at < threshold,
    ).update(
        {
            PushDelivery.state: PushDeliveryState.FAILED.value,
            PushDelivery.available_at: utc_now(),
            PushDelivery.locked_at: None,
            PushDelivery.last_error: (
                "Veraltete Verarbeitung wurde erneut freigegeben."
            ),
        },
        synchronize_session=False,
    )
    return int(updated_count)


def push_batch_state_counts(batch_id: str) -> dict[str, int]:
    rows = (
        db.session.query(PushDelivery.state, func.count(PushDelivery.id))
        .filter(PushDelivery.batch_id == batch_id)
        .group_by(PushDelivery.state)
        .all()
    )
    return {str(state): int(count) for state, count in rows}


def _enqueue_for_user(
    user: User,
    event_type: PushEventType,
    title: str,
    body: str,
    payload: dict[str, Any],
    commission: Commission | None,
    collapse_id: str | None,
    safety_inspection_job: (
        SafetyInspectionJob | None
    ) = None,
) -> PushBatch:
    devices = PushDevice.query.filter_by(
        user_id=user.id,
        active=True,
        platform="ios",
    ).order_by(PushDevice.id.asc()).all()
    batch_id = str(uuid4())

    for device in devices:
        db.session.add(
            PushDelivery(
                batch_id=batch_id,
                device=device,
                user=user,
                commission=commission,
                device_token=device.device_token,
                event_type=event_type.value,
                title=title[:180],
                body=body[:500],
                payload=payload,
                collapse_id=collapse_id,
                state=PushDeliveryState.PENDING.value,
                available_at=utc_now(),
                safety_inspection_job=(
                    safety_inspection_job
                ),
            )
        )

    return PushBatch(
        batch_id=batch_id,
        queued_count=len(devices),
    )


def _event_enabled(
    user: User,
    event_type: PushEventType,
) -> bool:
    if (
        event_type
        == PushEventType.NEW_COMMISSION
    ):
        return bool(
            user.push_new_commission_enabled
        )

    if (
        event_type
        == PushEventType.CHANGED_COMMISSION
    ):
        return bool(
            user
            .push_changed_commission_enabled
        )

    if event_type in {
        PushEventType
        .NEW_SAFETY_INSPECTION,
        PushEventType
        .CHANGED_SAFETY_INSPECTION,
        PushEventType
        .URGENT_SAFETY_DEFECT,
    }:
        return bool(
            user
            .push_safety_inspection_enabled
        )

    return True


def _commission_message(
    commission: Commission,
    event_type: PushEventType,
) -> tuple[str, str]:
    reference = (
        commission.erp_idnum
        or commission.code
        or f"Auftrag {commission.id}"
    )
    display_title = commission.display_title

    if event_type == PushEventType.NEW_COMMISSION:
        return "Neuer Auftrag", f"{reference}: {display_title}"

    return "Auftrag geändert", f"{reference}: {display_title}"


def _safety_inspection_message(
    job: SafetyInspectionJob,
    event_type: PushEventType,
) -> tuple[str, str]:
    inspection_name = (
        job.template_version.template.name
    )
    object_text = (
        job.object_address
        or job.object_location
        or job.use_unit_idnum
        or job.building_idnum
        or job.economic_unit_idnum
        or f"Prüfung {job.id}"
    )
    due_text = job.due_to.strftime(
        "%d.%m.%Y"
    )

    if event_type == (
        PushEventType
        .NEW_SAFETY_INSPECTION
    ):
        return (
            "Neue Prüfung",
            (
                f"{inspection_name}: "
                f"{object_text}, "
                f"fällig bis {due_text}"
            ),
        )

    if event_type == (
        PushEventType
        .URGENT_SAFETY_DEFECT
    ):
        return (
            "Dringender Mangel",
            (
                f"{inspection_name}: "
                f"{object_text}"
            ),
        )

    return (
        "Prüfung geändert",
        (
            f"{inspection_name}: "
            f"{object_text}, "
            f"fällig bis {due_text}"
        ),
    )


def _claim_next_delivery(
    batch_id: str | None,
) -> int | None:
    max_attempts = max(settings.get(SettingKeys.PUSH_MAX_ATTEMPTS), 1)
    query = PushDelivery.query.filter(
        PushDelivery.state.in_(
            (
                PushDeliveryState.PENDING.value,
                PushDeliveryState.FAILED.value,
            )
        ),
        PushDelivery.attempt_count < max_attempts,
        PushDelivery.available_at <= utc_now(),
    )

    if batch_id is not None:
        query = query.filter(PushDelivery.batch_id == batch_id)

    delivery = (
        query.order_by(
            PushDelivery.available_at.asc(),
            PushDelivery.id.asc(),
        )
        .with_for_update(skip_locked=True)
        .first()
    )

    if delivery is None:
        db.session.rollback()
        return None

    delivery.state = PushDeliveryState.PROCESSING.value
    delivery.locked_at = utc_now()
    delivery.attempt_count += 1
    delivery_id = delivery.id
    db.session.commit()
    return delivery_id


def _deliver(delivery_id: int) -> None:
    delivery = db.session.get(PushDelivery, delivery_id)
    if delivery is None:
        return

    user = db.session.get(User, delivery.user_id)
    if user is None or not user.is_active_flag:
        _discard_delivery(
            delivery,
            "Der Benutzer ist nicht mehr aktiv.",
        )
        return

    try:
        event_type = PushEventType(delivery.event_type)
    except ValueError:
        _discard_delivery(
            delivery,
            "Der Push-Ereignistyp ist ungültig.",
        )
        return

    if not _event_enabled(user, event_type):
        _discard_delivery(
            delivery,
            "Der Benutzer hat diese Mitteilung deaktiviert.",
        )
        return

    if delivery.commission_id is not None:
        commission = db.session.get(
            Commission,
            delivery.commission_id,
        )
        if (
            commission is None
            or commission.user_id != delivery.user_id
            or commission.deleted_at is not None
            or commission.local_status == CommissionLocalStatus.DELETED.value
        ):
            _discard_delivery(
                delivery,
                "Der Auftrag ist diesem Benutzer nicht mehr zugeordnet.",
            )
            return

    if (
        delivery
        .safety_inspection_job_id
        is not None
    ):
        job = db.session.get(
            SafetyInspectionJob,
            (
                delivery
                .safety_inspection_job_id
            ),
        )

        if job is None:
            _discard_delivery(
                delivery,
                (
                    "Der Prüfauftrag ist "
                    "nicht mehr vorhanden."
                ),
            )
            return

        if event_type in {
            PushEventType
            .NEW_SAFETY_INSPECTION,
            PushEventType
            .CHANGED_SAFETY_INSPECTION,
        }:
            if (
                job.assigned_user_id
                != delivery.user_id
            ):
                _discard_delivery(
                    delivery,
                    (
                        "Der Prüfauftrag ist "
                        "diesem Benutzer nicht "
                        "mehr zugeordnet."
                    ),
                )
                return

            if job.status in {
                SafetyInspectionJobStatus
                .COMPLETED.value,
                SafetyInspectionJobStatus
                .COMPLETED_WITH_DEFECTS.value,
                SafetyInspectionJobStatus
                .MISSED.value,
                SafetyInspectionJobStatus
                .CANCELED.value,
            }:
                _discard_delivery(
                    delivery,
                    (
                        "Der Prüfauftrag ist "
                        "nicht mehr offen."
                    ),
                )
                return

        if event_type == (
            PushEventType
            .URGENT_SAFETY_DEFECT
        ):
            if not (
                user.is_admin
                or (
                    user
                    .can_review_safety_inspections
                )
            ):
                _discard_delivery(
                    delivery,
                    (
                        "Der Benutzer ist für "
                        "die Mangelprüfung nicht "
                        "mehr berechtigt."
                    ),
                )
                return

    device = db.session.get(PushDevice, delivery.device_id)
    if (
        device is None
        or not device.active
        or device.user_id != delivery.user_id
        or device.device_token != delivery.device_token
    ):
        _discard_delivery(
            delivery,
            "Die Gerätezuordnung ist nicht mehr aktuell.",
        )
        return

    relay_url = settings.get(SettingKeys.PUSH_RELAY_URL).strip().rstrip("/")
    api_key = settings.get(SettingKeys.PUSH_RELAY_API_KEY).strip()

    if not relay_url or not api_key:
        _fail_delivery(
            delivery,
            "Der Push-Provider ist nicht vollständig konfiguriert.",
            retryable=True,
        )
        return

    request_body: dict[str, Any] = {
        "device_token": delivery.device_token,
        "payload": delivery.payload,
        "push_type": "alert",
        "priority": 10,
        "expiration": int(time.time()) + 86_400,
    }

    if delivery.collapse_id:
        request_body["collapse_id"] = delivery.collapse_id

    try:
        response = requests.post(
            f"{relay_url}/v1/push",
            json=request_body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
            timeout=max(
                settings.get(SettingKeys.PUSH_RELAY_TIMEOUT_SECONDS),
                1.0,
            ),
        )
    except requests.RequestException as exc:
        _fail_delivery(
            delivery,
            f"Verbindung zum Push-Provider fehlgeschlagen: {exc}",
            retryable=True,
        )
        return

    try:
        response_data = response.json()
    except ValueError:
        response_data = {}

    if not isinstance(response_data, dict):
        response_data = {}

    reason = response_data.get("reason")
    if reason is None:
        error_data = response_data.get("error")
        if isinstance(error_data, dict):
            reason = error_data.get("code") or error_data.get("message")

    provider_status = response_data.get("apns_status")
    if not isinstance(provider_status, int):
        provider_status = response.status_code

    delivery.provider_status = provider_status
    delivery.provider_reason = _optional_text(reason, 120)

    if response.status_code == 202 and response_data.get("accepted") is True:
        delivery.state = PushDeliveryState.SUCCEEDED.value
        delivery.sent_at = utc_now()
        delivery.locked_at = None
        delivery.last_error = None
        db.session.commit()
        return

    if reason in INVALID_DEVICE_REASONS:
        device.active = False
        device.updated_at = utc_now()
        _discard_delivery(
            delivery,
            f"APNs hat das Geräte-Token abgelehnt: {reason}",
        )
        return

    retryable_value = response_data.get("retryable")
    retryable = (
        retryable_value is True
        or response.status_code in {429, 500, 502, 503, 504}
    )
    detail = reason or f"HTTP {response.status_code}"
    _fail_delivery(
        delivery,
        f"Push-Zustellung fehlgeschlagen: {detail}",
        retryable=retryable,
    )


def _fail_delivery(
    delivery: PushDelivery,
    message: str,
    retryable: bool,
) -> None:
    max_attempts = max(settings.get(SettingKeys.PUSH_MAX_ATTEMPTS), 1)
    delivery.state = PushDeliveryState.FAILED.value
    delivery.locked_at = None
    delivery.last_error = message[:4000]

    if retryable and delivery.attempt_count < max_attempts:
        delay_seconds = min(
            3600,
            30 * (2 ** max(delivery.attempt_count - 1, 0)),
        )
        delivery.available_at = utc_now() + timedelta(
            seconds=delay_seconds
        )
    else:
        delivery.attempt_count = max(
            delivery.attempt_count,
            max_attempts,
        )

    current_app.logger.warning(
        "push_delivery_failed delivery_id=%s user_id=%s retryable=%s error=%s",
        delivery.id,
        delivery.user_id,
        retryable,
        delivery.last_error,
    )
    db.session.commit()


def _discard_delivery(
    delivery: PushDelivery,
    message: str,
) -> None:
    delivery.state = PushDeliveryState.DISCARDED.value
    delivery.locked_at = None
    delivery.last_error = message[:4000]
    current_app.logger.info(
        "push_delivery_discarded delivery_id=%s user_id=%s reason=%s",
        delivery.id,
        delivery.user_id,
        delivery.last_error,
    )
    db.session.commit()


def _optional_text(
    value: Any,
    max_length: int,
) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:max_length] if text else None