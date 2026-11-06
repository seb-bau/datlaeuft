from __future__ import annotations

from functools import wraps

from flask import Blueprint, current_app, jsonify
from sqlalchemy import case, func, or_, select
from sqlalchemy.exc import SQLAlchemyError

from .extensions import db
from .models import (
    Commission,
    CommissionCompletionNotification,
    CommissionTenantNotification,
    DmsDocument,
    ErpOutboxOperation,
    ExternalCommissionAssignmentMail,
    HealthHeartbeat,
    ProcessInfoDelivery,
    ProcessInfoThread,
    PushDelivery,
    SafetyInspectionProtocol,
    TicketMirror,
    WebhookEvent,
    utc_now,
)
from .settings import SettingKeys, settings
from .utils import ensure_utc
from .webhooks import valid_bearer_token
from .health_rules import CHANNELS, evaluate_health_metrics, health_sensor_text


health_bp = Blueprint(
    "health",
    __name__,
    url_prefix="/health",
)


def protected(view):
    @wraps(view)
    def wrapped():
        if not valid_bearer_token():
            return jsonify({"message": "Nicht autorisiert."}), 401

        try:
            return view()
        except SQLAlchemyError:
            db.session.rollback()
            current_app.logger.exception("health_query_failed")
            return jsonify({
                "status": "Störung",
                "message": "Datenbankabfrage fehlgeschlagen.",
            }), 503

    return wrapped


@health_bp.after_request
def disable_caching(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def age_seconds(now, timestamp):
    if timestamp is None:
        return 0

    return max(
        0,
        int((now - ensure_utc(timestamp)).total_seconds()),
    )


def worker_age_seconds(now):
    last_seen_at = db.session.execute(
        select(HealthHeartbeat.last_seen_at).where(
            HealthHeartbeat.name == "worker"
        )
    ).scalar_one_or_none()

    if last_seen_at is None:
        return -1

    return age_seconds(now, last_seen_at)


def state_rows(state_column, created_column, states):
    rows = db.session.execute(
        select(
            state_column,
            func.count(),
            func.min(created_column),
        )
        .where(state_column.in_(states))
        .group_by(state_column)
    )

    return {
        state: (int(count), oldest_at)
        for state, count, oldest_at in rows
    }


def oldest_age_seconds(now, rows, states):
    timestamps = [
        rows[state][1]
        for state in states
        if state in rows and rows[state][1] is not None
    ]

    if not timestamps:
        return 0

    oldest_at = min(timestamps, key=ensure_utc)
    return age_seconds(now, oldest_at)


def count_where(model, condition):
    return int(
        db.session.execute(
            select(func.count())
            .select_from(model)
            .where(condition)
        ).scalar_one()
    )


def status_text(status_code):
    return ("In Ordnung", "Warnung", "Störung")[status_code]


def live_snapshot():
    now = utc_now()
    metrics = {
        "checked_at": now.isoformat(),
        "worker_age_seconds": worker_age_seconds(now),
    }
    metrics.update(evaluate_health_metrics(
        metrics, settings.get(SettingKeys.HEALTH_RULES)
    ))
    return metrics


def full_snapshot():
    now = utc_now()
    worker_age = worker_age_seconds(now)

    webhook_total, webhook_failed, webhook_oldest = (
        db.session.execute(
            select(
                func.count(),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                WebhookEvent.message.is_not(None),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.min(WebhookEvent.received_at),
            ).where(WebhookEvent.processed.is_(False))
        ).one()
    )

    erp_rows = state_rows(
        ErpOutboxOperation.state,
        ErpOutboxOperation.created_at,
        ("pending", "processing", "failed"),
    )

    commission_conditions = (
        Commission.erp_sync_state == "failed",
        Commission.document_sync_error.is_not(None),
        Commission.media_sync_error.is_not(None),
    )

    commission_counts = db.session.execute(
        select(
            *(
                func.coalesce(
                    func.sum(
                        case((condition, 1), else_=0)
                    ),
                    0,
                )
                for condition in commission_conditions
            ),
            func.coalesce(
                func.sum(
                    case(
                        (or_(*commission_conditions), 1),
                        else_=0,
                    )
                ),
                0,
            ),
        )
        .select_from(Commission)
        .where(Commission.deleted_at.is_(None))
    ).one()

    dms_rows = state_rows(
        DmsDocument.status,
        DmsDocument.created_at,
        (
            "pending",
            "processing",
            "failed",
            "uncertain",
            "blocked",
        ),
    )

    push_rows = state_rows(
        PushDelivery.state,
        PushDelivery.created_at,
        ("pending", "failed", "discarded"),
    )

    def value(rows, state):
        return rows.get(state, (0, None))[0]

    dms_waiting_states = (
        "pending",
        "processing",
        "failed",
        "uncertain",
    )

    metrics = {
        "checked_at": now.isoformat(),
        "worker_age_seconds": worker_age,
        "webhooks_unprocessed": int(webhook_total),
        "webhooks_failed": int(webhook_failed),
        "webhooks_oldest_seconds": age_seconds(
            now,
            webhook_oldest,
        ),
        "erp_pending": value(erp_rows, "pending"),
        "erp_processing": value(erp_rows, "processing"),
        "erp_failed": value(erp_rows, "failed"),
        "erp_oldest_seconds": oldest_age_seconds(
            now,
            erp_rows,
            ("pending", "processing", "failed"),
        ),
        "commissions_failed": int(commission_counts[0]),
        "documents_failed": int(commission_counts[1]),
        "media_failed": int(commission_counts[2]),
        "synchronizations_failed": int(
            commission_counts[3]
        ),
        "dms_enabled": int(
            settings.get(SettingKeys.DMS_ENABLED)
        ),
        "documents_in_queue": sum(
            value(dms_rows, state)
            for state in dms_waiting_states
        ),
        "dms_pending": value(dms_rows, "pending"),
        "dms_processing": value(dms_rows, "processing"),
        "dms_failed": value(dms_rows, "failed"),
        "dms_uncertain": value(dms_rows, "uncertain"),
        "dms_blocked": value(dms_rows, "blocked"),
        "dms_upload_errors": sum(
            value(dms_rows, state)
            for state in (
                "failed",
                "uncertain",
                "blocked",
            )
        ),
        "dms_oldest_seconds": oldest_age_seconds(
            now,
            dms_rows,
            dms_waiting_states,
        ),
        "push_pending": value(push_rows, "pending"),
        "push_failed": value(push_rows, "failed"),
        "push_discarded": value(
            push_rows,
            "discarded",
        ),
        "tenant_mail_failed": count_where(
            CommissionTenantNotification,
            CommissionTenantNotification.state
            == "failed",
        ),
        "completion_mail_failed": count_where(
            CommissionCompletionNotification,
            CommissionCompletionNotification.state
            == "failed",
        ),
        "external_mail_failed": count_where(
            ExternalCommissionAssignmentMail,
            ExternalCommissionAssignmentMail.state
            == "failed",
        ),
        "process_delivery_failed": count_where(
            ProcessInfoDelivery,
            ProcessInfoDelivery.state == "failed",
        ),
        "process_sync_failed": count_where(
            ProcessInfoThread,
            ProcessInfoThread.last_error.is_not(None),
        ),
        "ticket_ai_failed": count_where(
            TicketMirror,
            TicketMirror.ai_state == "failed",
        ),
        "safety_archive_failed": count_where(
            SafetyInspectionProtocol,
            SafetyInspectionProtocol.dms_archive_status
            == "failed",
        ),
    }

    metrics.update(evaluate_health_metrics(
        metrics, settings.get(SettingKeys.HEALTH_RULES)
    ))
    return metrics


def prtg_response(metrics):
    results = []

    for key, label in CHANNELS:
        if key not in metrics:
            continue

        result = {
            "channel": label,
            "value": metrics[key],
            "unit": (
                "TimeSeconds"
                if key.endswith("_seconds")
                else "Count"
            ),
        }

        if key == "status_code":
            result.update({
                "limitmode": 1,
                "limitmaxwarning": 0,
                "limitmaxerror": 1,
            })

        results.append(result)

    return jsonify({
        "prtg": {
            "result": results,
            "text": health_sensor_text(metrics),
        }
    })


@health_bp.get("/live")
@protected
def live():
    return prtg_response(live_snapshot())


@health_bp.get("")
@protected
def overview():
    return jsonify(full_snapshot())


@health_bp.get("/prtg")
@protected
def prtg():
    return prtg_response(full_snapshot())