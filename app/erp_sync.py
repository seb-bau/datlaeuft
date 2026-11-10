from __future__ import annotations

from datetime import timedelta
from time import sleep
from typing import Any

from flask import current_app
from sqlalchemy import or_
from time import monotonic, sleep

from .audit import audit
from .extensions import db
from app.models import (
    Commission,
    CommissionLocalStatus,
    CommissionSyncState,
    ErpOperationType,
    ErpOutboxOperation,
    ErpOutboxState,
    WebhookEvent,
    utc_now,
    CommissionPhoto,
    HealthHeartbeat
)
from .wowiport import (
    ERP_OPERATION_STATUS,
    WowiportService,
    mark_commission_deleted,
    sync_commission_from_erp,
)
from .document_sync import sync_commission_documents
from .push_service import (
    process_push_batch,
    requeue_stale_push_deliveries,
)
from .commission_priority import sync_ticket_commission_priorities
from .process_info_service import (
    process_process_info_work,
    sync_process_infos,
)
from .ticket_service import fetch_ticket_row, sync_ticket_mirror, upsert_ticket_row

from .external_craftsman_service import (
    process_external_assignment_mail_batch,
)
from .tenant_notifications import process_tenant_notification_batch
from .completion_notifications import process_completion_notification_batch
from .dms_service import process_dms_batch


def enqueue_erp_operation(
    commission: Commission,
    operation_type: ErpOperationType,
    idempotency_key: str,
    payload: dict[str, Any] | None = None,
    *,
    actor_user_id: int | None = None,
) -> ErpOutboxOperation:
    with db.session.no_autoflush:
        existing = ErpOutboxOperation.query.filter_by(
            idempotency_key=idempotency_key,
        ).one_or_none()

    if existing is not None:
        return existing

    operation_payload = dict(payload or {})

    if (
        actor_user_id is not None
        and operation_type.value in ERP_OPERATION_STATUS
    ):
        raw_payload = dict(commission.raw_payload or {})
        baseline_status = raw_payload.get("push_sync_status")

        if baseline_status is None:
            baseline_status = [
                commission.status_id,
                commission.status_code,
            ]
            raw_payload["push_sync_status"] = baseline_status
            commission.raw_payload = raw_payload

        operation_payload["push_origin"] = {
            "actor_user_id": actor_user_id,
            "baseline_signature": raw_payload.get("sync_signature"),
            "baseline_status": list(baseline_status),
            "consumed": False,
        }

    operation = ErpOutboxOperation(
        commission=commission,
        operation_type=operation_type.value,
        idempotency_key=idempotency_key,
        payload=operation_payload,
        state=ErpOutboxState.PENDING.value,
        available_at=utc_now(),
    )
    commission.erp_sync_state = CommissionSyncState.PENDING.value
    commission.erp_sync_message = None
    db.session.add(operation)
    return operation


def enqueue_pending_commission_photo_uploads(
    limit: int = 100,
) -> int:
    photos = (
        CommissionPhoto.query
        .join(
            Commission,
            Commission.id
            == CommissionPhoto.commission_id,
        )
        .filter(
            CommissionPhoto.source.in_(
                (
                    "app",
                    "web",
                )
            ),
            CommissionPhoto.context == "order",
            CommissionPhoto.active.is_(True),
            CommissionPhoto.external_document_id.is_not(
                None
            ),
            or_(
                CommissionPhoto.erp_sync_state.is_(None),
                CommissionPhoto.erp_sync_state
                != CommissionSyncState.SYNCED.value,
            ),
            Commission.erp_id.is_not(None),
        )
        .order_by(
            CommissionPhoto.created_at.asc(),
            CommissionPhoto.id.asc(),
        )
        .limit(limit)
        .all()
    )

    for photo in photos:
        enqueue_erp_operation(
            photo.commission,
            ErpOperationType.UPLOAD_COMMISSION_PHOTO,
            (
                "commission-photo:"
                f"{photo.external_document_id}"
            ),
            {
                "photo_id": photo.id,
            },
        )

    if photos:
        db.session.commit()

    return len(photos)


def process_due_work(
    outbox_limit: int = 20,
    webhook_limit: int = 50,
    push_limit: int = 50,
) -> dict[str, int]:
    requeue_stale_work()
    enqueue_pending_commission_photo_uploads()
    webhook_count = process_webhook_batch(webhook_limit)
    outbox_count = process_outbox_batch(outbox_limit)
    process_info_count = (
        process_process_info_work()
    )
    external_assignment_email_count = (
        process_external_assignment_mail_batch()
    )
    push_count = process_push_batch(push_limit)
    tenant_notification_count = process_tenant_notification_batch()
    completion_notification_count = process_completion_notification_batch()
    dms_count = process_dms_batch()
    return {
        "webhooks": webhook_count,
        "outbox": outbox_count,
        "process_infos": process_info_count,
        "push": push_count,
        "tenant_notifications": tenant_notification_count,
        "completion_notifications": completion_notification_count,
        "external_assignment_emails":
            external_assignment_email_count,
        "dms": dms_count,
    }


def requeue_stale_work() -> None:
    threshold = utc_now() - timedelta(minutes=15)

    ErpOutboxOperation.query.filter(
        ErpOutboxOperation.state == ErpOutboxState.PROCESSING.value,
        ErpOutboxOperation.locked_at < threshold,
    ).update(
        {
            ErpOutboxOperation.state: ErpOutboxState.FAILED.value,
            ErpOutboxOperation.available_at: utc_now(),
            ErpOutboxOperation.locked_at: None,
        },
        synchronize_session=False,
    )

    WebhookEvent.query.filter(
        WebhookEvent.processed.is_(False),
        WebhookEvent.processing_at < threshold,
    ).update(
        {
            WebhookEvent.processing_at: None,
            WebhookEvent.next_attempt_at: utc_now(),
        },
        synchronize_session=False,
    )

    requeue_stale_push_deliveries()

    db.session.commit()


def process_outbox_batch(limit: int) -> int:
    processed = 0
    service: WowiportService | None = None

    while processed < limit:
        operation_id = claim_outbox_operation()

        if operation_id is None:
            break

        try:
            if service is None:
                service = WowiportService()

            execute_outbox_operation(operation_id, service)
            finish_outbox_operation(operation_id)
        except Exception as exc:
            current_app.logger.exception(
                "erp_outbox_operation_failed operation_id=%s",
                operation_id,
            )
            fail_outbox_operation(operation_id, exc)

        processed += 1

    return processed


def claim_outbox_operation() -> int | None:
    operation = ErpOutboxOperation.query.filter(
        ErpOutboxOperation.state.in_(
            [
                ErpOutboxState.PENDING.value,
                ErpOutboxState.FAILED.value,
            ]
        ),
        ErpOutboxOperation.available_at <= utc_now(),
    ).order_by(
        ErpOutboxOperation.available_at.asc(),
        ErpOutboxOperation.id.asc(),
    ).with_for_update(
        skip_locked=True,
    ).first()

    if operation is None:
        db.session.rollback()
        return None

    operation.state = ErpOutboxState.PROCESSING.value
    operation.locked_at = utc_now()
    operation.attempt_count += 1
    db.session.commit()
    return operation.id


def execute_outbox_operation(
    operation_id: int,
    service: WowiportService,
) -> None:
    operation = db.session.get(ErpOutboxOperation, operation_id)

    if operation is None:
        raise RuntimeError("ERP-Outbox-Eintrag wurde nicht gefunden.")

    commission = operation.commission

    if (
            operation.operation_type
            == ErpOperationType.CREATE_COMMISSION.value
    ):
        service.resolve_commission_reference_ids(
            commission
        )
        result = (
            service.create_commission_waiting_for_acceptance(
                commission
            )
        )
        result_data = (
            result.data[0]
            if isinstance(result.data, list) and result.data
            else result.data
        )

        if not isinstance(result_data, dict):
            raise RuntimeError("OPENWOWI hat keine Auftragsdaten zurückgegeben.")

        erp_id = result_data.get("Id", result_data.get("id"))
        erp_idnum = result_data.get("IdNum", result_data.get("idNum"))

        if erp_id is None or not erp_idnum:
            raise RuntimeError(
                "OPENWOWI hat keine vollständige Auftragsnummer zurückgegeben."
            )

        commission.erp_id = int(erp_id)
        commission.erp_idnum = str(erp_idnum)
        commission.status_id = 12
        commission.status_code = "WaitForCraftsmanFeedback"
        commission.local_status = CommissionLocalStatus.WAITING_FOR_ACCEPTANCE.value

        try:
            document_result = (
                sync_commission_documents(
                    commission=commission,
                    service=service,
                    force=True,
                )
            )

            current_app.logger.info(
                "commission_document_sync_completed "
                "commission_id=%s erp_id=%s result=%s",
                commission.id,
                commission.erp_id,
                document_result,
            )
        except Exception as exc:
            commission.document_sync_error = (
                str(exc)[:4000]
            )

            current_app.logger.exception(
                "commission_document_sync_failed "
                "commission_id=%s erp_id=%s",
                commission.id,
                commission.erp_id,
            )

    elif operation.operation_type == ErpOperationType.ACCEPT_COMMISSION.value:
        require_erp_id(commission)
        service.mark_commission_accepted(commission.erp_id)
        commission.status_id = 7
        commission.status_code = "Accepted"
        commission.local_status = CommissionLocalStatus.IN_PROGRESS.value

    elif operation.operation_type in {
        ErpOperationType.REFUSE_COMMISSION.value,
        ErpOperationType.CANCEL_COMMISSION.value,
    }:
        require_erp_id(commission)

        reason = str(
            commission.completion_reason or ""
        ).strip()

        if not reason:
            raise RuntimeError(
                "Für den nicht abgeschlossenen Auftrag fehlt die Begründung."
            )

        service.mark_commission_canceled(
            commission.erp_id
        )

        commission.status_id = 10
        commission.status_code = "Canceled"
        commission.local_status = (
            CommissionLocalStatus.COMPLETION_REVIEW.value
        )

    elif operation.operation_type == ErpOperationType.COMPLETE_COMMISSION.value:
        require_erp_id(commission)
        service.mark_commission_done(commission.erp_id)
        commission.status_id = 9
        commission.status_code = "Done"
        commission.local_status = CommissionLocalStatus.DONE.value

    elif operation.operation_type == ErpOperationType.AWAIT_INVOICE.value:
        require_erp_id(commission)
        service.mark_commission_await_invoice(commission.erp_id)
        commission.status_id = 11
        commission.status_code = "AwaitInvoice"
        commission.local_status = CommissionLocalStatus.TENANT_CHARGE_REVIEW.value
    elif (
        operation.operation_type
        == ErpOperationType.UPLOAD_COMMISSION_PHOTO.value
    ):
        require_erp_id(commission)

        try:
            photo_id = int(
                (operation.payload or {}).get(
                    "photo_id"
                )
                or 0
            )
        except (TypeError, ValueError):
            photo_id = 0

        photo = db.session.get(
            CommissionPhoto,
            photo_id,
        )

        if (
            photo is None
            or photo.commission_id
            != commission.id
        ):
            raise RuntimeError(
                "Das zu übertragende Foto wurde nicht gefunden."
            )

        if photo.active:
            uploaded_row = (
                service.upload_commission_photo(
                    commission,
                    photo,
                )
            )

            file_guid = (
                uploaded_row.get("FileGuid")
                or uploaded_row.get("fileGuid")
                or uploaded_row.get("file_guid")
            )
            file_id = (
                uploaded_row.get("Id")
                or uploaded_row.get("id")
            )

            external_file_identity = (
                file_guid
                or file_id
            )

            if external_file_identity is None:
                raise RuntimeError(
                    "OPENWOWI hat keine Dokument-ID zurückgegeben."
                )

            photo.external_id = (
                "Commission:"
                f"{external_file_identity}"
            )
            photo.external_entity_name = (
                "Commission"
            )
            photo.external_entity_id = (
                commission.erp_id
            )
            photo.external_updated_at = utc_now()

        photo.erp_sync_state = (
            CommissionSyncState.SYNCED.value
        )
        photo.erp_sync_message = None
        photo.erp_synced_at = utc_now()
    else:
        raise RuntimeError(
            f"Unbekannte ERP-Operation: {operation.operation_type}"
        )

    commission.synced_at = utc_now()


def finish_outbox_operation(operation_id: int) -> None:
    operation = db.session.get(ErpOutboxOperation, operation_id)

    if operation is None:
        return

    operation.state = ErpOutboxState.SUCCEEDED.value
    operation.completed_at = utc_now()
    operation.locked_at = None
    operation.last_error = None
    operation.commission.erp_sync_state = CommissionSyncState.SYNCED.value
    operation.commission.erp_sync_message = None

    audit(
        "erp.outbox.succeeded",
        "commission",
        operation.commission_id,
        {
            "operation_type": operation.operation_type,
            "operation_id": operation.id,
        },
    )

    db.session.commit()


def fail_outbox_operation(
    operation_id: int,
    error: Exception,
) -> None:
    db.session.rollback()
    operation = db.session.get(ErpOutboxOperation, operation_id)

    if operation is None:
        return

    delay_seconds = min(
        30 * (2 ** min(max(operation.attempt_count - 1, 0), 7)),
        3600,
    )
    message = str(error)[:4000]
    if (
        operation.operation_type
        == ErpOperationType.UPLOAD_COMMISSION_PHOTO.value
    ):
        try:
            photo_id = int(
                (operation.payload or {}).get(
                    "photo_id"
                )
                or 0
            )
        except (TypeError, ValueError):
            photo_id = 0

        photo = db.session.get(
            CommissionPhoto,
            photo_id,
        )

        if (
            photo is not None
            and photo.commission_id
            == operation.commission_id
        ):
            photo.erp_sync_state = (
                CommissionSyncState.FAILED.value
            )
            photo.erp_sync_message = message

    operation.state = ErpOutboxState.FAILED.value
    operation.available_at = utc_now() + timedelta(seconds=delay_seconds)
    operation.locked_at = None
    operation.last_error = message
    operation.commission.erp_sync_state = CommissionSyncState.FAILED.value
    operation.commission.erp_sync_message = message

    audit(
        "erp.outbox.failed",
        "commission",
        operation.commission_id,
        {
            "operation_type": (
                operation.operation_type
            ),
            "operation_id": operation.id,
            "attempt_count": (
                operation.attempt_count
            ),
            "next_attempt_seconds": (
                delay_seconds
            ),
            "error": message,
        },
    )

    db.session.commit()


def process_webhook_batch(limit: int) -> int:
    processed = 0

    while processed < limit:
        event_id = claim_webhook_event()

        if event_id is None:
            break

        try:
            process_webhook_event(event_id)
            finish_webhook_event(event_id)
        except Exception as exc:
            current_app.logger.exception(
                "wowiport_webhook_processing_failed event_id=%s",
                event_id,
            )
            fail_webhook_event(event_id, exc)

        processed += 1

    return processed


def claim_webhook_event() -> int | None:
    event = WebhookEvent.query.filter(
        WebhookEvent.processed.is_(False),
        WebhookEvent.processing_at.is_(None),
        or_(
            WebhookEvent.next_attempt_at.is_(None),
            WebhookEvent.next_attempt_at <= utc_now(),
        ),
    ).order_by(
        WebhookEvent.received_at.asc(),
        WebhookEvent.id.asc(),
    ).with_for_update(
        skip_locked=True,
    ).first()

    if event is None:
        db.session.rollback()
        return None

    event.processing_at = utc_now()
    event.attempt_count += 1
    db.session.commit()
    return event.id


def process_webhook_event(event_id: int) -> None:
    event = db.session.get(WebhookEvent, event_id)

    if event is None:
        raise RuntimeError("Webhook-Ereignis wurde nicht gefunden.")

    if (event.entity_type or "").casefold() == "ticket":
        if event.entity_id is None:
            raise RuntimeError(
                "Webhook enthält keine Ticket-ID."
            )

        ticket_erp_id = event.entity_id
        action = event.action
        deleted = (action or "").casefold() == "deleted"
        service = WowiportService()
        ticket_row = (
            None if deleted else fetch_ticket_row(ticket_erp_id, service)
        )

        db.session.rollback()
        if deleted:
            sync_ticket_mirror(ticket_erp_id, service, deleted=True)
        else:
            upsert_ticket_row(ticket_row)
        db.session.commit()

        count = sync_ticket_commission_priorities(
            ticket_erp_id,
            service,
            deleted=deleted,
        )

        event.message = (
            "Ticket gespiegelt; Priorität für "
            f"{count} Aufträge geprüft"
        )

        audit(
            "webhook.wowiport",
            "ticket",
            ticket_erp_id,
            {
                "action": action,
                "event_id": event_id,
            },
        )
        return

    entity_type = "".join(
        character
        for character
        in (
                event.entity_type
                or ""
        ).casefold()
        if character.isalnum()
    )

    if entity_type in {
        "craftprocess",
        "craftsprocess",
    }:
        if event.entity_id is None:
            raise RuntimeError(
                "Webhook enthält keine "
                "Vorgangs-ID."
            )

        if (
                event.action
                or ""
        ).casefold() != "deleted":
            sync_process_infos(
                event.entity_id,
                notify=True,
            )

        event.message = (
            "Vorgangsinformationen verarbeitet"
        )

        audit(
            "webhook.wowiport",
            "crafts_process",
            event.entity_id,
            {
                "action": event.action,
                "event_id": event.id,
            },
        )
        return

    if (event.entity_type or "").casefold() != "commission":
        event.message = "Ignorierte Entität"
        return

    if event.entity_id is None:
        raise RuntimeError("Webhook enthält keine Auftrags-ID.")

    if (event.action or "").casefold() == "deleted":
        commission = mark_commission_deleted(event.entity_id)
    else:
        commission = sync_commission_from_erp(event.entity_id)

    event.message = (
        "Verarbeitet"
        if commission is not None
        else "Nicht relevant"
    )

    audit(
        "webhook.wowiport",
        "commission",
        event.entity_id,
        {
            "action": event.action,
            "event_id": event.id,
        },
    )


def finish_webhook_event(event_id: int) -> None:
    event = db.session.get(WebhookEvent, event_id)

    if event is None:
        return

    event.processed = True
    event.processed_at = utc_now()
    event.processing_at = None
    event.next_attempt_at = None
    db.session.commit()


def fail_webhook_event(
    event_id: int,
    error: Exception,
) -> None:
    db.session.rollback()
    event = db.session.get(WebhookEvent, event_id)

    if event is None:
        return

    delay_seconds = min(
        30 * (2 ** min(max(event.attempt_count - 1, 0), 7)),
        3600,
    )

    event.processing_at = None
    event.next_attempt_at = utc_now() + timedelta(seconds=delay_seconds)
    event.message = str(error)[:4000]
    db.session.commit()


def require_erp_id(commission: Commission) -> None:
    if commission.erp_id is None:
        raise RuntimeError("Der Auftrag besitzt noch keine OPENWOWI-ID.")


def run_worker(
    poll_seconds: int,
) -> None:
    last_heartbeat = None

    while True:
        current_time = monotonic()

        if (
            last_heartbeat is None
            or current_time - last_heartbeat >= 60
        ):
            heartbeat = db.session.get(
                HealthHeartbeat,
                "worker",
            )

            if heartbeat is None:
                heartbeat = HealthHeartbeat(
                    name="worker",
                    last_seen_at=utc_now(),
                )
                db.session.add(heartbeat)
            else:
                heartbeat.last_seen_at = utc_now()

            db.session.commit()
            last_heartbeat = monotonic()

        result = process_due_work()

        if (
            result["webhooks"] == 0
            and result["outbox"] == 0
            and result["process_infos"] == 0
            and result["push"] == 0
            and result["external_assignment_emails"] == 0
            and result["tenant_notifications"] == 0
            and result["completion_notifications"] == 0
            and result["dms"] == 0
        ):
            sleep(poll_seconds)
