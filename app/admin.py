from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from functools import partial
from werkzeug.utils import secure_filename

from io import BytesIO
from typing import Any
import json
import qrcode
from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    url_for,
)
from .safety_inspection_service import (
    reconcile_safety_inspection_assignments,
)
from flask_login import current_user, login_required

from .audit import audit
from .auth import (
    admin_required,
    commission_creation_required,
    commission_planning_required,
    foreman_required,
    office_review_required,
    invoice_required,
    offer_required,
)
from .entra import import_entra_users
from .extensions import db
from .logging_service import configure_logging
from .settings import all_definitions, grouped_definitions, settings, SettingKeys
from .catalog import sync_catalogs_from_wowiport
from .object_directory import (
    commission_creation_tasks,
    get_commission_object_choice,
    resolve_commission_assignment,
    resolve_commission_object,
    resolve_commission_reporter,
    search_commission_reporters,
    search_commission_objects,
    search_invoice_contracts,
    sync_object_directory,
    commission_service_positions,
)
from sqlalchemy import (
    and_,
    case,
    or_,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from .commission_service import create_pending_commission
from .attachment_service import MAX_ATTACHMENT_BYTES, commission_attachment_path, store_commission_attachment_bytes
from .ticket_attachments import (
    prepare_ticket_attachment_selection, read_ticket_attachment, render_ticket_photo,
    store_selected_ticket_attachments, ticket_attachment_choices,
)
from .models import (
    WorkflowNotification,
    Commission,
    CommissionLocalStatus,
    CommissionSource,
    CraftActivity,
    InventoryItem,
    InventoryMovement,
    InventoryMovementType,
    MaterialSource,
    MaterialUsageStatus,
    ReviewLevel,
    SafetyInspectionGroup,
    SafetyInspectionJob,
    SafetyInspectionTemplate,
    SafetyInspectionTemplateVersion,
    TimeEntry,
    TimeReviewStatus,
    TimeTaskExpectedMaterial,
    TimeTaskTemplate,
    User,
    WowiCommissionType,
    utc_now,
    WowiCompanyCode,
    InventoryLocation,
    InventoryLocationType,
    InventoryItemBarcode,
    AppointmentWindow,
    AuditLog,
    CommissionAppointmentRequest,
    CommissionPhoto,
    CommissionAttachment,
    CommissionSyncState,
    ErpOperationType,
    ErpOutboxOperation,
    ErpOutboxState,
    WebhookEvent,
    PushEventType,
    CraftActivityDepartmentMapping,
    WowiDepartment,
    InvoiceBillingRule,
    InvoiceBillingRuleType,
    Invoice,
    InvoiceCommission,
    InvoiceStatus,
    InvoiceRecipientType,
    WowiLicenseAgreement,
    InvoicePositionSource,
    InvoiceArchiveStatus,
    InvoiceKind,
    OfferCraftFallback,
    Offer,
    OfferPositionSource,
    OfferStatus,
    OfferArchiveStatus,
    UserJurisdictionPersonMapping,
    WowiEconomicUnit,
    WowiEconomicUnitJurisdiction,
    ProcessInfo,
    CommissionTicketLink,
    TicketAiState,
    TicketMirror,
    TicketMirrorState,
    TicketCraftFallback,
    TicketAiAssignmentMode,
    TicketAiAssignmentRule,
    InventoryItemGroup,
    InventoryItemType,
    UserWorkTimeProfileAssignment,
    WorkTimeProfile,
    WorkTimeTargetOverride,
    ExternalCraftsman,
    ExternalCraftsmanAccessToken,
    CommissionAssignmentType,
    WowiResponsibleOfficial,
    TicketAiJob,
)
from .external_craftsman_service import (
    automatic_external_craftsman,
    available_external_craftsmen,
    create_invitation_token,
    default_external_username,
    send_external_invitation_email,
    sync_external_craftsmen_from_wowiport,
)
from .external_order_responses import validate_external_communication
from .ticket_service import (
    ticket_license_agreement_local_id,
    ticket_list_object_details,
    mark_ticket_analyses_stale,
    ticket_analysis_input_hash,
)
from .ticket_filters import open_tickets_query
from .work_time_service import (
    current_work_time_assignment,
    work_time_period_bounds,
    work_time_rows,
)
from .offer_archive import (
    OfferArchiveError,
    archive_offer,
)
from .invoice_archive import (
    InvoiceArchiveError,
    archive_invoice,
)
from .inventory_service import (
    book_inventory_movement,
    get_default_inventory_location,
    get_inventory_location,
)
from .photo_service import (
    commission_photo_path,
    store_commission_photo,
)
from .erp_sync import enqueue_erp_operation
from .push_service import enqueue_commission_push
from .wowiport import (
    configured_commission_type_ids,
    sync_commission_from_erp,
    WowiportService,
)
from .invoice_service import (
    add_invoice_suggestions,
    assign_invoice_agreement,
    available_invoice_commissions,
    create_agreement_invoice_draft,
    create_commission_invoice_draft,
    create_free_invoice_draft,
    invoice_agreement_companies,
    remove_invoice_agreement,
    replace_invoice_positions,
    InvoiceFinalizationError,
    finalize_invoice,
    create_cancellation_invoice,
)
from .invoice_document import (
    InvoiceDocumentError,
    create_invoice_document,
    register_invoice_archive_pdf,
    remove_invoice_documents,
    resolve_invoice_document_path,
)
from .work_report_document import (
    WORK_REPORT_SCOPE_ALL,
    WORK_REPORT_SCOPE_BILLED,
    WorkReportDocumentError,
    commission_work_report_entries,
    create_invoice_with_work_reports_pdf,
    create_work_report_pdf,
    invoice_work_report_groups,
    invoice_work_reports_filename,
    work_report_counts,
    work_report_filename,
    work_report_fingerprint,
)
from .offer_document import (
    OfferDocumentError,
    resolve_offer_document_path,
)
from .offer_service import (
    OfferFinalizationError,
    accept_offer,
    cancel_offer,
    copy_offer_to_draft,
    create_offer_draft,
    offer_commission_draft_data,
    reject_offer,
    release_offer,
    replace_offer_positions,
)
from datetime import date, time, timedelta
from uuid import uuid4

from .commission_priority import set_manual_commission_priority
from .commission_links import commission_wowiport_url
from .commission_object_history import commission_object_history
from .commission_approval import approve_commission, approval_user_choices, can_approve_commission, save_user_approval_settings
from .commission_followup import can_handle_commission_review, review_commission, resolve_commission_review, commission_reissue_data, copy_commission_files
from .commission_workflow_service import CommissionWorkflowError, cancel_commission
from .push_preferences import admin_push_preference_rows, save_admin_push_preferences
from .workflow_notifications import overdue_commission_filter
from .ticket_replies import enqueue_ticket_reply, ticket_reply_recipients
from .utils import commission_detail_url, commission_list_return_url, format_local_datetime, parse_local_datetime, ticket_list_return_url

from .process_info_service import (
    create_office_info,
    sync_process_infos,
    write_outgoing_info,
)

from .performance_review_service import (
    performance_review_rows,
    review_performance_scope,
)

from .reporting_service import (
    ReportPeriod,
    build_craftsman_commission_report,
    build_installed_material_report,
    build_service_position_report,
    resolve_reporting_period,
)

from .catalog import (
    sync_catalogs_from_wowiport,
    sync_craft_activities_from_wowiport,
)

from .appointment_service import format_manual_appointment
from .blacklist import blacklist_ids, include_blacklisted, is_blacklisted
from .document_articles import attach_article_fields
from .document_previews import render_document_preview
from .invoice_payments import invoice_payment_information, payment_options_from_wowiport, save_invoice_payment_form
from .tenant_billing import (
    choose_contract_recipient,
    contract_recipients,
    tenant_billing_agreement,
    tenant_billing_issuer,
)
from .ticket_ai_service import (
    enqueue_ticket_ai, ticket_ai_enabled, ticket_ai_is_current,
    ticket_ai_lease_seconds,
)

from math import ceil
from zoneinfo import ZoneInfo
from sqlalchemy import func
from .models import CommissionTenantNotification, HealthHeartbeat
from .utils import ensure_utc

admin_bp = Blueprint("admin", __name__)

WORK_TIME_WEEKDAYS = (
    ("monday", "Montag"),
    ("tuesday", "Dienstag"),
    ("wednesday", "Mittwoch"),
    ("thursday", "Donnerstag"),
    ("friday", "Freitag"),
    ("saturday", "Samstag"),
    ("sunday", "Sonntag"),
)

INVOICE_TIME_TYPES = (
    {
        "key": "work_minutes",
        "label": "Arbeitszeit",
    },
    {
        "key": "waiting_minutes",
        "label": "Wartezeit",
    },
    {
        "key": "material_minutes",
        "label": "Materialbeschaffung",
    },
    {
        "key": "travel_minutes",
        "label": "Fahrtzeit",
    },
    {
        "key": "documentation_minutes",
        "label": "Dokumentationszeit",
    },
    {
        "key": "extra_minutes",
        "label": "Zusatzzeit",
    },
)


OFFER_STATUS_LABELS = {
    OfferStatus.DRAFT.value:
        "Entwurf",
    OfferStatus.AWAITING_RESPONSE.value:
        "Warte auf Rückmeldung",
    OfferStatus.IN_PROGRESS.value:
        "In Bearbeitung",
    OfferStatus.READY_FOR_INVOICE.value:
        "Bereit zur Rechnungsstellung",
    OfferStatus.REVIEW_REQUIRED.value:
        "Prüfung erforderlich",
    OfferStatus.INVOICED.value:
        "Abgerechnet",
    OfferStatus.REJECTED.value:
        "Abgelehnt",
    OfferStatus.EXPIRED.value:
        "Abgelaufen",
    OfferStatus.CANCELED.value:
        "Storniert",
}


@admin_bp.get("/")
@login_required
def root() -> Any:
    return redirect(url_for("admin.dashboard"))


def commission_review_query():
    query = Commission.query.filter(
        Commission.deleted_at.is_(None),
        Commission.local_status != CommissionLocalStatus.CANCELED.value,
        Commission.review_resolved_at.is_(None),
        or_(Commission.local_status.in_([
            CommissionLocalStatus.COMPLETION_REVIEW.value,
            CommissionLocalStatus.TENANT_CHARGE_REVIEW.value,
        ]), overdue_commission_filter(), Commission.approval_state == "pending"),
    )

    if not current_user.can_review_work:
        return query.filter(Commission.id.is_(None))
    if current_user.is_admin:
        return query

    if current_user.is_foreman:
        return query.filter(
            or_(
                Commission.created_by_user_id
                == current_user.id,
                Commission.created_by_user_id.is_(None),
                Commission.local_official_user_id == current_user.id,
                and_(Commission.approval_state == "pending", Commission.approval_approver_user_id == current_user.id),
            )
        )

    return query.filter(
        Commission.id.is_(None)
    )


def safe_next_url(
    default_endpoint: str,
) -> str:
    next_url = request.form.get(
        "next",
        "",
    ).strip()

    if (
        next_url.startswith("/")
        and not next_url.startswith("//")
    ):
        return next_url

    return url_for(default_endpoint)


@admin_bp.get("/admin")
@login_required
def dashboard() -> Any:
    my_invoice_count = 0
    if current_user.is_admin or current_user.can_manage_invoices:
        my_invoice_count = Invoice.query.filter(
            Invoice.responsible_user_id == current_user.id,
            Invoice.status.in_((InvoiceStatus.DRAFT.value, InvoiceStatus.FINALIZED.value)),
            Invoice.completed_at.is_(None),
        ).count()
    my_open_ticket_count = 0
    if current_user.is_admin or current_user.is_foreman:
        my_open_ticket_count = open_tickets_query(TicketMirror.query).filter(
            TicketMirror.assigned_user_erp_id.in_(ticket_owner_erp_ids(current_user))
        ).count()

    open_count = Commission.query.filter(
        Commission.deleted_at.is_(None),
        Commission.review_resolved_at.is_(None),
        Commission.local_status.notin_([
            CommissionLocalStatus.DONE.value, CommissionLocalStatus.CANCELED.value, CommissionLocalStatus.DELETED.value,
        ]),
    ).count()

    review_count = TimeEntry.query.filter_by(
        review_status=TimeReviewStatus.NEEDS_REVIEW.value
    ).count()

    material_review_count = InventoryMovement.query.filter_by(
        review_status=TimeReviewStatus.NEEDS_REVIEW.value
    ).count()

    low_stock_count = InventoryItem.query.filter(
        InventoryItem.stock_quantity
        <= InventoryItem.reorder_level
    ).count()

    commission_reviews = commission_review_query()

    completion_review_count = commission_reviews.filter(
        Commission.rejected_at.is_(None),
        Commission.local_status
        == CommissionLocalStatus.COMPLETION_REVIEW.value
    ).count()
    rejected_review_count = commission_reviews.filter(Commission.rejected_at.is_not(None)).count()

    tenant_charge_review_count = commission_reviews.filter(
        Commission.local_status
        == CommissionLocalStatus.TENANT_CHARGE_REVIEW.value
    ).count()
    overdue_review_count = commission_reviews.filter(overdue_commission_filter()).count()
    approval_review_count = commission_reviews.filter(Commission.approval_state == "pending").count()

    sync_problem_count = 0

    if current_user.is_admin:
        failed_outbox_count = (
            ErpOutboxOperation.query
            .filter(
                ErpOutboxOperation.state
                == ErpOutboxState.FAILED.value
            )
            .count()
        )

        failed_webhook_count = (
            WebhookEvent.query
            .filter(
                WebhookEvent.processed.is_(False),
                WebhookEvent.attempt_count > 0,
            )
            .count()
        )

        commission_sync_error_count = (
            Commission.query
            .filter(
                or_(
                    Commission.erp_sync_state
                    == CommissionSyncState.FAILED.value,
                    Commission.media_sync_error.isnot(None),
                    Commission.document_sync_error.isnot(None),
                )
            )
            .count()
        )

        sync_problem_count = (
            failed_outbox_count
            + failed_webhook_count
            + commission_sync_error_count
            + CommissionTenantNotification.query.filter_by(
                state="failed"
            ).count()
        )

    return render_template(
        "admin/dashboard.html",
        my_invoice_count=my_invoice_count,
        my_open_ticket_count=my_open_ticket_count,
        open_count=open_count,
        rejected_review_count=rejected_review_count,
        review_count=review_count,
        material_review_count=material_review_count,
        low_stock_count=low_stock_count,
        completion_review_count=completion_review_count,
        tenant_charge_review_count=tenant_charge_review_count,
        overdue_review_count=overdue_review_count,
        approval_review_count=approval_review_count,
        sync_problem_count=sync_problem_count,
    )


TENANT_SYNC_FILTERS = {
    "active": "Offene und fehlgeschlagene",
    "all": "Alle",
    "pending": "Wartend",
    "sending": "In Bearbeitung",
    "failed": "Fehlgeschlagen",
    "sent": "Abgeschlossen",
    "no_recipients": "Ohne E-Mail-Adresse",
    "discarded": "Verworfen",
}


def _tenant_sync_time(value):
    return (
        ensure_utc(value).astimezone(ZoneInfo("Europe/Berlin"))
        if value else None
    )


def _tenant_notification_progress(item):
    payload = item.payload if isinstance(item.payload, dict) else {}
    recipients = payload.get("recipients")
    recipients = recipients if isinstance(recipients, list) else []
    delivered = payload.get("delivered")
    delivered = delivered if isinstance(delivered, list) else []
    delivered_count = sum(address in delivered for address in recipients)

    return {
        "payload": payload,
        "recipients": recipients,
        "delivered_count": delivered_count,
        "complete": bool(recipients) and delivered_count == len(recipients),
    }


def _tenant_notification_sync_row(item, now, writeback_enabled):
    progress = _tenant_notification_progress(item)
    payload = progress["payload"]
    complete = progress["complete"]
    state = item.state

    queue_labels = {
        "pending": ("Wartend", "secondary"),
        "sending": ("In Bearbeitung", "primary"),
        "failed": ("Vorbereitung oder Versand fehlgeschlagen", "danger"),
        "sent": ("Abgeschlossen", "success"),
        "no_recipients": ("Ohne E-Mail-Adresse", "warning"),
        "discarded": ("Verworfen", "secondary"),
    }

    queue_label, queue_color = queue_labels.get(
        state, ("Unbekannter Status", "warning")
    )

    if complete and state in {"pending", "sending", "failed"}:
        queue_label = {
            "pending": "Nachbearbeitung eingeplant",
            "sending": "Nachbearbeitung läuft",
            "failed": "Nachbearbeitung fehlgeschlagen",
        }[state]

    if complete:
        mail_label = (
            "Übergeben (mit Debug-Umleitung)"
            if payload.get("debug_delivery")
            else "An Mailserver übergeben"
        )
        mail_color = "success"
    elif progress["delivered_count"]:
        mail_label, mail_color = "Teilweise übergeben", "warning"
    elif state == "no_recipients":
        mail_label, mail_color = "Keine Adresse vorhanden", "warning"
    elif state == "discarded":
        mail_label, mail_color = "Nicht versendet", "secondary"
    else:
        mail_label, mail_color = "Noch nicht übergeben", "secondary"

    tickets = payload.get("tickets")
    tickets = tickets if isinstance(tickets, list) else None
    documented_count = sum(
        bool(ticket.get("comment_id"))
        for ticket in tickets or []
        if isinstance(ticket, dict)
    )
    skipped_count = sum(
        ticket.get("writeback_skipped") is True and not ticket.get("comment_id")
        for ticket in tickets or []
        if isinstance(ticket, dict)
    )
    ticket_count = len(tickets) if tickets is not None else None

    if ticket_count and documented_count == ticket_count:
        ticket_label, ticket_color = "Dokumentiert", "success"
    elif ticket_count and skipped_count == ticket_count:
        ticket_label, ticket_color = "Per Einstellung ausgenommen", "secondary"
    elif ticket_count and documented_count + skipped_count == ticket_count:
        ticket_label, ticket_color = "Abgeschlossen mit Ausnahmen", "success"
    elif not writeback_enabled:
        ticket_label, ticket_color = "Derzeit deaktiviert", "secondary"
    elif tickets is None:
        ticket_label, ticket_color = "Noch nicht ermittelt", "secondary"
    elif not tickets:
        ticket_label, ticket_color = "Kein Ticket verknüpft", "secondary"
    elif complete and state == "failed":
        ticket_label, ticket_color = "Fehlgeschlagen", "danger"
    elif state == "sent":
        ticket_label, ticket_color = "Keine Dokumentation gespeichert", "secondary"
    elif complete:
        ticket_label, ticket_color = "Ausstehend", "warning"
    else:
        ticket_label, ticket_color = "Nach dem E-Mail-Versand", "secondary"

    actual_recipients = payload.get("delivery_recipients")
    if not isinstance(actual_recipients, list):
        actual_recipients = (
            [] if payload.get("debug_delivery")
            else payload.get("delivered", [])
        )
    if not isinstance(actual_recipients, list):
        actual_recipients = []

    available_at = ensure_utc(item.available_at) if item.available_at else None
    waiting = state in {"pending", "failed"}
    remaining_minutes = (
        max(1, ceil((available_at - now).total_seconds() / 60))
        if waiting and available_at and available_at > now
        else None
    )

    commission = item.commission

    return {
        "id": item.id,
        "commission_id": item.commission_id,
        "commission_label": (
            commission.erp_idnum or commission.code or str(commission.id)
        ) if commission else str(item.commission_id),
        "queue_label": queue_label,
        "queue_color": queue_color,
        "mail_label": mail_label,
        "mail_color": mail_color,
        "recipients": progress["recipients"],
        "delivered_count": progress["delivered_count"],
        "recipient_count": len(progress["recipients"]),
        "actual_recipients": actual_recipients,
        "debug_delivery": bool(payload.get("debug_delivery")),
        "sent_at": _tenant_sync_time(item.sent_at),
        "mail_complete": complete,
        "ticket_label": ticket_label,
        "ticket_color": ticket_color,
        "ticket_count": ticket_count,
        "documented_count": documented_count,
        "skipped_count": skipped_count,
        "attempt_count": item.attempt_count,
        "next_attempt_at": _tenant_sync_time(available_at) if waiting else None,
        "remaining_minutes": remaining_minutes,
        "due": bool(waiting and available_at and available_at <= now),
        "locked_at": (
            _tenant_sync_time(item.locked_at)
            if state == "sending" else None
        ),
        "last_error": item.last_error,
        "can_retry": state == "failed",
    }


def tenant_notification_sync_context():
    now = ensure_utc(utc_now())
    selected_filter = request.args.get("tenant_state", "active")

    if selected_filter not in TENANT_SYNC_FILTERS:
        selected_filter = "active"

    query = CommissionTenantNotification.query.options(
        selectinload(CommissionTenantNotification.commission)
    )

    if selected_filter == "active":
        query = query.filter(
            CommissionTenantNotification.state.in_(
                ("pending", "sending", "failed")
            )
        )
    elif selected_filter != "all":
        query = query.filter(
            CommissionTenantNotification.state == selected_filter
        )

    if selected_filter in {"all", "sent", "no_recipients", "discarded"}:
        query = query.order_by(CommissionTenantNotification.id.desc())
    else:
        query = query.order_by(
            CommissionTenantNotification.available_at.asc(),
            CommissionTenantNotification.id.asc(),
        )

    page_number = max(1, request.args.get("tenant_page", 1, type=int))
    pagination = query.paginate(
        page=page_number,
        per_page=25,
        error_out=False,
    )

    if pagination.pages and pagination.page > pagination.pages:
        pagination = query.paginate(
            page=pagination.pages,
            per_page=25,
            error_out=False,
        )

    counts = dict(
        CommissionTenantNotification.query.with_entities(
            CommissionTenantNotification.state,
            func.count(CommissionTenantNotification.id),
        )
        .group_by(CommissionTenantNotification.state)
        .all()
    )

    writeback_enabled = bool(
        settings.get(SettingKeys.TENANT_TICKET_WRITEBACK_ENABLED)
    )

    heartbeat = db.session.get(HealthHeartbeat, "worker")
    last_seen_at = ensure_utc(heartbeat.last_seen_at) if heartbeat else None
    worker_age = (
        max(0, (now - last_seen_at).total_seconds())
        if last_seen_at else None
    )

    from .health_rules import evaluate_health_metrics

    worker_check = evaluate_health_metrics(
        {"worker_age_seconds": -1 if worker_age is None else int(worker_age)},
        settings.get(SettingKeys.HEALTH_RULES),
    )["checks"][0]
    worker_label, worker_color = worker_check["status"], worker_check["color"]

    return {
        "rows": [
            _tenant_notification_sync_row(item, now, writeback_enabled)
            for item in pagination.items
        ],
        "pagination": pagination,
        "selected_filter": selected_filter,
        "filter_choices": TENANT_SYNC_FILTERS,
        "summary": [
            (TENANT_SYNC_FILTERS[state], counts.get(state, 0))
            for state in (
                "pending", "sending", "failed",
                "sent", "no_recipients", "discarded",
            )
        ],
        "checked_at": _tenant_sync_time(now),
        "writeback_enabled": writeback_enabled,
        "worker_label": worker_label,
        "worker_color": worker_color,
        "worker_seen_at": _tenant_sync_time(last_seen_at),
    }


@admin_bp.post(
    "/admin/sync-health/tenant-notifications/<int:notification_id>/retry"
)
@admin_required
def sync_health_retry_tenant_notification(notification_id: int) -> Any:
    item = db.get_or_404(CommissionTenantNotification, notification_id)

    changed = CommissionTenantNotification.query.filter(
        CommissionTenantNotification.id == notification_id,
        CommissionTenantNotification.state == "failed",
    ).update({
        CommissionTenantNotification.state: "pending",
        CommissionTenantNotification.available_at: utc_now(),
        CommissionTenantNotification.locked_at: None,
    }, synchronize_session=False)

    if changed == 1:
        audit(
            "tenant_notification.retry",
            "commission",
            item.commission_id,
            {
                "notification_id": item.id,
                "mail_complete": _tenant_notification_progress(item)["complete"],
            },
        )
        db.session.commit()
        flash(
            "Die Benachrichtigung wurde erneut eingeplant. "
            "Bereits gespeicherte Versandfortschritte bleiben erhalten.",
            "success",
        )
    else:
        db.session.rollback()
        flash(
            "Der Eintrag ist nicht mehr fehlgeschlagen "
            "oder wird bereits verarbeitet.",
            "warning",
        )

    selected_filter = request.form.get("tenant_state", "active")
    if selected_filter not in TENANT_SYNC_FILTERS:
        selected_filter = "active"

    page_number = max(1, request.form.get("tenant_page", 1, type=int))

    return redirect(url_for(
        "admin.sync_health",
        tenant_state=selected_filter,
        tenant_page=page_number,
        _anchor="tenant-notifications",
    ))


TICKET_AI_SYNC_STATES = {
    "queued": ("Wartend", "secondary"),
    "processing": ("In Bearbeitung", "info"),
    "stalled": ("Überfällig", "warning"),
    "failed": ("Fehlgeschlagen", "danger"),
    "missing": ("Nicht eingeplant", "warning"),
}


def ticket_ai_sync_context():
    now = ensure_utc(utc_now())
    lease_seconds = ticket_ai_lease_seconds()
    selected_filter = request.args.get("ai_state", "all")
    if selected_filter not in TICKET_AI_SYNC_STATES:
        selected_filter = "all"

    queue_state = case(
        (
            TicketAiJob.ticket_id.is_(None),
            case(
                (TicketMirror.ai_state == TicketAiState.FAILED.value, "failed"),
                else_="missing",
            ),
        ),
        (
            TicketAiJob.locked_at < now - timedelta(seconds=lease_seconds),
            "stalled",
        ),
        (TicketAiJob.locked_at.is_not(None), "processing"),
        (TicketAiJob.attempt_count >= 3, "failed"),
        else_="queued",
    )
    query = TicketMirror.query.outerjoin(
        TicketAiJob, TicketAiJob.ticket_id == TicketMirror.id
    ).filter(or_(
        TicketAiJob.ticket_id.is_not(None),
        TicketMirror.ai_state.in_((
            TicketAiState.QUEUED.value,
            TicketAiState.PROCESSING.value,
            TicketAiState.FAILED.value,
        )),
    ))
    counts = dict(query.with_entities(
        queue_state, func.count(TicketMirror.id)
    ).group_by(queue_state).all())
    if selected_filter != "all":
        query = query.filter(queue_state == selected_filter)

    query = query.add_entity(TicketAiJob).add_columns(
        queue_state.label("queue_state")
    ).order_by(TicketAiJob.available_at.asc(), TicketMirror.id.asc())
    pagination = query.paginate(
        page=max(1, request.args.get("ai_page", 1, type=int)),
        per_page=25,
        error_out=False,
    )
    if pagination.pages and pagination.page > pagination.pages:
        pagination = query.paginate(
            page=pagination.pages, per_page=25, error_out=False
        )

    heartbeat = db.session.get(HealthHeartbeat, "ticket_ai_worker")
    last_seen_at = ensure_utc(heartbeat.last_seen_at) if heartbeat else None
    if last_seen_at is None:
        worker_label, worker_color = "Noch keine Rückmeldung", "warning"
    elif (now - last_seen_at).total_seconds() > lease_seconds:
        worker_label, worker_color = "Rückmeldung überfällig", "danger"
    else:
        worker_label, worker_color = "Rückmeldung aktuell", "success"

    def page_url(page):
        return url_for(
            "admin.sync_health",
            ai_state=selected_filter,
            ai_page=page,
            tenant_state=request.args.get("tenant_state", "active"),
            tenant_page=max(1, request.args.get("tenant_page", 1, type=int)),
            _anchor="ticket-ai",
        )

    return {
        "rows": [
            {
                "ticket": ticket,
                "label": TICKET_AI_SYNC_STATES[state][0],
                "color": TICKET_AI_SYNC_STATES[state][1],
                "attempt_count": job.attempt_count if job else None,
                "mode": ("Automatisch" if job.automatic else "Manuell") if job else "–",
                "locked_at": _tenant_sync_time(job.locked_at) if job else None,
                "due": (
                    job is not None and state == "queued"
                    and ensure_utc(job.available_at) <= now
                ),
                "next_attempt_at": (
                    _tenant_sync_time(job.available_at)
                    if job and state == "queued" else None
                ),
                "can_open": (
                    ticket.deleted_at is None
                    and ticket.state != TicketMirrorState.IGNORED.value
                ),
            }
            for ticket, job, state in pagination.items
        ],
        "summary": [
            (label, counts.get(state, 0))
            for state, (label, color) in TICKET_AI_SYNC_STATES.items()
        ],
        "filter_choices": {
            "all": "Alle offenen und fehlgeschlagenen",
            **{state: label for state, (label, color) in TICKET_AI_SYNC_STATES.items()},
        },
        "selected_filter": selected_filter,
        "pagination": pagination,
        "page_url": page_url,
        "worker_label": worker_label,
        "worker_color": worker_color,
        "worker_seen_at": _tenant_sync_time(last_seen_at),
        "enabled": ticket_ai_enabled(),
    }


def mobile_sync_report_groups():
    from .models import MobileSyncReport

    rows = db.session.execute(
        db.select(MobileSyncReport, User.display_name)
        .join(User, User.id == MobileSyncReport.user_id)
        .order_by(User.display_name, User.id, MobileSyncReport.reported_at.desc())
    )
    groups = {}
    for report, display_name in rows:
        group = groups.setdefault(report.user_id, {"name": display_name, "devices": []})
        group["devices"].append({
            "id": report.device_id[:8],
            "app_version": report.app_version,
            "app_build": report.app_build,
            "reported_at": format_local_datetime(report.reported_at),
            "total_entries": report.total_entries,
            "entries": [
                {
                    **entry,
                    "time_label": format_local_datetime(parse_local_datetime(entry["created_at"])),
                }
                for entry in report.entries
            ],
        })
    return groups.values()


@admin_bp.get("/admin/sync-health")
@admin_required
def sync_health() -> Any:
    from .health import full_snapshot
    outbox_items = (
        ErpOutboxOperation.query
        .filter(
            ErpOutboxOperation.state.in_(
                (
                    ErpOutboxState.PENDING.value,
                    ErpOutboxState.PROCESSING.value,
                    ErpOutboxState.FAILED.value,
                )
            )
        )
        .order_by(
            ErpOutboxOperation.available_at.asc(),
            ErpOutboxOperation.id.asc(),
        )
        .all()
    )

    webhook_items = (
        WebhookEvent.query
        .filter(
            WebhookEvent.processed.is_(False)
        )
        .order_by(
            WebhookEvent.received_at.asc(),
            WebhookEvent.id.asc(),
        )
        .all()
    )

    commission_items = (
        Commission.query
        .filter(
            or_(
                Commission.erp_sync_state
                == CommissionSyncState.FAILED.value,
                Commission.media_sync_error.isnot(None),
                Commission.document_sync_error.isnot(None),
            )
        )
        .order_by(
            Commission.synced_at.asc(),
            Commission.id.asc(),
        )
        .all()
    )

    outbox_status_labels = {
        ErpOutboxState.PENDING.value:
            "Ausstehend",
        ErpOutboxState.PROCESSING.value:
            "In Bearbeitung",
        ErpOutboxState.FAILED.value:
            "Fehlgeschlagen",
    }

    return render_template(
        "admin/sync_health.html",
        outbox_items=outbox_items,
        webhook_items=webhook_items,
        commission_items=commission_items,
        outbox_status_labels=outbox_status_labels,
        tenant_sync=tenant_notification_sync_context(),
        ticket_ai_sync=ticket_ai_sync_context(),
        mobile_sync_users=mobile_sync_report_groups(),
        health=full_snapshot(),
    )


@admin_bp.route("/admin/settings/health", methods=["GET", "POST"])
@admin_required
def health_settings() -> Any:
    from .health import full_snapshot
    from .health_rules import DEFAULT_HEALTH_RULES

    if request.method == "POST":
        try:
            rules = {}
            for key in DEFAULT_HEALTH_RULES:
                rule = {
                    "enabled": request.form.get(f"health.{key}.enabled") == "true"
                }
                for name in ("warning", "error"):
                    raw_value = request.form.get(f"health.{key}.{name}", "").strip()
                    try:
                        rule[name] = int(raw_value) if raw_value else None
                    except ValueError as exc:
                        raise ValueError(
                            "Die Health-Grenzen müssen ganze Zahlen sein."
                        ) from exc
                rules[key] = rule
            settings.set(SettingKeys.HEALTH_RULES, rules)
            audit("health.settings.update")
            db.session.commit()
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
            return redirect(url_for("admin.health_settings"))
        flash("Health-Grenzen gespeichert.", "success")
        return redirect(url_for("admin.health_settings"))

    return render_template(
        "admin/health_settings.html",
        health=full_snapshot(),
        checked_at=_tenant_sync_time(utc_now()),
    )


@admin_bp.route("/admin/settings/email-templates", methods=["GET", "POST"])
@admin_required
def email_templates_page() -> Any:
    from .email_layout import EMAIL_LAYOUT_FIELDS, validate_email_layout
    from .email_templates import (
        EMAIL_LAYOUT_TEMPLATE_KEY,
        EMAIL_TEMPLATES, EXAMPLE_CONTEXT, FIELD_LABELS,
        email_layout_definition, email_template_definition,
        get_email_layout, get_email_template, render_email_value,
        validate_email_template,
    )
    from .external_portal_texts import PORTAL_TEXTS_TEMPLATE_KEY, edit_external_portal_texts, external_mail_preview

    template_key = (
        request.form.get("template_key")
        if request.method == "POST"
        else request.args.get("template", next(iter(EMAIL_TEMPLATES)))
    )
    if template_key == PORTAL_TEXTS_TEMPLATE_KEY:
        return edit_external_portal_texts()
    is_layout = template_key == EMAIL_LAYOUT_TEMPLATE_KEY
    if not is_layout and template_key not in EMAIL_TEMPLATES:
        abort(404)
    definition = email_layout_definition() if is_layout else email_template_definition(template_key)
    value = {"html": get_email_layout()} if is_layout else get_email_template(template_key)
    preview_options = [
        (key, item["label"])
        for key, item in EMAIL_TEMPLATES.items()
        if key != "appointment_calendar"
    ]
    preview_key = request.values.get("preview_template", preview_options[0][0])
    if preview_key not in dict(preview_options):
        abort(400)
    preview = None
    preview_html = None
    if request.method == "POST":
        action = request.form.get("action", "save")
        if action not in {"save", "preview", "reset"}:
            abort(400)
        if is_layout:
            value = {"html": definition.default if action == "reset" else request.form.get("html", "")}
        else:
            value = (
                dict(definition.default) if action == "reset"
                else {
                    "subject": request.form.get("subject", ""),
                    "body": request.form.get("body", ""),
                }
            )
        try:
            if is_layout:
                value["html"] = validate_email_layout(value["html"])
            else:
                value = validate_email_template(template_key, value)
            if action == "preview":
                rendered_key = preview_key if is_layout else template_key
                preview = render_email_value(
                    rendered_key,
                    get_email_template(preview_key) if is_layout else value,
                    {**EXAMPLE_CONTEXT, "reminder_number": "2" if rendered_key == "external_reminder_second" else EXAMPLE_CONTEXT["reminder_number"]},
                )
                if is_layout or template_key != "appointment_calendar":
                    preview, preview_html = external_mail_preview(
                        rendered_key, preview,
                        layout=value["html"] if is_layout else None,
                    )
            else:
                settings.set(definition, value["html"] if is_layout else value)
                audit(f"email_template.{action}", "app_setting", definition.key)
                db.session.commit()
                flash("HTML-Layout gespeichert." if is_layout else "E-Mail-Vorlage gespeichert.", "success")
                return redirect(url_for(
                    "admin.email_templates_page", template=template_key,
                    preview_template=preview_key,
                ))
        except ValueError as exc:
            preview = None
            preview_html = None
            db.session.rollback()
            flash(str(exc), "danger")
    return render_template(
        "admin/email_templates.html",
        template_options=[
            (EMAIL_LAYOUT_TEMPLATE_KEY, "Gemeinsames HTML-Layout"),
            (PORTAL_TEXTS_TEMPLATE_KEY, "Portal light: Antwortseite und Mail-Schaltflächen"),
            *[(key, item["label"]) for key, item in EMAIL_TEMPLATES.items()],
        ],
        template_key=template_key,
        template_value=value,
        is_layout=is_layout,
        preview_options=preview_options,
        preview_key=preview_key,
        fields=EMAIL_LAYOUT_FIELDS if is_layout else {
            name: FIELD_LABELS[name]
            for name in EMAIL_TEMPLATES[template_key]["fields"].split()
        },
        preview=preview,
        preview_html=preview_html,
    )

DOCUMENT_TEMPLATE_SECTIONS = {
    "invoice": (
        "Rechnung", "admin.invoice_layout",
        (
            SettingKeys.INVOICE_INTRODUCTORY_TEXT,
            SettingKeys.INVOICE_OFFER_INTRODUCTORY_TEXT,
            SettingKeys.INVOICE_CONCLUDING_TEXT,
            SettingKeys.INVOICE_TRANSFER_PAYMENT_TEXT,
            SettingKeys.INVOICE_SEPA_PAYMENT_TEXT,
        ),
    ),
    "cover-letter": (
        "Anschreiben", "admin.invoice_cover_letter_layout",
        (
            SettingKeys.INVOICE_COVER_LETTER_TITLE,
            SettingKeys.INVOICE_COVER_LETTER_TEXT,
            SettingKeys.INVOICE_TENANT_CHARGE_PURPOSE_TEMPLATE,
            SettingKeys.INVOICE_TENANT_CHARGE_CLOSING_TEMPLATE,
        ),
    ),
    "offer": (
        "Angebot", "admin.offer_layout",
        (SettingKeys.OFFER_INTRODUCTORY_TEXT, SettingKeys.OFFER_CONCLUDING_TEXT),
    ),
}
DOCUMENT_TEMPLATE_SETTING_KEYS = {
    definition.key
    for _, _, definitions in DOCUMENT_TEMPLATE_SECTIONS.values()
    for definition in definitions
} | {
    SettingKeys.INVOICE_LAYOUT.key,
    SettingKeys.INVOICE_COVER_LETTER_LAYOUT.key,
    SettingKeys.OFFER_LAYOUT.key,
}


@admin_bp.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def settings_page() -> Any:
    if request.method == "POST":
        try:
            for definition in all_definitions():
                if (
                    definition.key.startswith("dms.")
                    or definition.key == SettingKeys.HEALTH_RULES.key
                    or definition.key in DOCUMENT_TEMPLATE_SETTING_KEYS
                ):
                    continue
                if definition.key == SettingKeys.TENANT_TICKET_WRITEBACK_EXCLUDED_SOURCE_IDS.key:
                    try:
                        source_ids = [
                            int(value)
                            for value in request.form.getlist(definition.key)
                        ]
                    except ValueError as exc:
                        raise ValueError(
                            "Bitte gültige Ticketquellen auswählen."
                        ) from exc
                    settings.set(definition, source_ids)
                    continue
                settings.set_from_form(
                    definition,
                    request.form.get(definition.key),
                )
            audit("settings.update")
            db.session.commit()
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
            return redirect(url_for("admin.settings_page"))

        configure_logging(current_app._get_current_object())
        flash("Einstellungen gespeichert.", "success")
        return redirect(url_for("admin.settings_page"))

    source_choices = {
        source_id: source_code or "Ticketquelle"
        for source_id, source_code in (
            db.session.query(TicketMirror.source_erp_id, TicketMirror.source_code)
            .filter(TicketMirror.source_erp_id > 0)
            .order_by(TicketMirror.source_erp_id, TicketMirror.source_code)
            .distinct()
            .all()
        )
    }
    for source_id in settings.get(SettingKeys.TENANT_TICKET_WRITEBACK_EXCLUDED_SOURCE_IDS):
        source_choices.setdefault(source_id, "Nicht mehr lokal vorhanden")

    groups = {
        group: [
            item for item in definitions
            if not item.key.startswith("dms.") and item.key not in DOCUMENT_TEMPLATE_SETTING_KEYS
        ]
        for group, definitions in grouped_definitions().items()
    }
    return render_template(
        "admin/settings.html",
        groups={group: definitions for group, definitions in groups.items() if definitions},
        settings=settings,
        tenant_ticket_source_choices=sorted(
            source_choices.items(),
            key=lambda choice: (choice[1].casefold(), choice[0]),
        ),
    )


@admin_bp.route("/admin/document-templates", methods=["GET", "POST"])
@admin_required
def document_templates():
    submitted = {}
    status = 200
    if request.method == "POST":
        section = request.form.get("section")
        if section not in DOCUMENT_TEMPLATE_SECTIONS:
            flash("Bitte wählen Sie eine gültige Dokumentart.", "danger")
            return redirect(url_for("admin.document_templates"))
        _, _, definitions = DOCUMENT_TEMPLATE_SECTIONS[section]
        submitted = {definition.key: request.form[definition.key] for definition in definitions if definition.key in request.form}
        try:
            for definition in definitions:
                if definition.key in submitted:
                    settings.set(definition, submitted[definition.key])
            audit(f"document_templates.{section}.update")
            db.session.commit()
            flash("Standardtexte gespeichert.", "success")
            return redirect(url_for("admin.document_templates", _anchor=section))
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
            status = 400

    values = {
        definition.key: submitted[definition.key] if definition.key in submitted else settings.get(definition)
        for _, _, definitions in DOCUMENT_TEMPLATE_SECTIONS.values()
        for definition in definitions
    }
    return render_template(
        "admin/document_templates.html", sections=DOCUMENT_TEMPLATE_SECTIONS, values=values,
    ), status


@admin_bp.route("/admin/invoice-cover-letter-layout", methods=["GET", "POST"])
@admin_required
def invoice_cover_letter_layout():
    from .invoice_cover_letter_layout import COVER_LETTER_LAYOUT_FIELDS, DEFAULT_COVER_LETTER_LAYOUT
    value = settings.get(SettingKeys.INVOICE_COVER_LETTER_LAYOUT)
    if request.method == "POST":
        action = request.form.get("action", "save")
        value = DEFAULT_COVER_LETTER_LAYOUT if action == "reset" else request.form.get("html", "")
        try:
            if action not in {"save", "reset"}:
                raise ValueError("Die gewählte Aktion ist ungültig.")
            settings.set(SettingKeys.INVOICE_COVER_LETTER_LAYOUT, value)
            audit("invoice.cover_letter.layout.reset" if action == "reset" else "invoice.cover_letter.layout.update")
            db.session.commit()
            flash("Standardlayout wiederhergestellt." if action == "reset" else "Anschreiben-Layout gespeichert.", "success")
            return redirect(url_for("admin.invoice_cover_letter_layout"))
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
    return render_template(
        "admin/invoice_cover_letter_layout.html", layout=value, fields=COVER_LETTER_LAYOUT_FIELDS,
        invoices=Invoice.query.order_by(Invoice.created_at.desc(), Invoice.id.desc()).limit(100).all(),
    )


@admin_bp.post("/admin/invoice-cover-letter-layout/preview")
@admin_required
def invoice_cover_letter_layout_preview():
    from .invoice_correspondence import cover_letter_url_fetcher, cover_letter_values, letter_date, render_cover_letter
    from .invoice_cover_letter_layout import render_cover_letter_html
    from weasyprint import HTML
    try:
        layout = request.form.get("html", "")
        invoice_id = request.form.get("invoice_id", "").strip()
        if invoice_id:
            if not invoice_id.isdecimal() or len(invoice_id) > 18:
                raise ValueError("Bitte eine gültige Rechnung für die Vorschau auswählen.")
            invoice = db.session.get(Invoice, int(invoice_id))
            if invoice is None:
                raise ValueError("Die ausgewählte Rechnung ist nicht mehr verfügbar.")
            if invoice.tenant_billing_reference:
                from .tenant_charge_letter import render_tenant_charge_letter

                if not invoice.tenant_charge_letter:
                    raise ValueError("Bitte wählen Sie eine Rechnung mit gespeichertem Mieteranschreiben.")
                company = db.session.get(WowiCompanyCode, invoice.tenant_billing_reference["company_code_id"])
                if company is None:
                    raise ValueError("Das Buchwerk des Mietvertrags ist nicht verfügbar.")
                content = render_tenant_charge_letter(invoice, company, invoice.tenant_charge_letter, layout=layout)
            else:
                values = cover_letter_values(invoice)
                content = render_cover_letter(invoice, values["title"], values["text"], layout=layout)
        else:
            context = {
                "title": "Ihre Rechnung RE-BEISPIEL-001", "text": "Sehr geehrte Damen und Herren,\n\nbeigefügt erhalten Sie unsere Rechnung über 119,00 EUR.\n\nMit freundlichen Grüßen\nBeispielunternehmen",
                "recipient_address": "Max Mustermann\nMusterstraße 12\n12345 Musterstadt",
                "recipient_name": "Max Mustermann", "recipient_street": "Musterstraße 12", "recipient_postal_code": "12345",
                "recipient_town": "Musterstadt", "recipient_country_code": "DE", "contract_number": "1000/123/01",
                "invoice_number": "RE-BEISPIEL-001", "invoice_date": letter_date().strftime("%d.%m.%Y"),
                "letter_date": letter_date().strftime("%d.%m.%Y"), "caseworker_name": "Erika Musterfrau",
                "caseworker_phone": "02306 123456", "caseworker_email": "erika@example.invalid",
                "sender_line": "Beispielunternehmen · Firmenstraße 1 · 12345 Musterstadt", "seller_name": "Beispielunternehmen",
                "amount": "119,00", "currency": "EUR", "payment_terms": "Bitte überweisen Sie den Rechnungsbetrag zum vereinbarten Fälligkeitstermin.",
            }
            content = HTML(string=render_cover_letter_html(context, layout), url_fetcher=cover_letter_url_fetcher).write_pdf()
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except Exception:
        current_app.logger.exception("invoice_cover_letter_layout_preview_failed")
        return jsonify(error="Die Layout-Vorschau konnte nicht erstellt werden. Bitte Layout und Rechnungsdaten prüfen."), 502
    return document_preview_response(content, "Anschreiben-Layout-Vorschau.pdf")


@admin_bp.route("/admin/invoice-layout", methods=["GET", "POST"], defaults={"kind": "invoice"}, endpoint="invoice_layout")
@admin_bp.route("/admin/offer-layout", methods=["GET", "POST"], defaults={"kind": "offer"}, endpoint="offer_layout")
@admin_required
def document_layout(kind):
    from .document_layout import LAYOUT_LIMIT, POSITION_FIELDS, VAT_FIELDS
    from .invoice_layout import DEFAULT_INVOICE_LAYOUT, INVOICE_LAYOUT_FIELDS
    from .offer_layout import DEFAULT_OFFER_LAYOUT, OFFER_LAYOUT_FIELDS

    if kind == "invoice":
        model, definition, default_layout, fields = Invoice, SettingKeys.INVOICE_LAYOUT, DEFAULT_INVOICE_LAYOUT, INVOICE_LAYOUT_FIELDS
        label, heading = "Rechnungs-Layout", "Aufbau der Rechnung"
        description = "Gemeinsames PDF-Layout für Rechnungen und Stornorechnungen aller Buchwerke."
    else:
        model, definition, default_layout, fields = Offer, SettingKeys.OFFER_LAYOUT, DEFAULT_OFFER_LAYOUT, OFFER_LAYOUT_FIELDS
        label, heading = "Angebots-Layout", "Aufbau des Angebots"
        description = "Gemeinsames PDF-Layout für Angebote aller Buchwerke."

    value = settings.get(definition) or default_layout
    if request.method == "POST":
        action = request.form.get("action", "save")
        html = request.form.get("html", "").replace("\r\n", "\n").replace("\r", "\n")
        value = default_layout if action == "reset" else html
        try:
            if action not in {"save", "reset"}:
                raise ValueError("Die gewählte Aktion ist ungültig.")
            if not value.strip():
                raise ValueError(f"Bitte geben Sie ein {label} ein.")
            settings.set(definition, "" if value == default_layout else value)
            audit(f"{kind}.layout.reset" if action == "reset" else f"{kind}.layout.update")
            db.session.commit()
            flash("Standardlayout wiederhergestellt." if action == "reset" else f"{label} gespeichert.", "success")
            return redirect(url_for(f"admin.{kind}_layout"))
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
    return render_template(
        "admin/document_layout.html", kind=kind, label=label, heading=heading, description=description,
        layout=value, layout_limit=LAYOUT_LIMIT, fields=fields,
        position_fields=POSITION_FIELDS, vat_fields=VAT_FIELDS,
        selected_document_id=request.form.get(f"{kind}_id", ""),
        documents=model.query.order_by(model.created_at.desc(), model.id.desc()).limit(100).all(),
    )


@admin_bp.post("/admin/invoice-layout/preview")
@admin_required
def invoice_layout_preview():
    from .document_previews import render_invoice_preview
    from .invoice_document import render_invoice_content_pdf
    from .invoice_layout import example_invoice_layout_context

    try:
        layout = request.form.get("html", "")
        invoice_id = request.form.get("invoice_id", "").strip()
        if invoice_id:
            if not invoice_id.isdecimal() or len(invoice_id) > 18:
                raise ValueError("Bitte eine gültige Rechnung für die Vorschau auswählen.")
            invoice = db.session.get(Invoice, int(invoice_id))
            if invoice is None:
                raise ValueError("Die ausgewählte Rechnung ist nicht mehr verfügbar.")
            content = render_invoice_preview(invoice, layout=layout, include_attachments=False)
        else:
            content = render_invoice_content_pdf({**example_invoice_layout_context(), "preview": True}, layout=layout)
    except (ValueError, InvoiceDocumentError) as exc:
        return jsonify(error=str(exc)), 400
    except Exception:
        current_app.logger.exception("invoice_layout_preview_failed")
        return jsonify(error="Die Layout-Vorschau konnte nicht erstellt werden. Bitte Layout und Rechnungsdaten prüfen."), 502
    return document_preview_response(content, "Rechnungs-Layout-Vorschau.pdf")


@admin_bp.post("/admin/offer-layout/preview")
@admin_required
def offer_layout_preview():
    from .document_previews import render_offer_preview
    from .offer_document import render_offer_content_pdf
    from .offer_layout import example_offer_layout_context

    try:
        layout = request.form.get("html", "")
        offer_id = request.form.get("offer_id", "").strip()
        if offer_id:
            if not offer_id.isdecimal() or len(offer_id) > 18:
                raise ValueError("Bitte ein gültiges Angebot für die Vorschau auswählen.")
            offer = db.session.get(Offer, int(offer_id))
            if offer is None:
                raise ValueError("Das ausgewählte Angebot ist nicht mehr verfügbar.")
            content = render_offer_preview(offer, layout=layout)
        else:
            content = render_offer_content_pdf({**example_offer_layout_context(), "preview": True}, layout=layout)
    except (ValueError, OfferDocumentError) as exc:
        return jsonify(error=str(exc)), 400
    except Exception:
        current_app.logger.exception("offer_layout_preview_failed")
        return jsonify(error="Die Layout-Vorschau konnte nicht erstellt werden. Bitte Layout und Angebotsdaten prüfen."), 502
    return document_preview_response(content, "Angebots-Layout-Vorschau.pdf")


@admin_bp.get(
    "/admin/company-codes"
)
@admin_required
def company_codes() -> Any:
    items = (
        WowiCompanyCode.query
        .order_by(
            WowiCompanyCode.code.asc()
        )
        .all()
    )

    return render_template(
        "admin/company_codes.html",
        items=items,
    )


@admin_bp.route(
    "/admin/company-codes/<int:item_id>/invoicing",
    methods=["GET", "POST"],
)
@admin_required
def company_code_invoicing(
    item_id: int,
) -> Any:
    from .stationery_layout import form_layout_values, save_layouts

    item = db.get_or_404(
        WowiCompanyCode,
        item_id,
    )

    issuer_choices = WowiCompanyCode.query.filter(
        WowiCompanyCode.id != item.id,
        or_(WowiCompanyCode.active.is_(True), WowiCompanyCode.id == item.tenant_billing_issuer_id),
    ).order_by(WowiCompanyCode.code).all()

    if request.method == "POST":
        try:
            cover_upload = request.files.get("cover_letter_stationery")
            own_cover = bool(cover_upload is not None and cover_upload.filename) or (
                bool(item.cover_letter_stationery_filename) and "remove_cover_letter_stationery" not in request.form
            )
            save_layouts(item, request.form, has_own_cover_stationery=own_cover)
            save_company_code_invoice_form(
                item
            )
            for field in ("stationery", "cover_letter_stationery"):
                upload = request.files.get(field)
                if upload is not None and upload.filename:
                    validate_company_code_stationery(upload)

            if (
                "remove_stationery"
                in request.form
            ):
                remove_company_code_stationery(
                    item
                )

            stationery = request.files.get(
                "stationery"
            )

            if (
                stationery is not None
                and stationery.filename
            ):
                store_company_code_stationery(
                    item,
                    stationery,
                )
            if "remove_cover_letter_stationery" in request.form:
                remove_company_code_stationery(item, attribute="cover_letter_stationery_filename")
            cover_stationery = request.files.get("cover_letter_stationery")
            if cover_stationery is not None and cover_stationery.filename:
                store_company_code_stationery(item, cover_stationery, attribute="cover_letter_stationery_filename")

            audit(
                "company_code.invoicing.update",
                "wowi_company_code",
                item.id,
            )
            db.session.commit()

        except ValueError as exc:
            db.session.rollback()
            flash(
                str(exc),
                "danger",
            )

            return render_template(
                "admin/company_code_invoicing.html",
                item=item,
                issuer_choices=issuer_choices,
                stationery_layouts=form_layout_values(item, request.form),
            )

        flash(
            "Rechnungs- und Angebotseinstellungen "
            "gespeichert.",
            "success",
        )

        return redirect(
            url_for(
                "admin.company_code_invoicing",
                item_id=item.id,
            )
        )

    return render_template(
        "admin/company_code_invoicing.html",
        item=item,
        issuer_choices=issuer_choices,
        stationery_layouts=form_layout_values(item),
    )


@admin_bp.post("/admin/company-codes/<int:item_id>/invoicing/preview/<kind>")
@admin_required
def company_code_stationery_preview(item_id, kind):
    from .stationery_layout import form_layouts
    from .stationery_preview import render_stationery_preview

    if kind not in {"invoice", "offer", "cover_letter"}:
        abort(404)
    item = db.get_or_404(WowiCompanyCode, item_id)
    try:
        layouts = form_layouts(item, request.form)
        uploads = {}
        for slot in (("stationery", "cover_letter_stationery") if kind == "cover_letter" else ("stationery",)):
            upload = request.files.get(slot)
            if upload is not None and upload.filename:
                validate_company_code_stationery(upload)
                uploads[slot] = upload.stream.read()
        content = render_stationery_preview(item, kind, layouts, request.form, uploads)
    except (ValueError, InvoiceDocumentError) as exc:
        return jsonify(error=str(exc)), 400
    except Exception:
        current_app.logger.exception("company_code_stationery_preview_failed company_code_id=%s kind=%s", item.id, kind)
        return jsonify(error="Die Briefbogen-Vorschau konnte nicht erstellt werden. Bitte Briefbogen und Seitenabstände prüfen."), 502
    title = {"invoice": "Rechnung", "offer": "Angebot", "cover_letter": "Anschreiben"}[kind]
    return document_preview_response(content, f"{title}-Briefbogen-Vorschau.pdf")


@admin_bp.get(
    "/admin/company-codes/<int:item_id>/stationery"
)
@admin_required
def company_code_stationery(
    item_id: int,
) -> Any:
    item = db.get_or_404(
        WowiCompanyCode,
        item_id,
    )

    if not item.stationery_filename:
        abort(404)

    directory = invoice_stationery_directory()
    file_path = (
        directory
        / item.stationery_filename
    )

    if not file_path.is_file():
        abort(404)

    return send_from_directory(
        directory,
        item.stationery_filename,
        mimetype="application/pdf",
        as_attachment=False,
    )


@admin_bp.get("/admin/company-codes/<int:item_id>/cover-letter-stationery")
@admin_required
def company_code_cover_letter_stationery(item_id):
    item = db.get_or_404(WowiCompanyCode, item_id)
    if not item.cover_letter_stationery_filename:
        abort(404)
    return send_from_directory(invoice_stationery_directory(), item.cover_letter_stationery_filename, mimetype="application/pdf", as_attachment=False)


@admin_bp.route(
    "/admin/offer-settings",
    methods=["GET", "POST"],
)
@admin_required
def offer_settings() -> Any:
    craft_activities = (
        CraftActivity.query
        .filter(
            CraftActivity.active.is_(True),
            CraftActivity.app_enabled.is_(True),
        )
        .order_by(
            CraftActivity.name.asc(),
            CraftActivity.id.asc(),
        )
        .all()
    )

    service_positions = (
        TimeTaskTemplate.query
        .filter(
            TimeTaskTemplate.active.is_(True),
            TimeTaskTemplate
            .erp_service_catalogue_id
            .isnot(None),
            TimeTaskTemplate
            .craft_activity_id
            .isnot(None),
        )
        .order_by(
            TimeTaskTemplate.title.asc(),
            TimeTaskTemplate.code.asc(),
        )
        .all()
    )

    positions_by_craft = {
        craft_activity.id: []
        for craft_activity in craft_activities
    }

    for service_position in service_positions:
        craft_positions = (
            positions_by_craft.get(
                service_position.craft_activity_id
            )
        )

        if craft_positions is not None:
            craft_positions.append(
                service_position
            )

    mappings = {
        mapping.craft_activity_id: mapping
        for mapping in (
            OfferCraftFallback.query.all()
        )
    }

    if request.method == "POST":
        positions_by_id = {
            service_position.id:
                service_position
            for service_position
            in service_positions
        }
        selected_positions = {}

        try:
            for craft_activity in craft_activities:
                raw_position_id = (
                    request.form.get(
                        (
                            "fallback_position_"
                            f"{craft_activity.id}"
                        ),
                        "",
                    ).strip()
                )

                if not raw_position_id:
                    continue

                try:
                    position_id = int(
                        raw_position_id
                    )
                except ValueError as exc:
                    raise ValueError(
                        "Eine ausgewählte "
                        "Leistungsposition ist "
                        "ungültig."
                    ) from exc

                service_position = (
                    positions_by_id.get(
                        position_id
                    )
                )

                if (
                    service_position is None
                    or (
                        service_position
                        .craft_activity_id
                        != craft_activity.id
                    )
                ):
                    raise ValueError(
                        "Die Leistungsposition für "
                        f"{craft_activity.name} "
                        "gehört nicht zum Gewerk."
                    )

                selected_positions[
                    craft_activity.id
                ] = service_position

            for craft_activity in craft_activities:
                mapping = mappings.get(
                    craft_activity.id
                )
                service_position = (
                    selected_positions.get(
                        craft_activity.id
                    )
                )

                if service_position is None:
                    if mapping is not None:
                        db.session.delete(mapping)

                    continue

                if mapping is None:
                    mapping = OfferCraftFallback(
                        craft_activity=craft_activity
                    )
                    db.session.add(mapping)

                mapping.task_template = (
                    service_position
                )
                mapping.updated_by = current_user
                mapping.updated_at = utc_now()

            audit(
                "offer.settings.update",
                metadata={
                    "fallback_count": len(
                        selected_positions
                    ),
                },
            )
            db.session.commit()

        except ValueError as exc:
            db.session.rollback()
            flash(
                str(exc),
                "danger",
            )

            return redirect(
                url_for(
                    "admin.offer_settings"
                )
            )

        flash(
            "Angebotsvorgaben gespeichert.",
            "success",
        )

        return redirect(
            url_for(
                "admin.offer_settings"
            )
        )

    return render_template(
        "admin/offer_settings.html",
        craft_activities=craft_activities,
        positions_by_craft=(
            positions_by_craft
        ),
        mappings=mappings,
    )


@admin_bp.route(
    "/admin/ticket-settings",
    methods=["GET", "POST"],
)
@admin_required
def ticket_settings() -> Any:
    craft_activities = (
        CraftActivity.query
        .filter(
            CraftActivity.active.is_(True),
            CraftActivity.app_enabled.is_(True),
        )
        .order_by(
            CraftActivity.name.asc(),
            CraftActivity.id.asc(),
        )
        .all()
    )

    service_positions = (
        commission_service_positions()
        .order_by(
            TimeTaskTemplate.title.asc(),
            TimeTaskTemplate.code.asc(),
        )
        .all()
    )

    allowed_type_ids = (
        configured_commission_type_ids()
    )

    commission_types = (
        WowiCommissionType.query
        .filter(
            WowiCommissionType.active.is_(True),
            WowiCommissionType.erp_id.in_(
                allowed_type_ids
            ),
        )
        .order_by(
            WowiCommissionType.code.asc(),
            WowiCommissionType.erp_id.asc(),
        )
        .all()
    )

    default_commission_type_id = int(
        settings.get(
            SettingKeys
            .WOWI_DEFAULT_COMMISSION_TYPE_ID
        )
        or 0
    )

    positions_by_craft = {
        craft_activity.id: []
        for craft_activity in craft_activities
    }

    for service_position in service_positions:
        craft_positions = positions_by_craft.get(
            service_position.craft_activity_id
        )

        if craft_positions is not None:
            craft_positions.append(
                service_position
            )

    mappings = {
        mapping.craft_activity_id: mapping
        for mapping in (
            TicketCraftFallback.query.all()
        )
    }

    if request.method == "POST":
        positions_by_id = {
            service_position.id: service_position
            for service_position in service_positions
        }
        selected_positions = {}
        try:
            raw_commission_type_id = (
                request.form.get(
                    "default_commission_type_id",
                    "",
                ).strip()
            )

            if raw_commission_type_id:
                default_commission_type_id = int(
                    raw_commission_type_id
                )
            else:
                default_commission_type_id = 0

            available_type_ids = {
                item.erp_id
                for item in commission_types
            }

            if (
                    default_commission_type_id
                    and default_commission_type_id
                    not in available_type_ids
            ):
                raise ValueError(
                    "Die ausgewählte Standard-"
                    "Auftragsart ist nicht verfügbar."
                )

            settings.set(
                SettingKeys
                .WOWI_DEFAULT_COMMISSION_TYPE_ID,
                default_commission_type_id,
            )

            for craft_activity in craft_activities:
                raw_position_id = request.form.get(
                    (
                        "fallback_position_"
                        f"{craft_activity.id}"
                    ),
                    "",
                ).strip()

                if not raw_position_id:
                    continue

                try:
                    position_id = int(
                        raw_position_id
                    )
                except ValueError as exc:
                    raise ValueError(
                        "Eine ausgewählte "
                        "Leistungsposition ist "
                        "ungültig."
                    ) from exc

                service_position = (
                    positions_by_id.get(position_id)
                )

                if (
                    service_position is None
                    or service_position
                    .craft_activity_id
                    != craft_activity.id
                ):
                    raise ValueError(
                        "Die Leistungsposition für "
                        f"{craft_activity.name} "
                        "gehört nicht zum Gewerk."
                    )

                selected_positions[
                    craft_activity.id
                ] = service_position

            for craft_activity in craft_activities:
                field_name = f"fallback_position_{craft_activity.id}"
                if field_name not in request.form:
                    continue

                mapping = mappings.get(
                    craft_activity.id
                )
                service_position = (
                    selected_positions.get(
                        craft_activity.id
                    )
                )

                if service_position is None:
                    if mapping is not None:
                        db.session.delete(mapping)

                    continue

                if mapping is None:
                    mapping = TicketCraftFallback(
                        craft_activity=craft_activity
                    )
                    db.session.add(mapping)

                mapping.task_template = (
                    service_position
                )
                mapping.updated_by = current_user
                mapping.updated_at = utc_now()

            audit(
                "ticket.settings.update",
                metadata={
                    "fallback_count": len(
                        selected_positions
                    ),
                },
            )
            db.session.commit()

        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")

            return redirect(
                url_for(
                    "admin.ticket_settings"
                )
            )

        flash(
            "KI-Auftragsvorgaben gespeichert.",
            "success",
        )

        return redirect(
            url_for("admin.ticket_settings")
        )

    assignment_rules = (
        TicketAiAssignmentRule.query
        .order_by(
            TicketAiAssignmentRule.active.desc(),
            TicketAiAssignmentRule.priority.desc(),
            TicketAiAssignmentRule.name.asc(),
            TicketAiAssignmentRule.id.asc(),
        )
        .all()
    )
    craftsman_users = (
        User.query
        .filter(
            User.is_active_flag.is_(True),
            User.craftsman_erp_id.isnot(None),
        )
        .order_by(
            User.display_name.asc(),
            User.id.asc(),
        )
        .all()
    )
    edit_rule_id = request.args.get(
        "edit_rule_id",
        type=int,
    )
    edit_assignment_rule = (
        db.session.get(
            TicketAiAssignmentRule,
            edit_rule_id,
        )
        if edit_rule_id is not None
        else None
    )

    return render_template(
        "admin/ticket_settings.html",
        craft_activities=craft_activities,
        positions_by_craft=positions_by_craft,
        mappings=mappings,
        commission_types=commission_types,
        default_commission_type_id=(
            default_commission_type_id
        ),
        assignment_rules=assignment_rules,
        craftsman_users=craftsman_users,
        edit_assignment_rule=edit_assignment_rule,
        external_craftsmen=available_external_craftsmen(),
    )


@admin_bp.post(
    "/admin/ticket-settings/assignment-rules/save"
)
@admin_required
def ticket_assignment_rule_save() -> Any:
    raw_rule_id = request.form.get(
        "rule_id",
        "",
    ).strip()
    rule_id = None

    try:
        if raw_rule_id:
            try:
                rule_id = int(raw_rule_id)
            except ValueError as exc:
                raise ValueError(
                    "Die Zuweisungsregel ist ungültig."
                ) from exc

        name = request.form.get(
            "name",
            "",
        ).strip()
        condition_text = request.form.get(
            "condition_text",
            "",
        ).strip()

        def optional_form_id(
                field_name: str,
                label: str,
        ) -> int | None:
            raw_value = request.form.get(
                field_name,
                "",
            ).strip()

            if not raw_value:
                return None

            try:
                value = int(raw_value)
            except ValueError as exc:
                raise ValueError(
                    f"{label} ist ungültig."
                ) from exc

            return value if value > 0 else None

        target_user_id = optional_form_id(
            "target_user_id",
            "Der Handwerker",
        )
        target_assignment_type = (
            request.form.get(
                "target_assignment_type",
                "",
            ).strip().casefold()
            or None
        )
        target_external_craftsman_id = optional_form_id(
            "target_external_craftsman_id",
            "Der externe Handwerker",
        )

        if target_assignment_type not in {
            None,
            "internal",
            "external",
        }:
            raise ValueError(
                "Die Vergabeart der Regel ist ungültig."
            )

        if (
            target_assignment_type == "external"
            and target_user_id is not None
        ):
            raise ValueError(
                "Ein interner Handwerker passt nicht "
                "zur externen Vergabe."
            )

        if (
            target_assignment_type != "external"
            and target_external_craftsman_id is not None
        ):
            raise ValueError(
                "Ein externer Handwerker erfordert "
                "die externe Vergabeart."
            )
        target_craft_activity_id = optional_form_id(
            "target_craft_activity_id",
            "Das Gewerk",
        )
        target_task_template_id = optional_form_id(
            "target_task_template_id",
            "Die Leistungsposition",
        )

        if (
            target_user_id is None
            and target_external_craftsman_id is None
            and target_assignment_type is None
            and target_craft_activity_id is None
            and target_task_template_id is None
        ):
            raise ValueError(
                "Die Regel muss mindestens eine "
                "Vergabeart, ein Gewerk, einen "
                "Handwerker oder eine "
                "Leistungsposition festlegen."
            )

        enforcement_mode = request.form.get(
            "enforcement_mode",
            "",
        ).strip()

        try:
            priority = int(
                request.form.get(
                    "priority",
                    "100",
                )
            )
        except ValueError as exc:
            raise ValueError(
                "Die Priorität ist ungültig."
            ) from exc

        active = request.form.get("active") == "on"

        if not name:
            raise ValueError(
                "Eine Bezeichnung ist erforderlich."
            )

        if len(name) > 160:
            raise ValueError(
                "Die Bezeichnung darf höchstens "
                "160 Zeichen enthalten."
            )

        if not condition_text:
            raise ValueError(
                "Eine Bedingung ist erforderlich."
            )

        if len(condition_text) > 2000:
            raise ValueError(
                "Die Bedingung darf höchstens "
                "2000 Zeichen enthalten."
            )

        if not 0 <= priority <= 1000:
            raise ValueError(
                "Die Priorität muss zwischen 0 und "
                "1000 liegen."
            )

        allowed_modes = {
            item.value
            for item in TicketAiAssignmentMode
        }

        if enforcement_mode not in allowed_modes:
            raise ValueError(
                "Das Verhalten der Regel ist ungültig."
            )

        target_user = None
        target_craft_activity = None
        target_task_template = None

        if target_user_id is not None:
            target_user = (
                User.query
                .filter(
                    User.id == target_user_id,
                    User.is_active_flag.is_(True),
                    User.craftsman_erp_id.isnot(None),
                )
                .one_or_none()
            )

            if target_user is None:
                raise ValueError(
                    "Der ausgewählte Handwerker ist "
                    "nicht verfügbar."
                )

        if target_craft_activity_id is not None:
            target_craft_activity = (
                CraftActivity.query
                .filter(
                    CraftActivity.id
                    == target_craft_activity_id,
                    CraftActivity.active.is_(True),
                    CraftActivity.app_enabled.is_(True),
                )
                .one_or_none()
            )

            if target_craft_activity is None:
                raise ValueError(
                    "Das ausgewählte Gewerk ist "
                    "nicht verfügbar."
                )

        if target_task_template_id is not None:
            target_task_template = (
                commission_service_positions()
                .filter(
                    TimeTaskTemplate.id
                    == target_task_template_id
                )
                .one_or_none()
            )

            if target_task_template is None:
                raise ValueError(
                    "Die ausgewählte Leistungsposition "
                    "ist nicht verfügbar."
                )

        effective_craft_activity = (
                target_craft_activity
                or (
                    target_task_template.craft_activity
                    if target_task_template is not None
                    else None
                )
        )

        target_external_craftsman = None

        if target_external_craftsman_id is not None:
            available_craftsmen = available_external_craftsmen(
                effective_craft_activity.id
                if effective_craft_activity is not None
                else None
            )
            target_external_craftsman = next(
                (
                    craftsman
                    for craftsman in available_craftsmen
                    if craftsman.id
                    == target_external_craftsman_id
                ),
                None,
            )

            if target_external_craftsman is None:
                raise ValueError(
                    "Der ausgewählte externe "
                    "Handwerker ist für dieses "
                    "Gewerk nicht verfügbar."
                )

        if (
                target_craft_activity is not None
                and target_task_template is not None
                and target_task_template.craft_activity_id
                != target_craft_activity.id
        ):
            raise ValueError(
                "Die Leistungsposition gehört nicht "
                "zum ausgewählten Gewerk."
            )

        if (
                target_user is not None
                and effective_craft_activity is not None
                and effective_craft_activity
                not in target_user.craft_activities
        ):
            raise ValueError(
                "Der ausgewählte Handwerker ist dem "
                "ausgewählten Gewerk nicht zugeordnet."
            )

        if rule_id is None:
            rule = TicketAiAssignmentRule(
                created_by_user_id=current_user.id,
            )
            db.session.add(rule)
            audit_action = (
                "ticket.ai_assignment_rule.create"
            )
        else:
            rule = db.get_or_404(
                TicketAiAssignmentRule,
                rule_id,
            )
            audit_action = (
                "ticket.ai_assignment_rule.update"
            )

        rule.name = name
        rule.condition_text = condition_text
        rule.target_user_id = (
            target_user.id
            if target_user is not None
            else None
        )
        rule.target_assignment_type = target_assignment_type
        rule.target_external_craftsman_id = (
            target_external_craftsman.id
            if target_external_craftsman is not None
            else None
        )
        rule.target_craft_activity_id = (
            target_craft_activity.id
            if target_craft_activity is not None
            else None
        )
        rule.target_task_template_id = (
            target_task_template.id
            if target_task_template is not None
            else None
        )
        rule.enforcement_mode = enforcement_mode
        rule.priority = priority
        rule.active = active
        rule.updated_by_user_id = current_user.id
        rule.updated_at = utc_now()

        db.session.flush()
        stale_count = mark_ticket_analyses_stale()

        audit(
            audit_action,
            "ticket_ai_assignment_rule",
            rule.id,
            {
                "enforcement_mode": (
                    rule.enforcement_mode
                ),
                "priority": rule.priority,
                "active": rule.active,
                "stale_ticket_count": stale_count,
                "target_user_id": rule.target_user_id,
                "target_craft_activity_id": (
                    rule.target_craft_activity_id
                ),
                "target_task_template_id": (
                    rule.target_task_template_id
                ),
                "target_assignment_type": (
                    rule.target_assignment_type
                ),
                "target_external_craftsman_id": (
                    rule.target_external_craftsman_id
                ),
            },
        )
        db.session.commit()

    except (TypeError, ValueError) as exc:
        db.session.rollback()
        flash(str(exc), "danger")

        return redirect(
            url_for(
                "admin.ticket_settings",
                edit_rule_id=rule_id,
            )
        )

    flash(
        "KI-Zuweisungsregel gespeichert.",
        "success",
    )
    return redirect(
        url_for("admin.ticket_settings")
    )


@admin_bp.post(
    "/admin/ticket-settings/assignment-rules/"
    "<int:rule_id>/toggle"
)
@admin_required
def ticket_assignment_rule_toggle(
    rule_id: int,
) -> Any:
    rule = db.get_or_404(
        TicketAiAssignmentRule,
        rule_id,
    )
    new_active = not rule.active

    if new_active and rule.target_user_id is not None:
        target_user = rule.target_user

        if (
            target_user is None
            or not target_user.is_active
            or target_user.craftsman_erp_id is None
        ):
            flash(
                "Die Regel kann nicht aktiviert werden, "
                "weil der interne Handwerker nicht "
                "verfügbar ist.",
                "danger",
            )
            return redirect(
                url_for("admin.ticket_settings")
            )

    if (
        new_active
        and rule.target_external_craftsman_id is not None
    ):
        available_ids = {
            craftsman.id
            for craftsman in available_external_craftsmen()
        }

        if (
            rule.target_external_craftsman_id
            not in available_ids
        ):
            flash(
                "Die Regel kann nicht aktiviert werden, "
                "weil der externe Handwerker nicht "
                "verfügbar ist.",
                "danger",
            )
            return redirect(
                url_for("admin.ticket_settings")
            )

    rule.active = new_active
    rule.updated_by_user_id = current_user.id
    rule.updated_at = utc_now()
    stale_count = mark_ticket_analyses_stale()

    audit(
        "ticket.ai_assignment_rule.toggle",
        "ticket_ai_assignment_rule",
        rule.id,
        {
            "active": rule.active,
            "stale_ticket_count": stale_count,
        },
    )
    db.session.commit()

    flash(
        (
            "KI-Zuweisungsregel aktiviert."
            if rule.active
            else "KI-Zuweisungsregel deaktiviert."
        ),
        "success",
    )
    return redirect(
        url_for("admin.ticket_settings")
    )


def store_offer_archive_failure(
    offer_id: int,
    error_message: str,
) -> None:
    item = db.session.get(
        Offer,
        offer_id,
    )

    if item is None:
        return

    item.dms_archive_status = (
        OfferArchiveStatus.FAILED.value
    )
    item.dms_archive_attempted_at = (
        utc_now()
    )
    item.dms_archive_error = (
        error_message[:4000]
    )

    audit(
        "offer.archive_failed",
        "offer",
        item.id,
        {
            "offer_number": (
                item.offer_number
            ),
            "error": error_message[:4000],
        },
    )

    db.session.commit()


@admin_bp.post(
    "/admin/offers/<int:offer_id>/archive"
)
@offer_required
def offer_archive(offer_id: int) -> Any:
    from .dms_client import DmsError
    from .dms_service import retry_latest_document

    db.get_or_404(Offer, offer_id)
    try:
        document = retry_latest_document("offer", offer_id)
        db.session.commit()
        flash(
            "Bereits archiviert."
            if document.status == "archived"
            else "Archivierungsauftrag vorgemerkt.",
            "success",
        )
    except DmsError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return redirect(
        url_for("admin.offer_edit", offer_id=offer_id)
    )


@admin_bp.route(
    "/admin/invoice-billing-rules",
    methods=["GET", "POST"],
)
@admin_required
def invoice_billing_rules() -> Any:
    company_codes = (
        WowiCompanyCode.query
        .filter(
            WowiCompanyCode.active.is_(True)
        )
        .order_by(
            WowiCompanyCode.code.asc()
        )
        .all()
    )

    try:
        company_code = (
            invoice_rule_company_code_from_request()
        )
    except ValueError as exc:
        flash(
            str(exc),
            "danger",
        )

        return redirect(
            url_for(
                "admin.invoice_billing_rules"
            )
        )

    rules_query = (
        InvoiceBillingRule.query
    )

    if company_code is None:
        rules_query = rules_query.filter(
            InvoiceBillingRule
            .company_code_id
            .is_(None)
        )
    else:
        rules_query = rules_query.filter(
            InvoiceBillingRule
            .company_code_id
            == company_code.id
        )

    existing_rules = rules_query.all()

    time_rules = {
        rule.time_type: rule
        for rule in existing_rules
        if (
            rule.rule_type
            == InvoiceBillingRuleType
            .TIME_TYPE.value
            and rule.time_type
        )
    }

    task_rules = {
        rule.task_template_id: rule
        for rule in existing_rules
        if (
            rule.rule_type
            == InvoiceBillingRuleType
            .TASK_TEMPLATE.value
            and rule.task_template_id
            is not None
        )
    }

    task_template_query = (
        TimeTaskTemplate.query
    )

    if task_rules:
        task_template_query = (
            task_template_query.filter(
                or_(
                    TimeTaskTemplate.active.is_(
                        True
                    ),
                    TimeTaskTemplate.id.in_(
                        task_rules.keys()
                    ),
                )
            )
        )
    else:
        task_template_query = (
            task_template_query.filter(
                TimeTaskTemplate.active.is_(
                    True
                )
            )
        )

    task_templates = (
        task_template_query
        .order_by(
            TimeTaskTemplate.title.asc(),
            TimeTaskTemplate.code.asc(),
        )
        .all()
    )

    if request.method == "POST":
        try:
            save_invoice_time_rules(
                company_code,
                time_rules,
            )
            save_invoice_task_rules(
                company_code,
                task_rules,
                task_templates,
            )

            audit(
                "invoice_billing_rules.update",
                "wowi_company_code",
                (
                    company_code.id
                    if company_code
                    else None
                ),
                {
                    "scope": (
                        company_code.code
                        if company_code
                        else "global"
                    ),
                },
            )
            db.session.commit()

        except ValueError as exc:
            db.session.rollback()
            flash(
                str(exc),
                "danger",
            )

            return redirect(
                invoice_billing_rules_url(
                    company_code
                )
            )

        flash(
            "Abrechnungssätze gespeichert.",
            "success",
        )

        return redirect(
            invoice_billing_rules_url(
                company_code
            )
        )

    return render_template(
        "admin/invoice_billing_rules.html",
        company_codes=company_codes,
        company_code=company_code,
        time_types=INVOICE_TIME_TYPES,
        time_rules=time_rules,
        task_templates=task_templates,
        task_rules=task_rules,
    )


@admin_bp.post(
    "/admin/invoices/<int:invoice_id>/finalize"
)
@login_required
@invoice_required
def invoice_finalize(
    invoice_id: int,
) -> Any:
    invoice = Invoice.query.filter_by(id=invoice_id).populate_existing().with_for_update().first_or_404()
    document_result = None

    try:
        if request.form.get("pending_agreement_erp_id") or request.form.get("remove_agreement") == "1":
            raise ValueError("Bitte speichern Sie den Entwurf, um die Änderung am Mietvertrag zu übernehmen.")
        if request.form.get("save_draft") == "1":
            if invoice.status != InvoiceStatus.DRAFT.value:
                raise InvoiceFinalizationError("Nur Entwürfe können festgeschrieben werden.")
            save_invoice_draft_form(invoice)
            if invoice.recipient_license_agreement_erp_id is not None and not invoice.tenant_billing_reference:
                agreement = invoice_agreement(invoice, invoice.recipient_license_agreement_erp_id)
                invoice.recipient_license_agreement_idnum = agreement.id_num
            save_invoice_positions_form(invoice)
            audit("invoice.update", "invoice", invoice.id)

        document_result = finalize_invoice(
            invoice,
            current_user,
        )
        db.session.commit()
    except IntegrityError:
        db.session.rollback()

        if document_result is not None:
            remove_invoice_documents(
                document_result
            )

        current_app.logger.exception(
            "invoice_finalization_conflict "
            "invoice_id=%s",
            invoice_id,
        )
        flash(
            "Die Rechnungsnummer wurde "
            "gleichzeitig bereits vergeben. "
            "Bitte erneut versuchen.",
            "danger",
        )
    except (
        ValueError,
        InvoiceFinalizationError,
        InvoiceDocumentError,
    ) as exc:
        db.session.rollback()
        flash(
            str(exc),
            "danger",
        )
    except Exception:
        db.session.rollback()

        if document_result is not None:
            remove_invoice_documents(
                document_result
            )

        current_app.logger.exception(
            "invoice_finalization_failed "
            "invoice_id=%s",
            invoice_id,
        )
        flash(
            "Die Rechnung konnte nicht erstellt werden.",
            "danger",
        )
    else:
        flash(
            f"Rechnung {invoice.invoice_number} wurde erstellt. Die verknüpften "
            "Aufträge wurden zur Erledigung "
            "an Wowiport vorgemerkt.",
            "success",
        )

    return redirect(
        url_for(
            "admin.invoice_edit",
            invoice_id=invoice_id,
        )
    )


@admin_bp.route(
    "/admin/invoices/<int:invoice_id>/document", methods=["GET", "POST"],
)
@login_required
@invoice_required
def invoice_document(
    invoice_id: int,
) -> Any:
    saving = request.method == "POST"
    invoice = db.get_or_404(Invoice, invoice_id, populate_existing=saving, with_for_update=saving)

    if (
        invoice.status not in {
            InvoiceStatus.FINALIZED.value,
            InvoiceStatus.CANCELED.value,
        }
        or not invoice.document_filename
    ):
        abort(404)

    try:
        document_path = (
            resolve_invoice_document_path(
                invoice.document_filename
            )
        )
    except InvoiceDocumentError:
        abort(404)

    if not document_path.is_file():
        abort(404)

    document_label = (
        "Stornorechnung"
        if (
            invoice.invoice_kind
            == InvoiceKind.CANCELLATION.value
        )
        else "Rechnung"
    )

    filename = f"{document_label}-{invoice.invoice_number}.pdf"
    includes_tenant_letter = (
        invoice.tenant_billing_reference and invoice.invoice_kind == InvoiceKind.INVOICE.value
        and (invoice.status == InvoiceStatus.FINALIZED.value or invoice.tenant_charge_letter)
    )
    try:
        if includes_tenant_letter:
            content = invoice_pdf_with_tenant_letter(invoice, save_default=saving)
            return document_preview_response(content, filename)
        if invoice.cover_letter_title is not None:
            from .invoice_correspondence import invoice_correspondence_pdf

            return document_preview_response(invoice_correspondence_pdf(invoice), filename)
    except (ValueError, InvoiceDocumentError, WorkReportDocumentError) as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    except Exception:
        db.session.rollback()
        current_app.logger.exception("invoice_document_with_letter_failed invoice_id=%s", invoice_id)
        flash("Die vollständige Rechnungs-PDF konnte nicht erstellt werden. Bitte prüfen Sie das Anschreiben.", "danger")
    else:
        return send_file(document_path, mimetype="application/pdf", as_attachment=False, download_name=filename)

    if includes_tenant_letter and invoice.status == InvoiceStatus.FINALIZED.value:
        return redirect(url_for("admin.invoice_tenant_charge", invoice_id=invoice_id, complete=None if invoice.completed_at else 1))
    return redirect(url_for("admin.invoice_edit", invoice_id=invoice_id))


@admin_bp.get(
    "/admin/invoices/<int:invoice_id>/"
    "document-with-work-reports"
)
@login_required
@invoice_required
def invoice_document_with_work_reports(
    invoice_id: int,
) -> Any:
    invoice = db.get_or_404(
        Invoice,
        invoice_id,
    )
    scope = request.args.get(
        "scope",
        WORK_REPORT_SCOPE_BILLED,
    )

    try:
        document = (
            create_invoice_with_work_reports_pdf(
                invoice,
                scope,
            )
        )
    except WorkReportDocumentError as exc:
        db.session.rollback()
        flash(
            str(exc),
            "danger",
        )

        return redirect(
            url_for(
                "admin.invoice_edit",
                invoice_id=invoice.id,
            )
        )

    return send_file(
        document,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=(
            invoice_work_reports_filename(
                invoice
            )
        ),
    )


@admin_bp.get(
    "/admin/invoices/<int:invoice_id>/"
    "commissions/<int:commission_id>/"
    "work-report"
)
@login_required
@invoice_required
def invoice_commission_work_report(
    invoice_id: int,
    commission_id: int,
) -> Any:
    invoice = db.get_or_404(
        Invoice,
        invoice_id,
    )
    scope = request.args.get(
        "scope",
        WORK_REPORT_SCOPE_ALL,
    )

    try:
        groups = {
            commission.id: (
                commission,
                entries,
            )
            for commission, entries
            in invoice_work_report_groups(
                invoice,
                scope,
            )
        }
    except WorkReportDocumentError as exc:
        db.session.rollback()
        flash(
            str(exc),
            "danger",
        )

        return redirect(
            url_for(
                "admin.invoice_edit",
                invoice_id=invoice.id,
            )
        )

    group = groups.get(
        commission_id
    )

    if group is None:
        abort(404)

    commission, entries = group

    try:
        document = create_work_report_pdf(
            commission,
            entries,
            scope,
        )
    except WorkReportDocumentError as exc:
        db.session.rollback()
        flash(
            str(exc),
            "danger",
        )

        return redirect(
            url_for(
                "admin.invoice_edit",
                invoice_id=invoice.id,
            )
        )

    db.session.commit()

    return send_file(
        document,
        mimetype="application/pdf",
        as_attachment=False,
        download_name=work_report_filename(
            commission
        ),
    )


@admin_bp.get("/admin/commissions/<int:commission_id>/work-report")
@foreman_required
def commission_work_report(commission_id: int) -> Any:
    from .dms_client import DmsError
    from .dms_service import document_path
    from .models import DmsDocument

    commission = db.get_or_404(Commission, commission_id)
    entries = commission_work_report_entries(commission)
    fingerprint = work_report_fingerprint(
        commission, entries, WORK_REPORT_SCOPE_ALL,
    )

    stored_document = (
        DmsDocument.query
        .filter_by(
            entity_type="commission",
            entity_id=commission_id,
            document_type="work_report",
        )
        .filter(
            DmsDocument.context["scope"].as_string()
            == WORK_REPORT_SCOPE_ALL,
            DmsDocument.context["content_fingerprint"].as_string()
            == fingerprint,
        )
        .order_by(DmsDocument.created_at.desc())
        .first()
    )

    try:
        if stored_document is not None:
            document = document_path(stored_document)
        else:
            document = create_work_report_pdf(
                commission,
                entries,
                WORK_REPORT_SCOPE_ALL,
            )
            db.session.commit()
    except (WorkReportDocumentError, DmsError) as exc:
        db.session.rollback()
        flash(str(exc), "danger")
        return redirect(
            url_for(
                "admin.commission_detail",
                commission_id=commission.id,
            )
        )

    return send_file(
        document,
        mimetype="application/pdf",
        as_attachment=False,
        download_name=work_report_filename(commission),
    )


@admin_bp.post(
    "/admin/invoices/<int:invoice_id>/document/regenerate"
)
@admin_required
def invoice_document_regenerate(
    invoice_id: int,
) -> Any:
    from .invoice_dms_dispatch import existing_dispatch

    invoice = (
        Invoice.query
        .filter(Invoice.id == invoice_id)
        .with_for_update()
        .one_or_none()
    )

    if invoice is None:
        abort(404)

    redirect_url = url_for(
        "admin.invoice_edit",
        invoice_id=invoice.id,
    )

    if existing_dispatch(invoice.id, lock=True):
        flash("Für diese Rechnung wurde bereits eine DMS-Übergabe veranlasst.", "warning")
        return redirect(redirect_url)

    if invoice.status not in {
        InvoiceStatus.FINALIZED.value,
        InvoiceStatus.CANCELED.value,
    }:
        flash(
            "Nur festgeschriebene Rechnungen können "
            "neu erzeugt werden.",
            "danger",
        )
        return redirect(redirect_url)

    previous_document_filename = (
        invoice.document_filename
    )
    previous_document_sha256 = (
        invoice.document_sha256
    )
    previous_pdf_path = None
    previous_xml_path = None
    previous_pdf_bytes = None
    previous_xml_bytes = None

    if previous_document_filename:
        try:
            previous_pdf_path = (
                resolve_invoice_document_path(
                    previous_document_filename
                )
            )
            previous_xml_path = (
                previous_pdf_path.with_suffix(".xml")
            )

            if previous_pdf_path.is_file():
                previous_pdf_bytes = (
                    previous_pdf_path.read_bytes()
                )

            if previous_xml_path.is_file():
                previous_xml_bytes = (
                    previous_xml_path.read_bytes()
                )
        except (InvoiceDocumentError, OSError):
            current_app.logger.exception(
                "invoice_document_backup_failed "
                "invoice_id=%s",
                invoice.id,
            )
            flash(
                "Die bisherige Rechnungsdatei konnte "
                "nicht gesichert werden.",
                "danger",
            )
            return redirect(redirect_url)

    def restore_previous_documents() -> None:
        documents = (
            (
                previous_pdf_path,
                previous_pdf_bytes,
            ),
            (
                previous_xml_path,
                previous_xml_bytes,
            ),
        )

        for document_path, document_bytes in documents:
            if document_path is None:
                continue

            try:
                if document_bytes is None:
                    document_path.unlink(
                        missing_ok=True
                    )
                else:
                    document_path.parent.mkdir(
                        parents=True,
                        exist_ok=True,
                    )
                    document_path.write_bytes(
                        document_bytes
                    )
            except OSError:
                current_app.logger.exception(
                    "invoice_document_restore_failed "
                    "invoice_id=%s path=%s",
                    invoice.id,
                    document_path,
                )

    document_result = None

    try:
        if current_app.jinja_env.cache is not None:
            current_app.jinja_env.cache.clear()

        document_result = create_invoice_document(
            invoice
        )

        invoice.document_filename = (
            document_result.pdf_storage_name
        )
        invoice.document_sha256 = (
            document_result.pdf_sha256
        )

        audit(
            "invoice.document.regenerate",
            "invoice",
            invoice.id,
            {
                "invoice_number": (
                    invoice.invoice_number
                ),
                "previous_document_sha256": (
                    previous_document_sha256
                ),
                "document_sha256": (
                    document_result.pdf_sha256
                ),
                "xml_sha256": (
                    document_result.xml_sha256
                ),
            },
        )

        db.session.commit()
    except Exception as exc:
        db.session.rollback()

        if document_result is not None:
            remove_invoice_documents(
                document_result
            )

        restore_previous_documents()

        current_app.logger.exception(
            "invoice_document_regeneration_failed "
            "invoice_id=%s",
            invoice.id,
        )

        if isinstance(
            exc,
            InvoiceDocumentError,
        ):
            flash(str(exc), "danger")
        else:
            flash(
                "PDF und ZUGFeRD-XML konnten nicht "
                "neu erzeugt werden.",
                "danger",
            )

        return redirect(redirect_url)

    flash(
        "PDF und ZUGFeRD-XML wurden neu erzeugt.",
        "success",
    )
    return redirect(redirect_url)


@admin_bp.route(
    "/admin/craft-activities",
    methods=["GET", "POST"],
)
@admin_required
def craft_activities() -> Any:
    items = (
        CraftActivity.query
        .order_by(
            CraftActivity.name.asc(),
            CraftActivity.erp_id.asc(),
        )
        .all()
    )
    external_craftsmen = (
        available_external_craftsmen()
    )
    external_craft_ids_by_id = {
        craftsman.id: {
            activity.id
            for activity
            in craftsman.craft_activities
        }
        for craftsman in external_craftsmen
    }
    external_craftsman_options_by_craft_id = {
        item.id: [
            craftsman
            for craftsman in external_craftsmen
            if (
                not external_craft_ids_by_id[
                    craftsman.id
                ]
                or item.id
                in external_craft_ids_by_id[
                    craftsman.id
                ]
            )
        ]
        for item in items
    }

    if request.method == "POST":
        enabled_ids = set()

        for value in request.form.getlist(
            "enabled_craft_activity_ids"
        ):
            try:
                enabled_ids.add(int(value))
            except ValueError:
                continue

        preferred_by_craft_id = {}

        for item in items:
            raw_preferred_id = str(
                request.form.get(
                    "preferred_external_"
                    f"craftsman_id_{item.id}"
                )
                or ""
            ).strip()

            if not raw_preferred_id:
                preferred_by_craft_id[
                    item.id
                ] = None
                continue

            try:
                preferred_id = int(
                    raw_preferred_id
                )
            except ValueError:
                flash(
                    "Die externe Zuordnung für "
                    f"„{item.name}“ ist ungültig.",
                    "danger",
                )
                return redirect(
                    url_for(
                        "admin.craft_activities"
                    )
                )

            preferred_craftsman = next(
                (
                    craftsman
                    for craftsman
                    in external_craftsman_options_by_craft_id[
                        item.id
                    ]
                    if craftsman.id
                    == preferred_id
                ),
                None,
            )

            if preferred_craftsman is None:
                flash(
                    "Der bevorzugte externe "
                    "Handwerker ist für das Gewerk "
                    f"„{item.name}“ nicht verfügbar.",
                    "danger",
                )
                return redirect(
                    url_for(
                        "admin.craft_activities"
                    )
                )

            preferred_by_craft_id[
                item.id
            ] = preferred_craftsman

        for item in items:
            item.app_enabled = (
                item.active
                and item.id in enabled_ids
            )
            item.preferred_external_craftsman = (
                preferred_by_craft_id[
                    item.id
                ]
            )

        enabled_count = sum(
            1
            for item in items
            if item.app_enabled
        )
        preferred_count = sum(
            1
            for item in items
            if (
                item
                .preferred_external_craftsman_id
                is not None
            )
        )

        audit(
            "craft_activities.app_access.update",
            metadata={
                "enabled_count": enabled_count,
                "preferred_external_count": (
                    preferred_count
                ),
                "total_count": len(items),
            },
        )
        db.session.commit()

        flash(
            "Die Gewerkeinstellungen wurden "
            "gespeichert.",
            "success",
        )

        return redirect(
            url_for(
                "admin.craft_activities"
            )
        )

    return render_template(
        "admin/craft_activities.html",
        items=items,
        external_craftsman_options_by_craft_id=(
            external_craftsman_options_by_craft_id
        ),
    )


@admin_bp.route(
    "/admin/craft-department-mappings",
    methods=["GET", "POST"],
)
@admin_required
def craft_department_mappings() -> Any:
    craft_activities = (
        CraftActivity.query
        .filter(
            CraftActivity.active.is_(True),
            CraftActivity.app_enabled.is_(True),
        )
        .order_by(
            CraftActivity.name.asc()
        )
        .all()
    )
    departments = (
        WowiDepartment.query
        .filter_by(
            active=True
        )
        .order_by(
            WowiDepartment.name.asc()
        )
        .all()
    )
    mappings = {
        item.craft_activity_id: item
        for item in (
            CraftActivityDepartmentMapping
            .query
            .all()
        )
    }

    allowed_assignment_types = {
        item.value
        for item in CommissionAssignmentType
    }

    if request.method == "POST":
        departments_by_id = {
            item.id: item
            for item in departments
        }
        selected_departments = {}
        selected_assignment_types = {}

        for craft_activity in craft_activities:
            assignment_type = str(
                request.form.get(
                    (
                        "default_assignment_type_"
                        f"{craft_activity.id}"
                    ),
                    (
                            craft_activity
                            .default_assignment_type
                            or CommissionAssignmentType
                            .INTERNAL.value
                    ),
                )
            ).strip().casefold()

            if (
                    assignment_type
                    not in allowed_assignment_types
            ):
                flash(
                    "Die ausgewählte Standardvergabe "
                    "ist ungültig.",
                    "danger",
                )
                return redirect(
                    url_for(
                        "admin."
                        "craft_department_mappings"
                    )
                )

            selected_assignment_types[
                craft_activity.id
            ] = assignment_type

            raw_value = request.form.get(
                (
                    "department_"
                    f"{craft_activity.id}"
                ),
                "",
            ).strip()

            if not raw_value:
                continue

            try:
                department_id = int(
                    raw_value
                )
            except ValueError:
                flash(
                    "Die ausgewählte Abteilung "
                    "ist ungültig.",
                    "danger",
                )
                return redirect(
                    url_for(
                        "admin."
                        "craft_department_mappings"
                    )
                )

            department = departments_by_id.get(
                department_id
            )

            if department is None:
                flash(
                    "Die ausgewählte Abteilung "
                    "ist nicht verfügbar.",
                    "danger",
                )
                return redirect(
                    url_for(
                        "admin."
                        "craft_department_mappings"
                    )
                )

            selected_departments[
                craft_activity.id
            ] = department

        for craft_activity in craft_activities:
            craft_activity.default_assignment_type = (
                selected_assignment_types[
                    craft_activity.id
                ]
            )
            mapping = mappings.get(
                craft_activity.id
            )
            department = selected_departments.get(
                craft_activity.id
            )

            if department is None:
                if mapping is not None:
                    db.session.delete(mapping)

                continue

            if mapping is None:
                mapping = (
                    CraftActivityDepartmentMapping(
                        craft_activity=(
                            craft_activity
                        ),
                    )
                )
                db.session.add(mapping)

            mapping.department = department
            mapping.updated_by = current_user
            mapping.updated_at = utc_now()

        audit(
            "craft_department_mappings.update",
            metadata={
                "craft_activity_count": len(
                    craft_activities
                ),
            },
        )
        db.session.commit()

        flash(
            "Gewerk-Zuordnungen gespeichert.",
            "success",
        )

        return redirect(
            url_for(
                "admin."
                "craft_department_mappings"
            )
        )

    return render_template(
        "admin/craft_department_mappings.html",
        craft_activities=craft_activities,
        departments=departments,
        mappings=mappings,
    )


@admin_bp.post(
    "/admin/object-directory/sync"
)
@admin_required
def object_directory_sync() -> Any:
    try:
        result = sync_object_directory()
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "object_directory_sync_failed"
        )
        flash(
            "Die Wowiport-Objektdaten "
            "konnten nicht synchronisiert werden.",
            "danger",
        )
        return redirect(
            url_for(
                "admin."
                "craft_department_mappings"
            )
        )

    audit(
        "object_directory.sync",
        metadata=result,
    )
    db.session.commit()

    flash(
        "Objektdaten synchronisiert: "
        f"{result['economic_units']} "
        "Wirtschaftseinheiten, "
        f"{result['jurisdictions']} "
        "Zuständigkeiten, "
        f"{result['buildings']} Gebäude, "
        f"{result['use_units']} "
        "Nutzungseinheiten, "
        f"{result['responsible_officials']} Sachbearbeiter.",
        "success",
    )

    return redirect(
        url_for(
            "admin.craft_department_mappings"
        )
    )


@admin_bp.get("/admin/users")
@admin_required
def users() -> Any:
    items = User.query.order_by(User.display_name.asc()).all()
    return render_template("admin/users.html", items=items, entra_enabled=settings.get(SettingKeys.MICROSOFT_LOGIN_ENABLED))


@admin_bp.route("/admin/users/import-entra", methods=["POST"])
@admin_required
def import_users_from_entra() -> Any:
    if not settings.get(SettingKeys.MICROSOFT_LOGIN_ENABLED):
        abort(403)
    try:
        result = import_entra_users()
        audit("users.import_entra", metadata={"created": result.created, "updated": result.updated, "skipped": result.skipped})
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("users_import_entra_failed")
        flash("Die Synchronisierung mit Entra ist fehlgeschlagen. Bitte die Entra-Konfiguration prüfen und erneut versuchen.", "danger")
    else:
        flash(f"Synchronisierung abgeschlossen: {result.created} neu, {result.updated} aktualisiert, {result.skipped} übersprungen.", "success")
    return redirect(url_for("admin.users"))


@admin_bp.route("/admin/users/new", methods=["GET", "POST"])
@admin_required
def user_create() -> Any:
    item = User(email="", display_name="")
    craft_activities = (
        CraftActivity.query
        .filter(
            CraftActivity.active.is_(True),
            CraftActivity.app_enabled.is_(True),
        )
        .order_by(
            CraftActivity.name.asc()
        )
        .all()
    )

    if request.method == "POST":
        try:
            db.session.add(item)
            save_user_form(item)
            save_user_craft_activities(item)
            db.session.flush()
            save_user_work_time_profile_assignment(item)
            visible_user_ids = (
                save_user_commission_visibility(
                    item
                )
            )
            db.session.flush()
            audit(
                "user.create",
                "user",
                item.id,
                metadata={
                    "visible_commission_user_ids": (
                        visible_user_ids
                    ),
                },
            )
            db.session.commit()
        except (ValueError, IntegrityError) as exc:
            db.session.rollback()
            flash(
                (
                    str(exc)
                    if isinstance(exc, ValueError)
                    else (
                        "Der Benutzer konnte nicht "
                        "gespeichert werden."
                    )
                ),
                "danger",
            )
        else:
            flash(
                "Benutzer gespeichert.",
                "success",
            )
            return redirect(
                url_for(
                    "admin.user_edit",
                    user_id=item.id,
                )
            )

    return render_template(
        "admin/user_form.html",
        item=item,
        craft_activities=craft_activities,
        selected_craft_activities=[],
        **user_work_time_profile_context(item),
        **user_commission_visibility_context(item),
        push_preference_rows=admin_push_preference_rows(item),
        approval_user_choices=approval_user_choices(item),
    )


@admin_bp.route(
    "/admin/users/<int:user_id>",
    methods=["GET", "POST"],
)
@admin_required
def user_edit(user_id: int) -> Any:
    item = db.get_or_404(User, user_id)
    craft_activities = (
        CraftActivity.query
        .filter(
            CraftActivity.active.is_(True),
            CraftActivity.app_enabled.is_(True),
        )
        .order_by(
            CraftActivity.name.asc()
        )
        .all()
    )

    if request.method == "POST":
        try:
            save_user_form(item)
            save_user_craft_activities(item)
            visible_user_ids = (
                save_user_commission_visibility(
                    item
                )
            )
            db.session.flush()
            save_user_work_time_profile_assignment(item)
            mapping_ids = (
                save_user_jurisdiction_person_mappings(
                    item
                )
            )
            db.session.flush()

            assignment_result = (
                reconcile_safety_inspection_assignments()
            )

            audit(
                "user.update",
                "user",
                item.id,
                metadata={
                    (
                        "jurisdiction_person_"
                        "erp_ids"
                    ): mapping_ids,
                    (
                        "safety_inspection_"
                        "assignments"
                    ): assignment_result,
                    "visible_commission_user_ids": (
                        visible_user_ids
                    ),
                },
            )
            db.session.commit()
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
        except IntegrityError:
            db.session.rollback()
            flash(
                "Die Wowiport-Person ist bereits "
                "einem anderen Benutzer "
                "zugeordnet.",
                "danger",
            )
        else:
            flash(
                "Benutzer gespeichert. "
                f"{assignment_result['assigned']} "
                "offene Prüfaufträge wurden "
                "neu zugeordnet.",
                "success",
            )
            return redirect(
                url_for(
                    "admin.user_edit",
                    user_id=item.id,
                )
            )

        item = db.get_or_404(User, user_id)

    selected_craft_activities = [
        activity
        for activity in item.craft_activities
        if (
            activity.active
            and activity.app_enabled
        )
    ]
    jurisdiction_candidates = (
        jurisdiction_person_candidates()
    )
    selected_jurisdiction_person_ids = {
        mapping.jurisdiction_person_erp_id
        for mapping in (
            UserJurisdictionPersonMapping.query
            .filter_by(
                user_id=item.id,
                active=True,
            )
            .all()
        )
    }

    return render_template(
        "admin/user_form.html",
        item=item,
        craft_activities=craft_activities,
        selected_craft_activities=(
            selected_craft_activities
        ),
        jurisdiction_candidates=(
            jurisdiction_candidates
        ),
        selected_jurisdiction_person_ids=(
            selected_jurisdiction_person_ids
        ),
        **user_work_time_profile_context(item),
        **user_commission_visibility_context(item),
        push_preference_rows=admin_push_preference_rows(item),
        approval_user_choices=approval_user_choices(item),
    )


def save_web_commission_upload(
    commission: Commission,
    upload: Any,
    caption: str | None,
    stored_paths: list[Path],
    as_attachment: bool = False,
) -> tuple[CommissionPhoto | CommissionAttachment, bool]:
    raw_data = upload.stream.read(MAX_ATTACHMENT_BYTES + 1)
    if not raw_data:
        raise ValueError("Die Datei ist leer.")
    if len(raw_data) > MAX_ATTACHMENT_BYTES:
        raise ValueError("Die Datei darf höchstens 25 MB groß sein.")
    if not as_attachment:
        try:
            render_ticket_photo(raw_data, thumbnail=True)
        except ValueError as exc:
            if Path(upload.filename).suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff"} and not raw_data.lstrip().startswith(b"%PDF-"):
                raise ValueError("Die Bilddatei konnte nicht gelesen werden.") from exc
        else:
            upload.stream.seek(0)
            return save_web_commission_photo(commission, upload, caption, stored_paths)

    stored = store_commission_attachment_bytes(commission, raw_data, upload.filename)
    paths = [commission_attachment_path(stored.storage_name)]
    if stored.thumbnail_storage_name:
        paths.append(commission_attachment_path(stored.thumbnail_storage_name))
    stored_paths.extend(paths)
    existing = CommissionAttachment.query.filter_by(
        commission_id=commission.id, content_sha256=stored.content_sha256, active=True,
    ).first()
    if existing is not None:
        for path in paths:
            path.unlink(missing_ok=True)
        return existing, True
    mime_type = stored.mime_type
    if mime_type.startswith("image/") or (mime_type == "application/pdf" and not raw_data.lstrip().startswith(b"%PDF-")):
        mime_type = "application/octet-stream"
    attachment = CommissionAttachment(
        commission=commission, uploaded_by_user_id=current_user.id, source="web",
        source_entity_name="Commission", source_entity_id=commission.erp_id,
        original_filename=stored.original_filename, storage_name=stored.storage_name,
        thumbnail_storage_name=stored.thumbnail_storage_name, mime_type=mime_type,
        size_bytes=stored.size_bytes, content_sha256=stored.content_sha256,
        caption=str(caption or "").strip()[:500] or None, active=True,
        erp_sync_state=CommissionSyncState.PENDING.value,
    )
    db.session.add(attachment)
    db.session.flush()
    if commission.erp_id is not None:
        enqueue_erp_operation(
            commission, ErpOperationType.UPLOAD_COMMISSION_ATTACHMENT,
            f"commission-attachment:{attachment.external_document_id}", {"attachment_id": attachment.id},
        )
    audit("commission.attachment.upload", "commission", commission.id, {
        "attachment_id": attachment.id, "filename": attachment.original_filename, "source": "web",
    })
    return attachment, False


def save_web_commission_photo(
    commission: Commission,
    upload: Any,
    caption: str | None,
    stored_paths: list[Path] | None = None,
) -> tuple[CommissionPhoto, bool]:
    stored_photo = store_commission_photo(
        commission,
        upload,
    )

    existing_photo = (
        CommissionPhoto.query
        .filter(
            CommissionPhoto.commission_id
            == commission.id,
            CommissionPhoto.context == "order",
            CommissionPhoto.content_sha256
            == stored_photo.content_sha256,
            CommissionPhoto.active.is_(True),
        )
        .one_or_none()
    )

    if existing_photo is not None:
        commission_photo_path(
            stored_photo.storage_name
        ).unlink(missing_ok=True)

        commission_photo_path(
            stored_photo.thumbnail_storage_name
        ).unlink(missing_ok=True)

        return existing_photo, True

    if stored_paths is not None:
        stored_paths.extend((
            commission_photo_path(stored_photo.storage_name),
            commission_photo_path(stored_photo.thumbnail_storage_name),
        ))

    normalized_caption = (
        str(caption or "").strip()[:500]
        or None
    )

    photo = CommissionPhoto(
        commission=commission,
        uploaded_by_user_id=current_user.id,
        source="web",
        context="order",
        external_document_id=(
            "datlaeuft-photo-"
            f"{uuid4().hex}"
        ),
        external_entity_name="Commission",
        external_entity_id=commission.erp_id,
        original_filename=(
            stored_photo.original_filename
        ),
        storage_name=stored_photo.storage_name,
        thumbnail_storage_name=(
            stored_photo.thumbnail_storage_name
        ),
        mime_type=stored_photo.mime_type,
        size_bytes=stored_photo.size_bytes,
        content_sha256=(
            stored_photo.content_sha256
        ),
        caption=normalized_caption,
        active=True,
        erp_sync_state=(
            CommissionSyncState.PENDING.value
        ),
    )

    db.session.add(photo)
    db.session.flush()

    if commission.erp_id is not None:
        enqueue_erp_operation(
            commission,
            ErpOperationType.UPLOAD_COMMISSION_PHOTO,
            (
                "commission-photo:"
                f"{photo.external_document_id}"
            ),
            {
                "photo_id": photo.id,
            },
        )

    audit(
        "commission.photo.upload",
        "commission",
        commission.id,
        {
            "photo_id": photo.id,
            "filename": photo.original_filename,
            "caption": photo.caption,
            "source": "web",
        },
    )

    return photo, False


def ticket_owner_names(owner_ids: set[int]) -> dict[int, str]:
    owner_ids = set(owner_ids) - blacklist_ids("erp_user")
    if not owner_ids:
        return {}

    rows = []
    for model, user_column, person_column, name_column in (
        (WowiResponsibleOfficial,
         WowiResponsibleOfficial.user_erp_id,
         WowiResponsibleOfficial.person_erp_id,
         WowiResponsibleOfficial.name),
        (WowiEconomicUnitJurisdiction,
         WowiEconomicUnitJurisdiction.responsible_user_erp_id,
         WowiEconomicUnitJurisdiction.responsible_person_erp_id,
         WowiEconomicUnitJurisdiction.responsible_name),
        (WowiEconomicUnit,
         WowiEconomicUnit.universal_responsible_user_erp_id,
         WowiEconomicUnit.universal_responsible_person_erp_id,
         WowiEconomicUnit.universal_responsible_name),
    ):
        rows.extend(
            db.session.query(
                user_column, person_column, name_column, model.active, model.synced_at
            ).filter(user_column.in_(owner_ids)).distinct().all()
        )

    missing_person_ids = {
        row[1] for row in rows
        if row[1] is not None and not str(row[2] or "").strip()
    }
    fallback_names = {}
    if missing_person_ids:
        snapshots = db.session.query(
            UserJurisdictionPersonMapping.jurisdiction_person_erp_id,
            UserJurisdictionPersonMapping.person_name_snapshot,
            User.display_name,
        ).join(UserJurisdictionPersonMapping.user).filter(
            UserJurisdictionPersonMapping.jurisdiction_person_erp_id.in_(missing_person_ids)
        ).all()
        for person_id, snapshot, display_name in snapshots:
            fallback_names[person_id] = (
                str(snapshot or "").strip() or str(display_name or "").strip()
            )
        users = User.query.filter(User.person_erp_id.in_(missing_person_ids)).order_by(User.id).all()
        for user in users:
            name = str(user.display_name or "").strip()
            if name:
                fallback_names.setdefault(user.person_erp_id, name)

    rows.sort(
        key=lambda row: (
            bool(row[3]),
            bool(str(row[2] or "").strip()),
            row[4].isoformat() if row[4] else "",
        ),
        reverse=True,
    )
    names = {}
    for user_id, person_id, name, active, synced_at in rows:
        name = str(name or "").strip() or fallback_names.get(person_id, "")
        if name:
            names.setdefault(user_id, name)
    missing_ids = sorted(owner_ids - names.keys())
    if missing_ids:
        current_app.logger.warning("ticket_owner_names_unresolved user_ids=%s", missing_ids)
    return names


def ticket_owner_sort_key(name: str) -> tuple[str, str]:
    last_name = name.split(",", 1)[0] if "," in name else name.rsplit(" ", 1)[-1]
    translation = str.maketrans("äöü", "aou")
    return (
        last_name.strip().casefold().translate(translation),
        name.casefold().translate(translation),
    )


def ticket_owner_erp_ids(user: User) -> set[int]:
    person_ids = {
        mapping.jurisdiction_person_erp_id
        for mapping in user.jurisdiction_person_mappings
        if mapping.active
    }

    if user.person_erp_id is not None:
        manual_mapping = (
            UserJurisdictionPersonMapping.query
            .filter_by(
                jurisdiction_person_erp_id=user.person_erp_id,
                active=True,
            )
            .one_or_none()
        )
        if manual_mapping is None or manual_mapping.user_id == user.id:
            person_ids.add(user.person_erp_id)

    if not person_ids:
        return set()

    jurisdiction_ids = db.session.execute(
        select(
            WowiEconomicUnitJurisdiction.responsible_user_erp_id
        )
        .join(
            WowiEconomicUnit,
            WowiEconomicUnit.id
            == WowiEconomicUnitJurisdiction.economic_unit_id,
        )
        .where(
            WowiEconomicUnit.active.is_(True),
            WowiEconomicUnit.universal_responsibility.is_(False),
            WowiEconomicUnitJurisdiction.active.is_(True),
            WowiEconomicUnitJurisdiction.responsible_person_erp_id.in_(
                person_ids
            ),
        )
    ).scalars().all()

    universal_ids = db.session.execute(
        select(WowiEconomicUnit.universal_responsible_user_erp_id)
        .where(
            WowiEconomicUnit.active.is_(True),
            WowiEconomicUnit.universal_responsibility.is_(True),
            WowiEconomicUnit.universal_responsible_person_erp_id.in_(
                person_ids
            ),
        )
    ).scalars().all()

    directory_ids = db.session.execute(
        select(WowiResponsibleOfficial.user_erp_id).where(
            WowiResponsibleOfficial.active.is_(True),
            WowiResponsibleOfficial.person_erp_id.in_(person_ids),
        )
    ).scalars().all()

    return {
        user_id
        for user_id in (*jurisdiction_ids, *universal_ids, *directory_ids)
        if user_id is not None
    }


def ticket_filter_options(query: Any) -> dict[str, dict[str, str]]:
    rows = query.with_entities(
        TicketMirror.assigned_user_erp_id,
        TicketMirror.department_erp_id,
        TicketMirror.department_name,
        TicketMirror.priority_erp_id,
        TicketMirror.priority_code,
    ).distinct().all()
    owner_ids = {row[0] for row in rows if row[0] is not None} - blacklist_ids("erp_user")
    owner_names = ticket_owner_names(owner_ids)

    departments = {}
    priorities = {}
    priority_labels = {
        "low": "Niedrige Priorität", "lowpriority": "Niedrige Priorität",
        "normal": "Normale Priorität", "medium": "Normale Priorität",
        "normalpriority": "Normale Priorität", "mediumpriority": "Normale Priorität",
        "high": "Hohe Priorität", "highpriority": "Hohe Priorität",
        "urgent": "Dringend",
    }

    for _, department_id, department_name, priority_id, priority_code in rows:
        if department_id is not None:
            key = str(department_id)
            name = str(department_name or "").strip()
            if name:
                departments[key] = name
            else:
                departments.setdefault(key, f"Abteilungs-ID {department_id}")

        if priority_id is not None:
            code = str(priority_code or "").strip().casefold().replace("_", "").replace(" ", "")
            label = priority_labels.get(code)
            if label:
                priorities[str(priority_id)] = label
            else:
                priorities.setdefault(str(priority_id), f"Priorität {priority_id}")

    owners = {
        f"erp:{owner_id}": (
                owner_names.get(owner_id) or f"Name nicht verfügbar (Benutzer-ID {owner_id})"
        )
        for owner_id in sorted(
            owner_ids,
            key=lambda owner_id: (
                owner_id not in owner_names,
                ticket_owner_sort_key(owner_names.get(owner_id, "")),
                owner_id,
            ),
        )
    }
    options = {
        "owner": {"mine": "Meine Tickets", "all": "Alle Sachbearbeiter", "unassigned": "Nicht zugewiesen"},
        "state": {"open": "Offen ohne Auftrag", "all": "Alle Bearbeitungsstände", "new": "Zu prüfen", "ignored": "Kein Auftrag", "converted": "Auftrag erstellt"},
        "priority": {"all": "Alle Prioritäten", "unassigned": "Ohne Priorität"},
        "department": {"all": "Alle Abteilungen", "unassigned": "Ohne Abteilung"},
        "ai_state": {
            "all": "Alle Analysestände",
            "not_requested": "Nicht analysiert",
            "queued": "Vorbereitung vorgemerkt",
            "processing": "Vorschlag wird erstellt",
            "ready": "Vorschlag erstellt",
            "stale": "Vorschlag veraltet",
            "failed": "Analyse fehlgeschlagen",
        },
    }

    options["owner"].update(owners)
    for key, choices in (
            ("priority", priorities),
            ("department", departments),
    ):
        options[key].update(
            sorted(choices.items(), key=lambda item: (item[1].casefold(), item[0]))
        )

    return options


@admin_bp.get("/admin/tickets")
@foreman_required
def tickets() -> Any:
    search_text = request.args.get(
        "q",
        "",
    ).strip()
    page = max(
        request.args.get(
            "page",
            1,
            type=int,
        ),
        1,
    )

    query = (
        TicketMirror.query
        .options(
            selectinload(
                TicketMirror.commissions
            )
        )
        .filter(
            TicketMirror.deleted_at.is_(None)
        )
    )

    filter_options = ticket_filter_options(query)
    defaults = {
        "owner": "mine",
        "state": "open",
        "priority": "all",
        "department": "all",
        "ai_state": "all",
    }
    selected_filters = {
        key: request.args.get(key, default).strip()
        for key, default in defaults.items()
    }

    for key, value in selected_filters.items():
        if value not in filter_options[key]:
            selected_filters[key] = "mine" if key == "owner" else "all"

    selected_owners = list(dict.fromkeys(
        value.strip()
        for value in request.args.getlist("owner")
        if value.strip() in filter_options["owner"]
    )) or ["mine"]
    if "all" in selected_owners:
        selected_owners = ["all"]
    selected_owner = selected_owners[0] if len(selected_owners) == 1 else "multiple"
    selected_filters["owner"] = selected_owner
    selected_state = selected_filters["state"]
    owner_ids = (
        ticket_owner_erp_ids(current_user)
        if "mine" in selected_owners
        else set()
    )

    if "all" not in selected_owners:
        selected_owner_ids = owner_ids | {
            int(value[4:]) for value in selected_owners if value.startswith("erp:")
        }
        owner_conditions = [TicketMirror.assigned_user_erp_id.in_(selected_owner_ids)]
        if "unassigned" in selected_owners:
            owner_conditions.append(TicketMirror.assigned_user_erp_id.is_(None))
        query = query.filter(or_(*owner_conditions))

    if selected_state == "open":
        query = open_tickets_query(query)
    elif selected_state != "all":
        query = query.filter(TicketMirror.state == selected_state)

    for key, column in (
            ("priority", TicketMirror.priority_erp_id),
            ("department", TicketMirror.department_erp_id),
    ):
        selected_value = selected_filters[key]
        if selected_value == "unassigned":
            query = query.filter(column.is_(None))
        elif selected_value != "all":
            query = query.filter(column == int(selected_value))

    if selected_filters["ai_state"] != "all":
        query = query.filter(
            TicketMirror.ai_state == selected_filters["ai_state"]
        )

    if search_text:
        like_term = f"%{search_text}%"
        conditions = [
            TicketMirror.id_num.ilike(like_term),
            TicketMirror.subject.ilike(like_term),
            TicketMirror.content_excerpt.ilike(
                like_term
            ),
        ]

        if search_text.isdigit():
            conditions.append(
                TicketMirror.erp_id
                == int(search_text)
            )

        query = query.filter(
            or_(*conditions)
        )

    ordered_query = query.order_by(
        TicketMirror.received_at.desc(),
        TicketMirror.id.desc(),
    )
    pagination = ordered_query.paginate(
        page=page,
        per_page=50,
        error_out=False,
    )
    if pagination.page > max(pagination.pages, 1):
        pagination = ordered_query.paginate(page=max(pagination.pages, 1), per_page=50, error_out=False)
    pagination_filters = {**selected_filters, "owner": selected_owners, "q": search_text}
    ticket_list_url = url_for("admin.tickets", page=pagination.page, **pagination_filters)

    openai_configured = bool(
        settings.get(
            SettingKeys.OPENAI_ENABLED
        )
        and str(
            settings.get(
                SettingKeys.OPENAI_API_KEY
            )
        ).strip()
        and str(
            settings.get(
                SettingKeys.OPENAI_MODEL
            )
        ).strip()
    )

    reply_notifications = WorkflowNotification.query.filter(
        WorkflowNotification.kind == "ticket_reply",
        WorkflowNotification.ticket_id.in_([ticket.id for ticket in pagination.items]),
    ).order_by(WorkflowNotification.id).all() if pagination.items else []
    return render_template(
        "admin/tickets.html",
        ticket_reply_notifications={item.ticket_id: item for item in reply_notifications},
        ticket_object_details=ticket_list_object_details(pagination.items),
        pagination=pagination,
        selected_state=selected_state,
        search_text=search_text,
        openai_configured=openai_configured,
        selected_owner=selected_owner,
        selected_owners=selected_owners,
        selected_owner_label=(
            filter_options["owner"][selected_owners[0]]
            if len(selected_owners) == 1 else f"{len(selected_owners)} ausgewählt"
        ),
        owner_mapping_available=bool(owner_ids),
        ticket_filters=[
            (key, label, selected_filters[key], filter_options[key])
            for key, label in (
                ("owner", "Sachbearbeiter"),
                ("state", "Bearbeitungsstand"),
                ("priority", "Priorität"),
                ("department", "Abteilung"),
                ("ai_state", "Analysestatus"),
            )
        ],
        ticket_owner_labels=filter_options["owner"],
        pagination_filters=pagination_filters,
        ticket_list_url=ticket_list_url,
    )


@admin_bp.get(
    "/admin/tickets/<int:ticket_id>/commissions"
)
@foreman_required
def ticket_commissions(
    ticket_id: int,
) -> Any:
    ticket = (
        TicketMirror.query
        .options(
            selectinload(
                TicketMirror.commissions
            )
        )
        .filter(
            TicketMirror.id == ticket_id,
            TicketMirror.deleted_at.is_(None),
        )
        .one_or_none()
    )

    if ticket is None:
        abort(404)

    local_status_labels = {
        "recorded": "Erfasst",
        "waiting_for_approval": "Wartet auf Freigabe",
        "waiting_for_acceptance": (
            "Warte auf Annahme"
        ),
        "open": "Beauftragt",
        "in_progress": "In Bearbeitung",
        "completion_review": (
            "Nicht abgeschlossen"
        ),
        "tenant_charge_review": (
            "Mieterbelastung"
        ),
        "done": "Erledigt",
        "awaiting_invoice": "Rechnungseingang erwartet",
        "deleted": "Gelöscht",
    }

    return render_template(
        "admin/ticket_commissions.html",
        ticket_list_url=ticket_list_return_url(),
        ticket=ticket,
        commissions=ticket.commissions,
        local_status_labels=local_status_labels,
    )


@admin_bp.post("/admin/tickets/<int:ticket_id>/analyze")
@foreman_required
def ticket_analyze(ticket_id: int) -> Any:
    ticket = db.get_or_404(TicketMirror, ticket_id)
    if ticket.deleted_at is not None or ticket.state == TicketMirrorState.IGNORED.value:
        abort(404)
    if ticket_ai_is_current(ticket):
        return redirect(url_for("admin.commission_create", ticket_id=ticket.id, next=ticket_list_return_url()))
    if not enqueue_ticket_ai(
        ticket, current_user.id, automatic=False,
        force=ticket.ai_state == TicketAiState.FAILED.value,
    ):
        flash("Die KI-Analyse ist nicht vollständig konfiguriert.", "warning")
        return redirect(ticket_list_return_url())
    db.session.commit()
    return redirect(url_for("admin.ticket_analysis_status", ticket_id=ticket.id, next=ticket_list_return_url()))


@admin_bp.post("/admin/tickets/<int:ticket_id>/regenerate")
@foreman_required
def ticket_regenerate(ticket_id: int) -> Any:
    ticket = db.get_or_404(TicketMirror, ticket_id)
    if ticket.deleted_at is not None or ticket.state == TicketMirrorState.IGNORED.value:
        abort(404)
    if not enqueue_ticket_ai(ticket, current_user.id, automatic=False, force=True):
        flash("Die KI-Analyse ist nicht vollständig konfiguriert.", "warning")
        return redirect(ticket_list_return_url())
    audit("ticket.ai.regenerate", "ticket", ticket.erp_id)
    db.session.commit()
    return redirect(url_for("admin.ticket_analysis_status", ticket_id=ticket.id, next=ticket_list_return_url()))


@admin_bp.get("/admin/tickets/<int:ticket_id>/analysis")
@foreman_required
def ticket_analysis_status(ticket_id: int) -> Any:
    ticket = db.get_or_404(TicketMirror, ticket_id)
    if ticket.deleted_at is not None:
        abort(404)
    if ticket_ai_is_current(ticket):
        return redirect(url_for("admin.commission_create", ticket_id=ticket.id, next=ticket_list_return_url()))
    job = db.session.get(TicketAiJob, ticket.id)
    return render_template(
        "admin/ticket_analysis_status.html",
        ticket_list_url=ticket_list_return_url(),
        ticket=ticket,
        pending=job is not None and (job.locked_at is not None or job.attempt_count < 3),
    )


@admin_bp.post(
    "/admin/tickets/<int:ticket_id>/ignore"
)
@foreman_required
def ticket_ignore(
    ticket_id: int,
) -> Any:
    ticket = db.get_or_404(
        TicketMirror,
        ticket_id,
    )

    if (
        ticket.state
        != TicketMirrorState.CONVERTED.value
    ):
        ticket.state = (
            TicketMirrorState.IGNORED.value
        )
        audit(
            "ticket.ignore",
            "ticket",
            ticket.erp_id,
        )
        db.session.commit()

    return redirect(ticket_list_return_url())


@admin_bp.get("/admin/tickets/<int:ticket_id>/reply-recipients")
@foreman_required
def ticket_reply_recipient_options(ticket_id: int) -> Any:
    ticket = db.get_or_404(TicketMirror, ticket_id)
    try:
        return jsonify({"recipients": ticket_reply_recipients(ticket)})
    except Exception:
        current_app.logger.exception("ticket_reply_recipient_lookup_failed ticket_id=%s", ticket_id)
        return jsonify({"message": "Die Empfängeradresse konnte nicht geladen werden. Bitte prüfen und eintragen."}), 502


@admin_bp.post("/admin/tickets/<int:ticket_id>/ignore-with-reply")
@foreman_required
def ticket_ignore_with_reply(ticket_id: int) -> Any:
    ticket = TicketMirror.query.filter_by(id=ticket_id).with_for_update().populate_existing().first_or_404()
    try:
        notification = enqueue_ticket_reply(ticket, request.form.get("recipient", ""), request.form.get("response_text", ""))
        audit("ticket.ignore_with_reply", "ticket", ticket.erp_id, {"notification_id": notification.id})
        db.session.commit()
        flash("Die Antwort wurde zum E-Mail-Versand und zur Dokumentation im Wowiport-Ticket vorgemerkt.", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return redirect(ticket_list_return_url())


@admin_bp.post(
    "/admin/tickets/<int:ticket_id>/restore"
)
@foreman_required
def ticket_restore(
    ticket_id: int,
) -> Any:
    ticket = db.get_or_404(
        TicketMirror,
        ticket_id,
    )

    if (
        ticket.state
        != TicketMirrorState.CONVERTED.value
    ):
        ticket.state = TicketMirrorState.NEW.value
        audit(
            "ticket.restore",
            "ticket",
            ticket.erp_id,
        )
        db.session.commit()

    return redirect(ticket_list_return_url())


def preferred_commission_type_id(
    service_position: TimeTaskTemplate | None,
    available_type_ids: set[int],
    default_type_id: int,
) -> int | None:
    if service_position is None:
        return (
            default_type_id
            if default_type_id
            in available_type_ids
            else None
        )

    mapped_type_ids = {
        int(item)
        for item in (
            service_position
            .commission_type_erp_ids
            or []
        )
        if int(item) in available_type_ids
    }

    if len(mapped_type_ids) == 1:
        return next(iter(mapped_type_ids))

    if (
        default_type_id in available_type_ids
        and (
            not mapped_type_ids
            or default_type_id
            in mapped_type_ids
        )
    ):
        return default_type_id

    return None


@admin_bp.route(
    "/admin/commissions/new",
    methods=["GET", "POST"],
)
@commission_creation_required
def commission_create() -> Any:
    service_positions = (
        commission_service_positions()
        .join(
            CraftActivity,
            TimeTaskTemplate.craft_activity_id
            == CraftActivity.id,
        )
        .filter(
            TimeTaskTemplate
            .erp_service_catalogue_id
            .isnot(None),
            CraftActivity.active.is_(True),
            CraftActivity.app_enabled.is_(True),
            CraftActivity.erp_id.isnot(None),
        )
        .order_by(
            TimeTaskTemplate.title.asc()
        )
        .all()
    )

    craft_activity_by_id = {
        item.craft_activity.id: item.craft_activity
        for item in service_positions
        if item.craft_activity is not None
    }
    craft_activities = sorted(
        craft_activity_by_id.values(),
        key=lambda item: (
            item.name.casefold(),
            item.id,
        ),
    )

    allowed_type_ids = (
        configured_commission_type_ids()
    )

    if allowed_type_ids:
        commission_types = (
            WowiCommissionType.query
            .filter(
                WowiCommissionType.active.is_(
                    True
                ),
                WowiCommissionType.erp_id.in_(
                    allowed_type_ids
                ),
            )
            .order_by(
                WowiCommissionType.code.asc(),
                WowiCommissionType.erp_id.asc(),
            )
            .all()
        )
    else:
        commission_types = []

    craftsman_users = (
        User.query
        .filter(
            User.is_active_flag.is_(True),
            User.craftsman_erp_id.isnot(None),
        )
        .order_by(
            User.display_name.asc(),
            User.id.asc(),
        )
        .all()
    )

    external_craftsmen = (
        available_external_craftsmen()
    )

    company_codes = (
        WowiCompanyCode.query
        .filter(
            WowiCompanyCode.active.is_(True)
        )
        .order_by(
            WowiCompanyCode.code.asc(),
            WowiCompanyCode.name.asc(),
        )
        .all()
    )

    available_commission_type_ids = {
        item.erp_id
        for item in commission_types
    }
    default_commission_type_id = int(
        settings.get(
            SettingKeys
            .WOWI_DEFAULT_COMMISSION_TYPE_ID
        )
        or 0
    )

    position_rows = [{}]
    selected_object = None

    selected_service_position = None
    position_selection_source = None

    source_ticket = None
    source_offer = None
    source_commission = None
    form_values = request.form

    source_ticket_id = (
        request.form.get(
            "source_ticket_id",
            type=int,
        )
        if request.method == "POST"
        else request.args.get(
            "ticket_id",
            type=int,
        )
    )

    source_offer_id = (
        request.form.get(
            "source_offer_id",
            type=int,
        )
        if request.method == "POST"
        else request.args.get(
            "offer_id",
            type=int,
        )
    )

    source_commission_id = request.form.get("source_commission_id", type=int) if request.method == "POST" else request.args.get("reissue_id", type=int)

    if sum(value is not None for value in (source_ticket_id, source_offer_id, source_commission_id)) > 1:
        flash(
            "Bitte nur eine Vorlage für den neuen Auftrag auswählen.",
            "danger",
        )
        return redirect(
            url_for("admin.commissions")
        )

    if source_commission_id is not None:
        try:
            source_commission = review_commission(source_commission_id, current_user, lock=request.method == "POST")
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
            return redirect(url_for("admin.commission_reviews"))
        if request.method == "GET":
            draft = commission_reissue_data(source_commission)
            form_values, position_rows = draft["form_values"], draft["position_rows"] or [{}]
            if form_values["object_id"]:
                try:
                    selected_object = get_commission_object_choice(form_values["object_type"], form_values["object_id"])
                except ValueError:
                    flash("Das bisherige Objekt ist nicht mehr verfügbar. Bitte ein Objekt auswählen.", "warning")

    if source_offer_id is not None:
        source_offer_query = (
            Offer.query
            .filter(
                Offer.id == source_offer_id
            )
        )

        if request.method == "POST":
            source_offer_query = (
                source_offer_query
                .with_for_update()
            )

        source_offer = (
            source_offer_query.one_or_none()
        )

        if source_offer is None:
            flash(
                "Das Angebot wurde nicht gefunden.",
                "danger",
            )
            return redirect(
                url_for("admin.offers")
            )

        try:
            offer_draft = (
                offer_commission_draft_data(
                    source_offer
                )
            )

            if request.method == "GET":
                form_values = (
                    offer_draft["form_values"]
                )
                position_rows = (
                    offer_draft["position_rows"]
                )
                selected_object = (
                    get_commission_object_choice(
                        source_offer.object_type,
                        source_offer.object_id,
                    )
                )

                service_positions.sort(
                    key=lambda service_position: (
                        service_position
                        .craft_activity_id
                        != source_offer
                        .craft_activity_id,
                        service_position
                        .title.casefold(),
                    )
                )

        except ValueError as exc:
            db.session.rollback()
            flash(
                str(exc),
                "danger",
            )
            return redirect(
                url_for(
                    "admin.offer_edit",
                    offer_id=source_offer.id,
                )
            )

    if source_ticket_id is not None:
        source_ticket_query = (
            TicketMirror.query
            .filter(
                TicketMirror.id
                == source_ticket_id,
                TicketMirror.deleted_at.is_(None),
            )
        )

        if request.method == "POST":
            source_ticket_query = (
                source_ticket_query
                .with_for_update()
            )

        source_ticket = (
            source_ticket_query.one_or_none()
        )

        if source_ticket is None:
            flash(
                "Das Ticket wurde nicht gefunden.",
                "danger",
            )
            return redirect(
                ticket_list_return_url()
            )

        if not ticket_ai_is_current(source_ticket):
            if request.method == "POST":
                flash("Die KI-Vorgaben haben sich geändert. Bitte den Auftrag erneut prüfen.", "warning")
            return redirect(url_for("admin.ticket_analysis_status", ticket_id=source_ticket.id, next=ticket_list_return_url()))

        if request.method == "GET":
            form_values = {
                "title": (
                    source_ticket.ai_title
                    or source_ticket.subject
                    or ""
                ),
                "damage_location": (
                    source_ticket.ai_damage_location
                    or ""
                ),
                "priority": (
                    "high"
                    if source_ticket.priority_erp_id == 3
                    else "normal"
                ),
            }

            selected_craft_activity = (
                craft_activity_by_id.get(
                    source_ticket
                    .ai_craft_activity_id
                )
            )

            if selected_craft_activity is not None:
                form_values["craft_activity_id"] = (
                    selected_craft_activity.id
                )
                form_values["assignment_type"] = (
                    CommissionAssignmentType
                    .EXTERNAL.value
                    if (
                        selected_craft_activity
                        .default_assignment_type
                        == CommissionAssignmentType
                        .EXTERNAL.value
                    )
                    else CommissionAssignmentType
                    .INTERNAL.value
                )
            position_rows = [
                {
                    "commission_text": (
                        source_ticket.ai_commission_text
                        or source_ticket.content_excerpt
                        or source_ticket.subject
                        or ""
                    ),
                    "units": "1",
                }
            ]

            available_craftsman_user_ids = {
                user.id
                for user in craftsman_users
            }

            if (
                    source_ticket.ai_state
                    == TicketAiState.READY.value
            ):
                selected_rule = (
                    source_ticket.ai_assignment_rule
                )
                selected_type = (
                    selected_rule.target_assignment_type
                    if selected_rule is not None
                    else None
                )

                if (
                        selected_type is None
                        and source_ticket.ai_assigned_user_id
                        is not None
                ):
                    selected_type = "internal"

                if selected_type in {
                    "internal",
                    "external",
                }:
                    form_values["assignment_type"] = (
                        selected_type
                    )

                if (
                        selected_type == "internal"
                        and source_ticket.ai_assigned_user_id
                        in available_craftsman_user_ids
                ):
                    form_values["craftsman_user_id"] = (
                        source_ticket.ai_assigned_user_id
                    )

                if (
                        selected_type == "external"
                        and selected_rule is not None
                        and selected_rule
                        .target_external_craftsman_id
                        is not None
                ):
                    available_external_ids = {
                        craftsman.id
                        for craftsman
                        in available_external_craftsmen(
                            source_ticket
                            .ai_craft_activity_id
                        )
                    }

                    if (
                            selected_rule
                                    .target_external_craftsman_id
                            in available_external_ids
                    ):
                        form_values[
                            "external_craftsman_id"
                        ] = (
                            selected_rule
                            .target_external_craftsman_id
                        )

            license_agreement_id = (
                ticket_license_agreement_local_id(
                    source_ticket
                )
            )

            if license_agreement_id is not None:
                try:
                    selected_object = (
                        get_commission_object_choice(
                            "license_agreement",
                            license_agreement_id,
                        )
                    )
                except ValueError as exc:
                    flash(
                        "Der im Ticket verknüpfte "
                        "Mietvertrag konnte nicht "
                        f"übernommen werden: {exc}",
                        "warning",
                    )


            if (
                    source_ticket.ai_craft_activity_id
                    is not None
            ):
                service_positions.sort(
                    key=lambda item: (
                        item.craft_activity_id
                        != source_ticket
                        .ai_craft_activity_id,
                        item.title.casefold(),
                    )
                )

                available_position_ids = {
                    item.id
                    for item in service_positions
                }

                ai_service_position = (
                    source_ticket.ai_task_template
                )

                if (
                        ai_service_position is not None
                        and ai_service_position.id
                        in available_position_ids
                        and ai_service_position
                        .craft_activity_id
                        == source_ticket
                        .ai_craft_activity_id
                ):
                    selected_service_position = (
                        ai_service_position
                    )
                    position_selection_source = "KI"
                else:
                    fallback = (
                        TicketCraftFallback.query
                        .filter_by(
                            craft_activity_id=(
                                source_ticket
                                .ai_craft_activity_id
                            )
                        )
                        .one_or_none()
                    )

                    if (
                            fallback is not None
                            and fallback.task_template_id
                            in available_position_ids
                    ):
                        selected_service_position = (
                            fallback.task_template
                        )
                        position_selection_source = (
                            "Fallback"
                        )

                if (
                        selected_service_position
                        is not None
                ):
                    position_rows[0][
                        "service_catalog_id"
                    ] = (
                        selected_service_position
                        .erp_service_catalogue_id
                    )
            selected_commission_type_id = (
                preferred_commission_type_id(
                    selected_service_position,
                    available_commission_type_ids,
                    default_commission_type_id,
                )
            )

            if (
                    selected_commission_type_id
                    is not None
            ):
                form_values[
                    "commission_type_id"
                ] = (
                    selected_commission_type_id
                )

    if request.method == "POST":
        stored_file_paths = []
        form_data = read_commission_form()
        position_rows = (
            form_data["positions"]
            or [{}]
        )

        try:
            object_id = int(
                form_data.get("object_id")
                or ""
            )
            selected_object = (
                get_commission_object_choice(
                    str(
                        form_data.get(
                            "object_type"
                        )
                        or ""
                    ),
                    object_id,
                )
            )
        except (
            TypeError,
            ValueError,
            AttributeError,
        ):
            selected_object = None

        try:
            ticket_attachment_selection = None
            ticket_attachment_service = None
            if source_ticket is not None:
                ticket_attachment_service = WowiportService()
                ticket_attachment_selection = prepare_ticket_attachment_selection(source_ticket, request.form, ticket_attachment_service)
            if (
                    source_ticket is not None
                    and source_ticket.ai_assignment_mode
                    == TicketAiAssignmentMode.FORCE.value
            ):
                selected_rule = (
                    source_ticket.ai_assignment_rule
                )
                required_type = (
                    selected_rule.target_assignment_type
                    if selected_rule is not None
                    else None
                )

                if (
                        required_type is None
                        and source_ticket.ai_assigned_user_id
                        is not None
                ):
                    required_type = "internal"

                if required_type is not None:
                    if (
                            source_ticket.ai_state
                            != TicketAiState.READY.value
                            or source_ticket.ai_input_hash
                            != ticket_analysis_input_hash(
                        source_ticket
                    )
                    ):
                        raise ValueError(
                            "Die verbindliche KI-Zuweisung "
                            "ist veraltet. Das Ticket muss "
                            "erneut analysiert werden."
                        )

                    if (
                            form_data.get("assignment_type")
                            != required_type
                    ):
                        raise ValueError(
                            "Die verbindliche Vergabeart "
                            "der KI-Regel darf nicht "
                            "geändert werden."
                        )

                    if (
                            required_type == "internal"
                            and source_ticket
                            .ai_assigned_user_id
                            is not None
                    ):
                        form_data[
                            "craftsman_user_id"
                        ] = (
                            source_ticket
                            .ai_assigned_user_id
                        )
                        form_values = (
                            request.form.to_dict()
                        )
                        form_values[
                            "craftsman_user_id"
                        ] = (
                            source_ticket
                            .ai_assigned_user_id
                        )

                    if (
                            required_type == "external"
                            and selected_rule is not None
                            and selected_rule
                            .target_external_craftsman_id
                            is not None
                    ):
                        form_data[
                            "external_craftsman_id"
                        ] = (
                            selected_rule
                            .target_external_craftsman_id
                        )
                        form_values = (
                            request.form.to_dict()
                        )
                        form_values[
                            "external_craftsman_id"
                        ] = (
                            selected_rule
                            .target_external_craftsman_id
                        )

            if source_offer is not None:
                form_data["contact_person"] = None

            commission = create_pending_commission(
                data=form_data,
                created_by_user=current_user,
                source=CommissionSource.WEB,
                allow_assignment_override=True,
            )

            db.session.flush()

            if source_commission is not None:
                if commission.craftsman_erp_id != source_commission.craftsman_erp_id:
                    commission.creditor_erp_id = None
                copy_commission_files(
                    source_commission, commission,
                    partial(save_web_commission_photo, stored_paths=stored_file_paths),
                    partial(save_web_commission_upload, stored_paths=stored_file_paths, as_attachment=True),
                )
                resolve_commission_review(source_commission, current_user, replacement=commission)

            if source_ticket is not None:
                store_selected_ticket_attachments(
                    commission, ticket_attachment_selection, ticket_attachment_service,
                    partial(save_web_commission_photo, stored_paths=stored_file_paths),
                    stored_paths=stored_file_paths, uploaded_by_user_id=current_user.id,
                )
                source_ticket.state = (
                    TicketMirrorState.CONVERTED.value
                )
                source_ticket.commission_id = (
                    commission.id
                )

                db.session.add(
                    CommissionTicketLink(
                        commission_id=commission.id,
                        ticket_erp_id=(
                            source_ticket.erp_id
                        ),
                    )
                )

            if source_offer is not None:
                accept_offer(
                    source_offer,
                    commission,
                    current_user,
                )

                audit(
                    "offer.accept",
                    "offer",
                    source_offer.id,
                    {
                        "commission_id": (
                            commission.id
                        ),
                        "craftsman_user_id": (
                            commission.user_id
                        ),
                        "craftsman_erp_id": (
                            commission
                            .craftsman_erp_id
                        ),
                        "assignment_type": (
                            commission
                            .assignment_type
                        ),
                    },
                )

            for photo_upload in request.files.getlist("photo"):
                if photo_upload.filename:
                    save_web_commission_upload(
                        commission=commission, upload=photo_upload,
                        caption=request.form.get("photo_caption"), stored_paths=stored_file_paths,
                    )

            audit(
                "commission.create.web",
                "commission",
                commission.id,
                {
                    "public_id": (
                        commission.public_id
                    ),
                    "assigned_user_id": (
                        commission.user_id
                    ),
                    "position_count": len(
                        commission.positions
                    ),
                    "assignment_rule_id": (
                        source_ticket
                        .ai_assignment_rule_id
                        if source_ticket is not None
                        else None
                    ),
                    "source_offer_id": (
                        source_offer.id
                        if source_offer is not None
                        else None
                    ),
                },

            )

            enqueue_commission_push(
                commission=commission,
                event_type=(
                    PushEventType.NEW_COMMISSION
                ),
                user=commission.user,
            )

            db.session.commit()
            stored_file_paths.clear()

            if source_commission is not None:
                flash("Der neue Auftrag wurde erfasst. Die Prüfung des ursprünglichen Auftrags ist abgeschlossen.", "success")
                return redirect(url_for("admin.commission_detail", commission_id=commission.id))

            if source_offer is not None:
                flash(
                    "Die Annahme wurde erfasst. Der Auftrag wartet auf die Freigabe."
                    if commission.approval_state == "pending" else
                    "Die Annahme wurde erfasst. "
                    "Der Auftrag wurde angelegt und "
                    "zur Übertragung an Wowiport "
                    "eingeplant.",
                    "success",
                )

                return redirect(
                    url_for(
                        "admin.offer_edit",
                        offer_id=source_offer.id,
                    )
                )

            flash(
                "Der Auftrag wurde erfasst und wartet auf die Freigabe durch einen anderen Sachbearbeiter."
                if commission.approval_state == "pending" else
                "Der Auftrag wurde erfasst und "
                "zur Übertragung an Wowiport "
                "eingeplant.",
                "success",
            )

            return redirect(
                ticket_list_return_url()
                if source_ticket is not None
                else url_for("admin.commissions")
            )

        except (RuntimeError, ValueError) as exc:
            db.session.rollback()
            flash(
                str(exc),
                "danger",
            )
        except IntegrityError as exc:
            db.session.rollback()
            if source_commission is None or not any(name in str(exc.orig) for name in ("reissued_from_commission_id", "uq_commission_reissued_from")):
                raise
            flash("Der Prüfauftrag wurde zwischenzeitlich bearbeitet. Bitte die Auftragsprüfung neu öffnen.", "warning")
        finally:
            for stored_path in stored_file_paths:
                try:
                    stored_path.unlink(missing_ok=True)
                except OSError:
                    current_app.logger.exception("commission_attachment_cleanup_failed")

    return render_template(
        "admin/commission_form.html",
        service_positions=service_positions,
        default_service_catalog_ids=(
            dict(db.session.query(
                TicketCraftFallback.craft_activity_id,
                TimeTaskTemplate.erp_service_catalogue_id,
            ).join(
                TimeTaskTemplate,
                TicketCraftFallback.task_template_id == TimeTaskTemplate.id,
            ).filter(
                TicketCraftFallback.craft_activity_id == TimeTaskTemplate.craft_activity_id,
                TimeTaskTemplate.id.in_([item.id for item in service_positions]),
            ).all())
            if source_ticket is None else {}
        ),
        craft_activities=craft_activities,
        commission_types=commission_types,
        craftsman_users=craftsman_users,
        company_codes=company_codes,
        position_rows=position_rows,
        form_values=form_values,
        selected_object=selected_object,
        source_ticket=source_ticket,
        ticket_list_url=ticket_list_return_url() if source_ticket is not None else None,
        selected_ticket_attachment_guids=(request.form.getlist("ticket_attachment_guid")
                                    if request.method == "POST" and request.form.get("ticket_attachment_selection_ready") == "true" else None),
        selected_service_position=(
            selected_service_position
        ),
        position_selection_source=(
            position_selection_source
        ),
        default_commission_type_id=(
            default_commission_type_id
        ),
        external_craftsmen=external_craftsmen,
        source_offer=source_offer,
        source_commission=source_commission,
        notify_tenants_default=settings.get(
            SettingKeys.COMMISSION_NOTIFY_TENANTS_DEFAULT
        ),
    )


@admin_bp.get(
    "/admin/commissions/assignment-preview"
)
@commission_planning_required
def commission_assignment_preview() -> Any:
    assignment_type = str(
        request.args.get(
            "assignment_type",
            "internal",
        )
    ).strip().casefold()
    object_type = request.args.get(
        "object_type",
        "",
    ).strip()
    object_id = request.args.get(
        "object_id",
        type=int,
    )
    craft_activity_id = request.args.get(
        "craft_activity_id",
        type=int,
    )

    if assignment_type not in {
        "internal",
        "external",
    }:
        return jsonify(
            {
                "message": (
                    "Die Vergabeart ist ungültig."
                )
            }
        ), 400

    if not object_type or object_id is None:
        return jsonify(
            {
                "message": (
                    "Für die automatische Zuweisung "
                    "muss ein Objekt ausgewählt sein."
                )
            }
        ), 400

    try:
        object_context = resolve_commission_object(
            {
                "object_type": object_type,
                "object_id": object_id,
            }
        )
        company_code = (
            WowiCompanyCode.query
            .filter(
                WowiCompanyCode.erp_id
                == object_context.economic_unit
                .company_code_erp_id,
                WowiCompanyCode.active.is_(
                    True
                ),
            )
            .one_or_none()
        )

        if company_code is None:
            raise ValueError(
                "Das automatisch ermittelte "
                "Buchwerk ist nicht verfügbar."
            )

    except ValueError as exc:
        return jsonify(
            {
                "message": str(exc),
            }
        ), 400

    payload = {
        "assignment_type": assignment_type,
        "company_code_erp_id": (
            company_code.erp_id
        ),
        "company_code_code": company_code.code,
        "company_code_name": company_code.name,
    }

    if craft_activity_id is None:
        return jsonify(payload)

    try:
        service_position = (
            commission_service_positions()
            .filter(
                TimeTaskTemplate.craft_activity_id
                == craft_activity_id
            )
            .order_by(
                TimeTaskTemplate.id.asc()
            )
            .first()
        )

        if service_position is None:
            raise ValueError(
                "Das ausgewählte Gewerk ist "
                "nicht verfügbar."
            )

        craft_activity = (
            service_position.craft_activity
        )

        if craft_activity is None:
            raise ValueError(
                "Der Leistungsposition ist kein "
                "Gewerk zugeordnet."
            )

        if assignment_type == "external":
            candidates = (
                available_external_craftsmen(
                    craft_activity.id
                )
            )

            if not candidates:
                raise ValueError(
                    "Für dieses Gewerk ist kein externer "
                    "Handwerker verfügbar."
                )

            automatic_candidate = (
                automatic_external_craftsman(
                    craft_activity,
                    candidates,
                )
            )

            payload.update(
                {
                    "external_candidate_ids": [
                        candidate.id
                        for candidate in candidates
                    ],
                    "external_craftsman_id": (
                        automatic_candidate.id
                        if automatic_candidate
                           is not None
                        else None
                    ),
                    "external_craftsman_name": (
                        automatic_candidate
                        .display_name
                        if automatic_candidate
                           is not None
                        else None
                    ),
                }
            )
        else:
            automatic_assignment_message = None

            try:
                assignment = (
                    resolve_commission_assignment(
                        [service_position],
                        object_context,
                        require_user=True,
                    )
                )
            except ValueError as exc:
                assignment = None
                automatic_assignment_message = str(exc)

            if assignment is not None and assignment.user is not None:
                payload.update(
                    {
                        "craftsman_user_id": (
                            assignment.user.id
                        ),
                        "craftsman_erp_id": (
                            assignment.user
                            .craftsman_erp_id
                        ),
                        "craftsman_name": (
                            assignment.user
                            .display_name
                        ),
                    }
                )
            else:
                payload["message"] = (
                    (
                            automatic_assignment_message
                            + " Bitte wählen Sie den "
                              "Handwerker manuell aus."
                    )
                    if automatic_assignment_message
                    else (
                        "Es konnte kein interner "
                        "Handwerker automatisch "
                        "ermittelt werden. Bitte "
                        "wählen Sie ihn manuell aus."
                    )
                )

    except ValueError as exc:
        payload["message"] = str(exc)
        return jsonify(payload), 400

    return jsonify(payload)


@admin_bp.get("/admin/commission-objects/commissions")
@foreman_required
def commission_object_commissions() -> Any:
    object_type = request.args.get("object_type", "")
    object_id = request.args.get("object_id", type=int)
    view = request.args.get("view", "all")
    page = request.args.get("page", type=int) if "page" in request.args else 1
    if (
        object_type not in {"license_agreement", "use_unit", "building", "economic_unit"}
        or object_id is None or object_id <= 0
        or view not in {"all", "open"} or page is None or page <= 0
    ):
        return jsonify({"message": "Die Auswahl für die Auftragsübersicht ist ungültig."}), 400
    try:
        choice = get_commission_object_choice(object_type, object_id)
        levels = request.args.get("levels")
        result = commission_object_history(
            choice, summary=request.args.get("summary") == "true", view=view, page=page,
            levels=[name.strip() for name in levels.split(",")] if levels is not None else None,
        )
    except ValueError as exc:
        return jsonify({"message": str(exc)}), 400
    return jsonify(result)


@admin_bp.get(
    "/admin/commission-objects/search"
)
@commission_planning_required
def commission_object_search() -> Any:
    search_text = request.args.get(
        "q",
        "",
    ).strip()
    search_context = request.args.get(
        "context",
        "tenant",
    ).strip()

    allowed_contexts = {
        "tenant",
        "address",
        "agreement",
        "use_unit",
        "economic_unit",
        "all",
    }

    if search_context not in allowed_contexts:
        return jsonify(
            {
                "message": (
                    "Der Suchkontext ist ungültig."
                ),
            }
        ), 400

    items = search_commission_objects(
        search_text,
        search_context=search_context,
        limit=30,
    )

    return jsonify(
        {
            "items": items,
        }
    )


@admin_bp.get("/admin/commission-objects/<int:object_id>/affected-contacts")
@commission_planning_required
def commission_affected_contacts(object_id: int) -> Any:
    try:
        choice = get_commission_object_choice("license_agreement", object_id)
    except ValueError as exc:
        return jsonify({"message": str(exc)}), 400

    agreement = choice["license_agreement"]
    try:
        text = WowiportService().get_affected_agreement_text(agreement["erp_id"], agreement["id_num"])
    except ValueError as exc:
        return jsonify({"message": str(exc)}), 400
    except Exception:
        current_app.logger.exception("commission_affected_contacts_failed object_id=%s", object_id)
        return jsonify({"message": "Die Mieterkontakte konnten nicht aus Wowiport geladen werden. Bitte erneut versuchen."}), 502

    return jsonify({"text": text})


@admin_bp.get("/admin/commission-reporters/search")
@commission_planning_required
def commission_reporter_search() -> Any:
    return jsonify({"items": search_commission_reporters(request.args.get("q", ""))})


@admin_bp.get("/admin/commission-reporters/<int:agreement_id>/<int:contractor_id>")
@commission_planning_required
def commission_reporter_contact(agreement_id: int, contractor_id: int) -> Any:
    try:
        text, selection = resolve_commission_reporter({
            "reporter_agreement_id": agreement_id, "reporter_contractor_id": contractor_id,
        })
    except ValueError as exc:
        return jsonify({"message": str(exc)}), 400
    except Exception:
        current_app.logger.exception("commission_reporter_contact_failed agreement_id=%s contractor_id=%s", agreement_id, contractor_id)
        return jsonify({"message": "Die Kontaktdaten des Melders konnten nicht geladen werden. Bitte erneut versuchen."}), 502
    return jsonify({"item": {**selection, "text": text}})


@admin_bp.get("/admin/offers")
@offer_required
def offers() -> Any:
    expired_items = (
        Offer.query
        .filter(
            Offer.status
            == OfferStatus
            .AWAITING_RESPONSE.value,
            Offer.valid_until.isnot(None),
            Offer.valid_until < date.today(),
        )
        .all()
    )

    for item in expired_items:
        item.status = OfferStatus.EXPIRED.value
        item.status_note = (
            "Die Angebotsgültigkeit ist "
            "abgelaufen."
        )

        audit(
            "offer.expire",
            "offer",
            item.id,
        )

    if expired_items:
        db.session.commit()

    items = (
        Offer.query
        .order_by(
            Offer.created_at.desc(),
            Offer.id.desc(),
        )
        .all()
    )

    return render_template(
        "admin/offers.html",
        items=items,
        status_labels=OFFER_STATUS_LABELS,
        available_codes=WowiCompanyCode.available_for_documents().limit(2).all(),
    )


@admin_bp.route(
    "/admin/offers/new",
    methods=["GET", "POST"],
)
@offer_required
def offer_create() -> Any:
    company_codes = (
        WowiCompanyCode.available_for_documents()
        .order_by(
            WowiCompanyCode.code.asc()
        )
        .all()
    )

    if request.method == "POST":
        try:
            company_code_id = required_form_int(
                request.form.get(
                    "company_code_id"
                ),
                "Buchwerk",
            )
            company_code = db.session.get(
                WowiCompanyCode,
                company_code_id,
            )

            if (
                company_code is None
                or not company_code.active
            ):
                raise ValueError(
                    "Das ausgewählte Buchwerk "
                    "ist nicht verfügbar."
                )

            item = create_offer_draft(
                company_code,
                current_user,
            )

            db.session.flush()

            audit(
                "offer.create",
                "offer",
                item.id,
                {
                    "company_code_id": (
                        company_code.id
                    ),
                },
            )
            db.session.commit()

        except ValueError as exc:
            db.session.rollback()
            flash(
                str(exc),
                "danger",
            )

            return render_template(
                "admin/offer_create.html",
                company_codes=company_codes,
            )

        flash(
            "Angebotsentwurf angelegt.",
            "success",
        )

        return redirect(
            url_for(
                "admin.offer_edit",
                offer_id=item.id,
            )
        )

    return render_template(
        "admin/offer_create.html",
        company_codes=company_codes,
    )


@admin_bp.route(
    "/admin/offers/<int:offer_id>",
    methods=["GET", "POST"],
)
@offer_required
def offer_edit(
    offer_id: int,
) -> Any:
    item = db.get_or_404(
        Offer,
        offer_id,
    )
    if (
        item.status
        == OfferStatus
        .AWAITING_RESPONSE.value
        and item.valid_until is not None
        and item.valid_until < date.today()
    ):
        item.status = OfferStatus.EXPIRED.value
        item.status_note = (
            "Die Angebotsgültigkeit ist "
            "abgelaufen."
        )

        audit(
            "offer.expire",
            "offer",
            item.id,
        )
        db.session.commit()
    editable = (
        item.status
        == OfferStatus.DRAFT.value
    )

    if request.method == "POST":
        if not editable:
            flash(
                "Ein freigegebenes Angebot "
                "kann nicht geändert werden.",
                "danger",
            )

            return redirect(
                url_for(
                    "admin.offer_edit",
                    offer_id=item.id,
                )
            )

        try:
            save_offer_draft_form(item)
            save_offer_positions_form(item)

            audit(
                "offer.update",
                "offer",
                item.id,
            )
            db.session.commit()

        except ValueError as exc:
            db.session.rollback()
            flash(
                str(exc),
                "danger",
            )

            return render_template(
                "admin/offer_draft.html",
                **offer_edit_context(item),
            )

        flash(
            "Angebotsentwurf gespeichert.",
            "success",
        )

        return redirect(
            url_for(
                "admin.offer_edit",
                offer_id=item.id,
            )
        )

    return render_template(
        "admin/offer_draft.html",
        **offer_edit_context(item),
    )


@admin_bp.get(
    "/admin/offers/<int:offer_id>/objects/search"
)
@offer_required
def offer_object_search(
    offer_id: int,
) -> Any:
    item = db.get_or_404(
        Offer,
        offer_id,
    )
    search_text = request.args.get(
        "q",
        "",
    ).strip()
    items = []

    for result in search_commission_objects(
        search_text,
        limit=50,
    ):
        company_code = (
            result.get("company_code")
            or {}
        )

        if (
            company_code.get("erp_id")
            != item.company_code_erp_id
        ):
            continue

        items.append(result)

    return jsonify(
        {
            "items": items,
        }
    )


@admin_bp.get(
    "/admin/offers/"
    "<int:offer_id>/recipient-candidates"
)
@offer_required
def offer_recipient_candidates(
    offer_id: int,
) -> Any:
    item = db.get_or_404(
        Offer,
        offer_id,
    )
    license_agreement_erp_id = (
        required_form_int(
            request.args.get(
                "license_agreement_id"
            ),
            "Vertrag",
        )
    )
    agreement = offer_agreement(
        item,
        license_agreement_erp_id,
    )

    try:
        service = WowiportService()
        items = (
            service
            .get_invoice_recipient_candidates(
                agreement.erp_id
            )
        )

    except Exception:
        current_app.logger.exception(
            "offer_recipient_fetch_failed "
            "offer_id=%s "
            "license_agreement_id=%s",
            item.id,
            agreement.erp_id,
        )

        return jsonify(
            {
                "message": (
                    "Die Anschrift konnte nicht "
                    "aus Wowiport geladen werden."
                ),
            }
        ), 502

    if not items:
        return jsonify(
            {
                "message": (
                    "Für den Vertrag wurden "
                    "keine Empfänger gefunden."
                ),
            }
        ), 404

    return jsonify(
        {
            "items": items,
            "license_agreement": {
                "erp_id": agreement.erp_id,
                "id_num": agreement.id_num,
            },
        }
    )


@admin_bp.post(
    "/admin/offers/<int:offer_id>/release"
)
@offer_required
def offer_release(
    offer_id: int,
) -> Any:
    item = Offer.query.filter_by(id=offer_id).populate_existing().with_for_update().first_or_404()

    try:
        if request.form.get("save_draft") == "1":
            if item.status != OfferStatus.DRAFT.value:
                raise OfferFinalizationError("Nur ein Entwurf kann freigegeben werden.")
            save_offer_draft_form(item)
            save_offer_positions_form(item)
            audit("offer.update", "offer", item.id)

        release_offer(
            item,
            current_user,
        )
        db.session.commit()

    except (
        ValueError,
        OfferFinalizationError,
        OfferDocumentError,
    ) as exc:
        db.session.rollback()

        current_app.logger.exception(
            "offer_release_failed "
            "offer_id=%s",
            item.id,
        )

        flash(
            str(exc),
            "danger",
        )

        return redirect(
            url_for(
                "admin.offer_edit",
                offer_id=item.id,
            )
        )

    except IntegrityError:
        db.session.rollback()

        current_app.logger.exception(
            "offer_number_conflict "
            "offer_id=%s",
            item.id,
        )

        flash(
            "Die Angebotsnummer konnte "
            "nicht eindeutig vergeben werden. "
            "Bitte versuchen Sie es erneut.",
            "danger",
        )

        return redirect(
            url_for(
                "admin.offer_edit",
                offer_id=item.id,
            )
        )

    flash(
        "Das Angebot wurde freigegeben.",
        "success",
    )

    return redirect(
        url_for(
            "admin.offer_edit",
            offer_id=item.id,
        )
    )


@admin_bp.post(
    "/admin/offers/<int:offer_id>/copy"
)
@offer_required
def offer_copy(
    offer_id: int,
) -> Any:
    source_offer = db.get_or_404(
        Offer,
        offer_id,
    )

    try:
        item = copy_offer_to_draft(
            source_offer,
            current_user,
        )
        db.session.flush()

        audit(
            "offer.copy",
            "offer",
            item.id,
            {
                "source_offer_id": (
                    source_offer.id
                ),
            },
        )
        db.session.commit()

    except ValueError as exc:
        db.session.rollback()
        flash(
            str(exc),
            "danger",
        )

        return redirect(
            url_for(
                "admin.offer_edit",
                offer_id=source_offer.id,
            )
        )

    flash(
        "Eine neue Angebotsfassung wurde "
        "als Entwurf angelegt.",
        "success",
    )

    return redirect(
        url_for(
            "admin.offer_edit",
            offer_id=item.id,
        )
    )


@admin_bp.post(
    "/admin/offers/<int:offer_id>/reject"
)
@offer_required
def offer_reject(
    offer_id: int,
) -> Any:
    item = db.get_or_404(
        Offer,
        offer_id,
    )

    try:
        reason = request.form.get(
            "reason",
            "",
        )
        reject_offer(
            item,
            reason,
            current_user,
        )

        audit(
            "offer.reject",
            "offer",
            item.id,
            {
                "reason": item.status_note,
            },
        )
        db.session.commit()

    except ValueError as exc:
        db.session.rollback()
        flash(
            str(exc),
            "danger",
        )

        return redirect(
            url_for(
                "admin.offer_edit",
                offer_id=item.id,
            )
        )

    flash(
        "Das Angebot wurde als abgelehnt "
        "markiert.",
        "success",
    )

    return redirect(
        url_for(
            "admin.offer_edit",
            offer_id=item.id,
        )
    )


@admin_bp.post(
    "/admin/offers/<int:offer_id>/cancel"
)
@offer_required
def offer_cancel(
    offer_id: int,
) -> Any:
    item = db.get_or_404(
        Offer,
        offer_id,
    )

    try:
        reason = request.form.get(
            "reason",
            "",
        )
        cancel_offer(
            item,
            reason,
            current_user,
        )

        audit(
            "offer.cancel",
            "offer",
            item.id,
            {
                "reason": item.status_note,
            },
        )
        db.session.commit()

    except ValueError as exc:
        db.session.rollback()
        flash(
            str(exc),
            "danger",
        )

        return redirect(
            url_for(
                "admin.offer_edit",
                offer_id=item.id,
            )
        )

    flash(
        "Das Angebot wurde storniert.",
        "success",
    )

    return redirect(
        url_for(
            "admin.offer_edit",
            offer_id=item.id,
        )
    )


@admin_bp.get(
    "/admin/offers/<int:offer_id>/accept"
)
@offer_required
def offer_accept(
    offer_id: int,
) -> Any:
    item = db.get_or_404(
        Offer,
        offer_id,
    )

    try:
        offer_commission_draft_data(item)
    except ValueError as exc:
        flash(
            str(exc),
            "danger",
        )
        return redirect(
            url_for(
                "admin.offer_edit",
                offer_id=item.id,
            )
        )

    return redirect(
        url_for(
            "admin.commission_create",
            offer_id=item.id,
        )
    )


@admin_bp.get(
    "/admin/offers/<int:offer_id>/document"
)
@offer_required
def offer_document(
    offer_id: int,
) -> Any:
    item = db.get_or_404(
        Offer,
        offer_id,
    )

    if not item.document_filename:
        abort(404)

    document_path = (
        resolve_offer_document_path(
            item.document_filename
        )
    )

    if not document_path.is_file():
        abort(404)

    download_name = (
        f"Angebot_{item.offer_number}.pdf"
        if item.offer_number
        else f"Angebot_{item.id}.pdf"
    )

    return send_file(
        document_path,
        mimetype="application/pdf",
        download_name=download_name,
        as_attachment=False,
    )


@admin_bp.post(
    "/admin/offers/<int:offer_id>/delete"
)
@offer_required
def offer_delete(
    offer_id: int,
) -> Any:
    item = db.get_or_404(
        Offer,
        offer_id,
    )

    if (
        item.status
        != OfferStatus.DRAFT.value
    ):
        flash(
            "Nur Angebotsentwürfe können "
            "gelöscht werden.",
            "danger",
        )

        return redirect(
            url_for(
                "admin.offer_edit",
                offer_id=item.id,
            )
        )

    audit(
        "offer.delete",
        "offer",
        item.id,
    )
    db.session.delete(item)
    db.session.commit()

    flash(
        "Angebotsentwurf gelöscht.",
        "success",
    )

    return redirect(
        url_for("admin.offers")
    )


def offer_edit_context(
    item: Offer,
) -> dict[str, Any]:
    craft_activities = (
        CraftActivity.query
        .filter(
            CraftActivity.active.is_(True),
            CraftActivity.app_enabled.is_(True),
        )
        .order_by(
            CraftActivity.name.asc()
        )
        .all()
    )
    task_templates = (
        TimeTaskTemplate.query
        .join(
            CraftActivity,
            TimeTaskTemplate.craft_activity_id
            == CraftActivity.id,
        )
        .filter(
            TimeTaskTemplate.active.is_(True),
            TimeTaskTemplate
            .craft_activity_id
            .isnot(None),
            CraftActivity.active.is_(True),
            CraftActivity.app_enabled.is_(True),
        )
        .order_by(
            CraftActivity.name.asc(),
            TimeTaskTemplate.title.asc(),
            TimeTaskTemplate.code.asc(),
        )
        .all()
    )
    catalogue_items = [
        {
            "id": task_template.id,
            "craft_activity_id": (
                task_template.craft_activity_id
            ),
            "code": task_template.code,
            "title": task_template.title,
        }
        for task_template in task_templates
    ]
    selected_object = None

    if (
        item.object_type
        and item.object_id
    ):
        try:
            selected_object = (
                get_commission_object_choice(
                    item.object_type,
                    item.object_id,
                )
            )
        except ValueError:
            selected_object = None

    return {
        "item": item,
        "editable": (
            item.status
            == OfferStatus.DRAFT.value
        ),
        "craft_activities": craft_activities,
        "catalogue_items": catalogue_items,
        "selected_object": selected_object,
        "status_labels": OFFER_STATUS_LABELS,
        "position_source_labels": {
            OfferPositionSource.CATALOG.value: "Katalog",
            OfferPositionSource.MATERIAL.value: "Material",
            OfferPositionSource.LOCAL_CATALOG.value: "Lokaler Katalog",
            OfferPositionSource.FREE.value: "Freie Position",
        },
    }


def save_offer_draft_form(
    item: Offer,
) -> None:
    craft_activity_id = optional_form_int(
        request.form.get(
            "craft_activity_id"
        ),
        "Gewerk",
    )

    if craft_activity_id is not None:
        craft_activity = db.session.get(
            CraftActivity,
            craft_activity_id,
        )

        if (
            craft_activity is None
            or not craft_activity.active
            or not craft_activity.app_enabled
        ):
            raise ValueError(
                "Das ausgewählte Gewerk "
                "ist nicht verfügbar."
            )

        item.craft_activity = craft_activity
    else:
        item.craft_activity = None

    item.offer_date = optional_form_date(
        request.form.get(
            "offer_date"
        ),
        "Angebotsdatum",
    )
    item.valid_until = optional_form_date(
        request.form.get(
            "valid_until"
        ),
        "Gültigkeitsdatum",
    )
    item.title = optional_form_text(
        "title"
    )
    item.introductory_text = (
        optional_form_text(
            "introductory_text"
        )
    )
    item.concluding_text = (
        optional_form_text(
            "concluding_text"
        )
    )

    if (
        item.offer_date
        and item.valid_until
        and item.valid_until
        < item.offer_date
    ):
        raise ValueError(
            "Das Gültigkeitsdatum darf nicht "
            "vor dem Angebotsdatum liegen."
        )

    recipient_type = request.form.get(
        "recipient_type",
        InvoiceRecipientType.PRIVATE.value,
    ).strip()

    if recipient_type not in {
        InvoiceRecipientType.PRIVATE.value,
        InvoiceRecipientType.BUSINESS.value,
    }:
        raise ValueError(
            "Die Empfängerart ist ungültig."
        )

    item.recipient_type = recipient_type
    item.recipient_person_erp_id = (
        optional_form_int(
            request.form.get(
                "recipient_person_erp_id"
            ),
            "Person",
        )
    )
    item.recipient_name = optional_form_text(
        "recipient_name"
    )
    item.recipient_name_addition = (
        optional_form_text(
            "recipient_name_addition"
        )
    )
    item.recipient_street = optional_form_text(
        "recipient_street"
    )
    item.recipient_postal_code = (
        optional_form_text(
            "recipient_postal_code"
        )
    )
    item.recipient_town = optional_form_text(
        "recipient_town"
    )
    item.recipient_country_code = (
        request.form.get(
            "recipient_country_code",
            "DE",
        ).strip().upper()
        or "DE"
    )
    item.recipient_email = optional_form_text(
        "recipient_email"
    )
    item.recipient_vat_id = optional_form_text(
        "recipient_vat_id"
    )

    if (
        len(item.recipient_country_code)
        != 2
    ):
        raise ValueError(
            "Der Ländercode des Empfängers "
            "muss zweistellig sein."
        )

    save_offer_object(item)


def save_offer_object(
    item: Offer,
) -> None:
    object_type = request.form.get(
        "object_type",
        "",
    ).strip()
    object_id = optional_form_int(
        request.form.get(
            "object_id"
        ),
        "Objekt",
    )

    if not object_type and object_id is None:
        item.object_type = None
        item.object_id = None
        item.economic_unit_erp_id = None
        item.building_erp_id = None
        item.use_unit_erp_id = None
        item.license_agreement_erp_id = None
        item.license_agreement_idnum = None
        item.object_address = None
        item.object_location = None
        item.tenant_name = None
        return

    if not object_type or object_id is None:
        raise ValueError(
            "Die Objektauswahl ist unvollständig."
        )

    context = resolve_commission_object(
        {
            "object_type": object_type,
            "object_id": object_id,
            "company_code_code": (
                item.company_code_code
            ),
        }
    )

    item.object_type = object_type
    item.object_id = object_id
    item.economic_unit_erp_id = (
        context.economic_unit.erp_id
    )
    item.building_erp_id = (
        context.building.erp_id
        if context.building
        else None
    )
    item.use_unit_erp_id = (
        context.use_unit.erp_id
        if context.use_unit
        else None
    )
    item.license_agreement_erp_id = (
        context.license_agreement.erp_id
        if context.license_agreement
        else None
    )
    item.license_agreement_idnum = (
        context.license_agreement.id_num
        if context.license_agreement
        else None
    )
    item.object_address = (
        context.use_unit.address
        if (
            context.use_unit
            and context.use_unit.address
        )
        else (
            context.building.address
            if context.building
            else None
        )
    )

    location_parts = []

    if context.use_unit:
        location_parts.extend(
            [
                context.use_unit.floor_name,
                context.use_unit.position_name,
                (
                    context.use_unit
                    .description_of_position
                ),
            ]
        )

    item.object_location = (
        " · ".join(
            value
            for value in location_parts
            if value
        )
        or None
    )
    item.tenant_name = context.tenant_name

    if (
        not item.recipient_name
        and context.tenant_name
    ):
        item.recipient_name = (
            context.tenant_name
        )


def save_offer_positions_form(
    item: Offer,
) -> None:
    descriptions = request.form.getlist(
        "position_description"
    )
    details = request.form.getlist(
        "position_details"
    )
    quantities = request.form.getlist(
        "position_quantity"
    )
    units = request.form.getlist(
        "position_unit"
    )
    unit_prices = request.form.getlist(
        "position_unit_price_net"
    )
    vat_rates = request.form.getlist(
        "position_vat_rate"
    )
    source_types = request.form.getlist(
        "position_source_type"
    )
    task_template_ids = request.form.getlist(
        "position_task_template_id"
    )
    rows = []

    for index, raw_description in enumerate(
        descriptions
    ):
        description = raw_description.strip()

        if not description:
            raise ValueError(
                f"Position {index + 1} "
                "benötigt eine Bezeichnung."
            )

        source_type = (
            list_value(
                source_types,
                index,
            )
            or OfferPositionSource.FREE.value
        )

        if source_type not in {
            OfferPositionSource.CATALOG.value,
            OfferPositionSource.MATERIAL.value,
            OfferPositionSource.LOCAL_CATALOG.value,
            OfferPositionSource.FREE.value,
        }:
            raise ValueError(
                f"Die Quelle der Position "
                f"{index + 1} ist ungültig."
            )

        rows.append(
            {
                "source_type": source_type,
                "task_template_id": (
                    optional_position_id(
                        list_value(
                            task_template_ids,
                            index,
                        )
                    )
                ),
                "description": description,
                "details": (
                    list_value(
                        details,
                        index,
                    )
                    or None
                ),
                "quantity": (
                    required_position_decimal(
                        list_value(
                            quantities,
                            index,
                        ),
                        (
                            "Menge der Position "
                            f"{index + 1}"
                        ),
                    )
                ),
                "unit": (
                    list_value(
                        units,
                        index,
                    )
                    or "Stk"
                ),
                "unit_price_net": (
                    required_position_decimal(
                        list_value(
                            unit_prices,
                            index,
                        ),
                        (
                            "Einzelpreis der "
                            f"Position {index + 1}"
                        ),
                    )
                ),
                "vat_rate": (
                    required_position_decimal(
                        list_value(
                            vat_rates,
                            index,
                        ),
                        (
                            "Umsatzsteuersatz der "
                            f"Position {index + 1}"
                        ),
                    )
                ),
            }
        )

    attach_article_fields(rows, item.positions)
    replace_offer_positions(
        item,
        rows,
    )


def offer_agreement(
    item: Offer,
    license_agreement_erp_id: int,
) -> WowiLicenseAgreement:
    agreement = (
        WowiLicenseAgreement.query
        .filter_by(
            erp_id=license_agreement_erp_id,
            active=True,
        )
        .one_or_none()
    )

    if (
        agreement is None
        or agreement.use_unit is None
        or agreement.use_unit.economic_unit
        is None
        or (
            agreement.use_unit
            .economic_unit
            .company_code_erp_id
            != item.company_code_erp_id
        )
    ):
        raise ValueError(
            "Der ausgewählte Vertrag gehört "
            "nicht zum Buchwerk des Angebots."
        )

    return agreement


@admin_bp.get("/admin/invoices")
@invoice_required
def invoices() -> Any:
    status_labels = {
        "active": "Offene Rechnungen", "draft": "Entwurf", "finalized": "Rechnung erstellt",
        "sent": "Versendet", "canceled": "Storniert",
    }
    creator_filter = request.args.get("creator", "all").strip()
    responsible_filter = request.args.get("responsible", "mine").strip()
    status_filter = request.args.get("status", "active").strip()
    if creator_filter not in {"all", "mine"} and not (creator_filter.isdecimal() and len(creator_filter) <= 18):
        creator_filter = "all"
    if responsible_filter not in {"all", "mine"} and not (responsible_filter.isdecimal() and len(responsible_filter) <= 18):
        responsible_filter = "mine"
    if status_filter not in {"all", *status_labels}:
        status_filter = "active"
    query = Invoice.query.options(
        selectinload(Invoice.created_by), selectinload(Invoice.responsible_user),
        selectinload(Invoice.commission_links),
    )
    if creator_filter != "all":
        query = query.filter(Invoice.created_by_user_id == (current_user.id if creator_filter == "mine" else int(creator_filter)))
    if responsible_filter != "all":
        query = query.filter(Invoice.responsible_user_id == (current_user.id if responsible_filter == "mine" else int(responsible_filter)))
    if status_filter == "active":
        query = query.filter(
            Invoice.status.in_((InvoiceStatus.DRAFT.value, InvoiceStatus.FINALIZED.value)),
            Invoice.completed_at.is_(None),
        )
    elif status_filter in {"finalized", "sent"}:
        query = query.filter(
            Invoice.status == InvoiceStatus.FINALIZED.value,
            Invoice.completed_at.is_not(None) if status_filter == "sent" else Invoice.completed_at.is_(None),
        )
    elif status_filter != "all":
        query = query.filter(Invoice.status == status_filter)
    creator_choices = User.query.join(Invoice, Invoice.created_by_user_id == User.id).distinct().order_by(User.display_name, User.id).all()
    responsible_choices = User.query.join(Invoice, Invoice.responsible_user_id == User.id).distinct().order_by(User.display_name, User.id).all()
    return render_template(
        "admin/invoices.html", items=query.order_by(Invoice.created_at.desc(), Invoice.id.desc()).all(),
        status_labels=status_labels, creator_filter=creator_filter, responsible_filter=responsible_filter,
        status_filter=status_filter, creator_choices=creator_choices, responsible_choices=responsible_choices,
    )


@admin_bp.post("/admin/invoices/<int:invoice_id>/responsibility")
@invoice_required
def invoice_responsibility(invoice_id):
    from .invoice_responsibility import assign_invoice
    item = Invoice.query.filter_by(id=invoice_id).populate_existing().with_for_update().first_or_404()
    try:
        previous_user_id = item.responsible_user_id
        changed = assign_invoice(item, request.form.get("responsible_user_id"), current_user, request.form.get("revision"))
        if changed:
            audit("invoice.responsibility.update", "invoice", item.id, {
                "previous_user_id": previous_user_id, "responsible_user_id": item.responsible_user.id,
            })
        db.session.commit()
        flash("Zuständigkeit gespeichert. Die E-Mail-Benachrichtigung wurde zum Versand vorgemerkt." if changed else "Die Zuständigkeit ist unverändert.", "success" if changed else "info")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return redirect(url_for("admin.invoice_edit", invoice_id=invoice_id))


@admin_bp.route("/admin/invoices/<int:invoice_id>/cover-letter", methods=["GET", "POST", "DELETE"])
@invoice_required
def invoice_cover_letter(invoice_id):
    from .invoice_correspondence import cover_letter_values, invoice_correspondence_pdf
    from .invoice_delivery import pending_invoice_completion
    from .invoice_dms_dispatch import existing_dispatch

    item = Invoice.query.filter_by(id=invoice_id).populate_existing().with_for_update().first_or_404()
    if item.tenant_billing_reference:
        return jsonify(message="Bitte nutzen Sie in der Rechnungsansicht „Anschreiben zur Mieterbelastung“."), 409
    if request.method == "GET":
        response = jsonify(cover_letter_values(item))
        response.headers["Cache-Control"] = "private, no-store, max-age=0"
        return response
    if pending_invoice_completion(item) or (item.status == "finalized" and existing_dispatch(item.id, lock=True)):
        return jsonify(message="Für diese Rechnung läuft bereits ein Versand oder eine DMS-Übergabe."), 409
    try:
        if request.method == "DELETE":
            had_letter = item.cover_letter_title is not None
            item.cover_letter_title = item.cover_letter_text = item.cover_letter_date = None
            if had_letter and item.status != InvoiceStatus.DRAFT.value:
                register_invoice_archive_pdf(item, invoice_correspondence_pdf(item))
            audit("invoice.cover_letter.remove", "invoice", item.id)
            db.session.commit()
            return jsonify(message="Anschreiben entfernt.")
        save_invoice_cover_letter(item)
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return jsonify(message=str(exc)), 400
    except InvoiceDocumentError as exc:
        db.session.rollback()
        return jsonify(message=str(exc)), 502
    except Exception:
        db.session.rollback()
        current_app.logger.exception("invoice_cover_letter_failed invoice_id=%s", invoice_id)
        return jsonify(message="Das Anschreiben konnte nicht erstellt werden. Bitte den Briefbogen und die Rechnungsdaten prüfen."), 502
    return jsonify(message="Anschreiben gespeichert.", url=url_for("admin.invoice_correspondence", invoice_id=item.id))


def save_invoice_cover_letter(invoice: Invoice) -> None:
    from .invoice_correspondence import invoice_correspondence_pdf, letter_date
    from .settings import validate_cover_letter_text

    invoice.cover_letter_title = validate_cover_letter_text(request.form.get("title"), title=True)
    invoice.cover_letter_text = validate_cover_letter_text(request.form.get("text"))
    invoice.cover_letter_date = invoice.cover_letter_date or letter_date()
    content = invoice_correspondence_pdf(invoice)
    if invoice.status != InvoiceStatus.DRAFT.value:
        register_invoice_archive_pdf(invoice, content)
    audit("invoice.cover_letter.update", "invoice", invoice.id)


@admin_bp.route("/admin/invoices/<int:invoice_id>/abschluss", methods=["GET", "POST"])
@invoice_required
def invoice_complete(invoice_id: int):
    from .invoice_correspondence import cover_letter_values
    from .invoice_delivery import pending_invoice_completion
    from .invoice_dms_dispatch import existing_dispatch

    saving = request.method == "POST"
    item = db.get_or_404(Invoice, invoice_id, populate_existing=saving, with_for_update=saving)
    if item.status != InvoiceStatus.FINALIZED.value or item.completed_at:
        return redirect(url_for("admin.invoice_edit", invoice_id=item.id))
    delivery_url = url_for("invoice_delivery.delivery_complete", invoice_id=item.id)
    if pending_invoice_completion(item) or existing_dispatch(item.id, lock=saving):
        return redirect(delivery_url)
    if item.tenant_billing_reference and item.invoice_kind == InvoiceKind.INVOICE.value:
        return redirect(url_for("admin.invoice_tenant_charge", invoice_id=item.id, complete=1))

    values = cover_letter_values(item)
    if saving:
        try:
            if request.form.get("action") == "skip":
                if item.cover_letter_title is not None:
                    raise ValueError("Das gespeicherte Anschreiben ist Teil des Versands.")
            else:
                values.update(title=request.form.get("title", ""), text=request.form.get("text", ""))
                if item.tenant_billing_reference:
                    raise ValueError("Für diese Stornorechnung steht kein Anschreiben zur Verfügung.")
                save_invoice_cover_letter(item)
            db.session.commit()
            return redirect(delivery_url)
        except (ValueError, InvoiceDocumentError) as exc:
            db.session.rollback()
            flash(str(exc), "danger")
        except Exception:
            db.session.rollback()
            current_app.logger.exception("invoice_completion_letter_failed invoice_id=%s", invoice_id)
            flash("Das Anschreiben konnte nicht gespeichert werden. Bitte den Briefbogen und die Rechnungsdaten prüfen.", "danger")

    return render_template("admin/invoice_completion_letter.html", item=item, values=values)


@admin_bp.post("/admin/invoices/<int:invoice_id>/erledigt")
@invoice_required
def invoice_mark_sent(invoice_id: int):
    from .invoice_delivery import pending_invoice_completion, record_invoice_completion
    from .invoice_dms_dispatch import existing_dispatch

    invoice = db.get_or_404(Invoice, invoice_id, populate_existing=True, with_for_update=True)
    if request.form.get("confirm_sent") != "1":
        abort(400)
    if invoice.status != InvoiceStatus.FINALIZED.value:
        abort(409)
    pending_delivery = pending_invoice_completion(invoice)
    dispatch = existing_dispatch(invoice.id, lock=True)
    if dispatch and dispatch.invoice_dispatch["mode"] == "dms" and dispatch.status != "archived":
        flash("Die DMS-Übergabe der Rechnung ist noch ausstehend.", "warning")
    elif pending_delivery is not None and pending_delivery.state != "uncertain":
        flash("Für die Rechnung läuft bereits ein Direktversand. Bitte prüfen Sie den Versandstatus.", "warning")
    else:
        record_invoice_completion(invoice, "manual", utc_now())
        db.session.commit()
        flash("Rechnung als versendet bestätigt.", "success")
    return redirect(url_for("admin.invoice_edit", invoice_id=invoice_id))


@admin_bp.post("/admin/invoices/<int:invoice_id>/cover-letter/preview")
@invoice_required
def invoice_cover_letter_preview(invoice_id):
    from .invoice_correspondence import invoice_correspondence_pdf
    item = db.get_or_404(Invoice, invoice_id)
    if item.tenant_billing_reference:
        return jsonify(message="Bitte nutzen Sie in der Rechnungsansicht „Anschreiben zur Mieterbelastung“."), 409
    try:
        content = invoice_correspondence_pdf(item, title=request.form.get("title", ""), text=request.form.get("text", ""))
    except ValueError as exc:
        return jsonify(message=str(exc)), 400
    except Exception:
        current_app.logger.exception("invoice_cover_letter_preview_failed invoice_id=%s", invoice_id)
        return jsonify(message="Die Vorschau konnte nicht erstellt werden. Bitte den Briefbogen und die Rechnungsdaten prüfen."), 502
    return document_preview_response(content, "Rechnung-mit-Anschreiben-Vorschau.pdf")


@admin_bp.get("/admin/invoices/<int:invoice_id>/correspondence")
@invoice_required
def invoice_correspondence(invoice_id):
    from .invoice_correspondence import invoice_correspondence_pdf
    item = db.get_or_404(Invoice, invoice_id)
    if item.tenant_billing_reference or item.cover_letter_title is None:
        abort(404)
    try:
        content = invoice_correspondence_pdf(item)
    except Exception:
        current_app.logger.exception("invoice_correspondence_failed invoice_id=%s", invoice_id)
        flash("Die PDF mit Anschreiben konnte nicht erstellt werden. Bitte den Briefbogen und die Rechnungsdatei prüfen.", "danger")
        return redirect(url_for("admin.invoice_edit", invoice_id=invoice_id))
    return document_preview_response(content, secure_filename(f"Rechnung-{item.invoice_number or item.id}-mit-Anschreiben.pdf"))


def tenant_charge_invoice(invoice_id: int, *, lock: bool = False) -> Invoice:
    query = Invoice.query.filter_by(id=invoice_id)
    if lock:
        query = query.populate_existing().with_for_update()
    invoice = query.first_or_404()
    if not invoice.tenant_billing_reference or invoice.invoice_kind != InvoiceKind.INVOICE.value:
        abort(404)
    if invoice.status != InvoiceStatus.FINALIZED.value:
        abort(409, description="Erstellen Sie zuerst die Rechnung für die Mieterbelastung.")
    return invoice


def build_tenant_charge_letter(values, recipients, payment, today, revision):
    from .settings import validate_tenant_charge_closing

    recipient = choose_contract_recipient(recipients, values["person_id"])
    due = optional_form_date(values["due_date"], "Zahlungstermin")
    if due is None or due < today:
        raise ValueError("Bitte geben Sie einen Zahlungstermin ab dem Datum des Anschreibens an.")
    if not values["purpose"] or len(values["purpose"]) > 4000 or len(values["caseworker_name"]) > 255:
        raise ValueError("Bitte geben Sie den Anlass mit höchstens 4.000 Zeichen und einen gültigen Ansprechpartner an.")
    return {
        "purpose": values["purpose"], "due_date": due.isoformat(), "letter_date": today.isoformat(),
        "closing": validate_tenant_charge_closing(values["closing"]),
        "caseworker_name": values["caseworker_name"], "include_work_reports": values["include_work_reports"],
        "recipient": {field: recipient.get(field) for field in (
            "person_id", "name", "name_addition", "street", "postal_code", "town", "country_code",
        )},
        "payment": payment, "revision": revision,
    }


def save_tenant_charge_letter(invoice, letter, content):
    register_invoice_archive_pdf(invoice, content)
    invoice.tenant_charge_letter = letter
    audit("invoice.tenant_charge.save", "invoice", invoice.id, {"payment_method": letter["payment"]["method"]})


def invoice_pdf_with_tenant_letter(invoice, *, save_default):
    from .invoice_correspondence import letter_date
    from .tenant_charge_letter import tenant_charge_package, tenant_letter_form_values, tenant_letter_payment, tenant_letter_recipients

    company = db.session.get(WowiCompanyCode, invoice.tenant_billing_reference["company_code_id"])
    if company is None:
        raise ValueError("Das Buchwerk des Mietvertrags ist nicht verfügbar.")
    if invoice.tenant_charge_letter:
        return tenant_charge_package(invoice, company, invoice.tenant_charge_letter)

    today = letter_date()
    values = tenant_letter_form_values(invoice, company, today, current_user.display_name)
    service = WowiportService()
    recipients = tenant_letter_recipients(invoice, service)
    payment = tenant_letter_payment(invoice, company, service)
    letter = build_tenant_charge_letter(values, recipients, payment, today, revision=1)
    content = tenant_charge_package(invoice, company, letter)
    if save_default:
        save_tenant_charge_letter(invoice, letter, content)
        db.session.commit()
    return content


@admin_bp.route("/admin/invoices/<int:invoice_id>/tenant-charge", methods=["GET", "POST"])
@admin_bp.post("/admin/invoices/<int:invoice_id>/tenant-charge/preview", endpoint="invoice_tenant_charge_preview", defaults={"preview": True})
@invoice_required
def invoice_tenant_charge(invoice_id: int, preview: bool = False):
    from .invoice_correspondence import letter_date
    from .tenant_charge_letter import tenant_charge_package, tenant_letter_form_values, tenant_letter_payment, tenant_letter_recipients

    item = tenant_charge_invoice(invoice_id, lock=request.method == "POST" and not preview)
    completing = request.args.get("complete") == "1" and not preview
    if completing and item.completed_at:
        return redirect(url_for("admin.invoice_edit", invoice_id=item.id))
    if completing or (request.method == "POST" and not preview):
        from .invoice_delivery import pending_invoice_completion
        from .invoice_dms_dispatch import existing_dispatch

        if pending_invoice_completion(item) or existing_dispatch(item.id, lock=request.method == "POST"):
            return redirect(url_for("invoice_delivery.delivery_complete", invoice_id=item.id))
    company = db.get_or_404(WowiCompanyCode, item.tenant_billing_reference["company_code_id"])
    today = letter_date()
    saved = item.tenant_charge_letter or {}
    values = {}
    has_work_reports = bool(work_report_counts(item))
    recipients = []
    payment = None
    try:
        values = tenant_letter_form_values(item, company, today, current_user.display_name)
        service = WowiportService()
        recipients = tenant_letter_recipients(item, service)
        payment = tenant_letter_payment(item, company, service)

        if request.method == "POST":
            values.update(
                purpose=request.form.get("purpose", "").strip(),
                closing=request.form.get("closing", values["closing"]),
                due_date=request.form.get("due_date", ""),
                person_id=optional_form_int(request.form.get("person_id"), "Vertragspartner"),
                caseworker_name=request.form.get("caseworker_name", "").strip(),
                include_work_reports="include_work_reports" in request.form and has_work_reports,
            )
            if request.form.get("revision", type=int) != saved.get("revision", 0):
                raise ValueError("Das Anschreiben wurde inzwischen geändert. Bitte laden Sie die Seite neu.")
            if request.form.get("payment_method") != payment["method"]:
                raise ValueError("Die Zahlungsart hat sich geändert. Bitte prüfen Sie die Angaben erneut.")
            letter = build_tenant_charge_letter(values, recipients, payment, today, saved.get("revision", 0) + 1)
            content = tenant_charge_package(item, company, letter)
            if preview:
                return document_preview_response(content, "Mieterbelastung-Vorschau.pdf")
            save_tenant_charge_letter(item, letter, content)
            db.session.commit()
            flash("Anschreiben „Mieterbelastung“ gespeichert.", "success")
            endpoint = "invoice_delivery.delivery_complete" if completing else "admin.invoice_tenant_charge"
            return redirect(url_for(endpoint, invoice_id=item.id))
    except (ValueError, InvoiceDocumentError, WorkReportDocumentError) as exc:
        db.session.rollback()
        if preview:
            return jsonify(error=str(exc)), 400
        flash(str(exc), "danger")
    except Exception:
        db.session.rollback()
        current_app.logger.exception("tenant_charge_letter_failed invoice_id=%s", invoice_id)
        message = "Das Mieteranschreiben konnte nicht vorbereitet werden. Bitte prüfen Sie Wowiport und den Briefbogen."
        if preview:
            return jsonify(error=message), 502
        flash(message, "danger")

    response = current_app.make_response(render_template(
        "admin/invoice_tenant_charge.html", item=item, company=company, today=today,
        values=values, has_work_reports=has_work_reports, recipients=recipients, payment=payment, completing=completing,
    ))
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    return response


@admin_bp.get("/admin/invoices/<int:invoice_id>/tenant-charge/document")
@invoice_required
def invoice_tenant_charge_document(invoice_id: int):
    from .tenant_charge_letter import tenant_charge_package, tenant_letter_payment

    item = tenant_charge_invoice(invoice_id)
    if not item.tenant_charge_letter:
        abort(404)
    company = db.get_or_404(WowiCompanyCode, item.tenant_billing_reference["company_code_id"])
    try:
        payment = tenant_letter_payment(item, company, WowiportService())
        if item.tenant_charge_letter.get("payment") != payment:
            raise ValueError("Die Zahlungsdaten des Mietvertrags haben sich geändert. Bitte prüfen und speichern Sie das Anschreiben erneut.")
        content = tenant_charge_package(item, company, item.tenant_charge_letter)
    except (ValueError, WorkReportDocumentError) as exc:
        db.session.rollback()
        flash(str(exc), "danger")
        return redirect(url_for("admin.invoice_tenant_charge", invoice_id=invoice_id))
    except Exception:
        db.session.rollback()
        current_app.logger.exception("tenant_charge_package_failed invoice_id=%s", invoice_id)
        flash("Die Mieterunterlagen konnten nicht als PDF erstellt werden.", "danger")
        return redirect(url_for("admin.invoice_tenant_charge", invoice_id=invoice_id))

    return document_preview_response(content, secure_filename(f"Mieterbelastung-{item.invoice_number}.pdf"))


@admin_bp.route(
    "/admin/invoices/new",
    methods=["GET", "POST"],
)
@invoice_required
def invoice_create() -> Any:
    company_codes = (
        WowiCompanyCode.available_for_documents()
        .order_by(
            WowiCompanyCode.code.asc()
        )
        .all()
    )
    commissions = (
        available_invoice_commissions()
    )

    if request.method == "POST":
        mode = request.form.get(
            "mode",
            "",
        ).strip()

        try:
            if mode == "free":
                company_code_id = (
                    required_form_int(
                        request.form.get(
                            "company_code_id"
                        ),
                        "Buchwerk",
                    )
                )
                company_code = db.session.get(
                    WowiCompanyCode,
                    company_code_id,
                )

                if (
                    company_code is None
                    or not company_code.active
                ):
                    raise ValueError(
                        "Das ausgewählte Buchwerk "
                        "ist nicht verfügbar."
                    )

                invoice = (
                    create_free_invoice_draft(
                        company_code,
                        current_user,
                    )
                )

            elif mode == "agreement":
                agreement_id = required_form_int(request.form.get("agreement_id"), "Mietvertrag")
                person_id = optional_form_int(request.form.get("person_id"), "Vertragspartner")
                agreement = tenant_billing_agreement(agreement_id)
                invoice_agreement_companies(agreement)
                recipient = choose_contract_recipient(invoice_contract_recipients(agreement_id), person_id)
                invoice = create_agreement_invoice_draft(agreement, recipient, current_user)

            elif mode == "selected":
                commission_ids = (
                    selected_invoice_commission_ids()
                )

                selected_commissions = (
                    Commission.query
                    .filter(
                        Commission.id.in_(
                            commission_ids
                        )
                    )
                    .all()
                )

                if (
                    len(selected_commissions)
                    != len(commission_ids)
                ):
                    raise ValueError(
                        "Mindestens ein Auftrag "
                        "wurde nicht gefunden."
                    )

                invoice = (
                    create_commission_invoice_draft(
                        selected_commissions,
                        current_user,
                    )
                )

            else:
                raise ValueError(
                    "Die Art der Rechnung "
                    "ist ungültig."
                )

            db.session.flush()

            audit(
                "invoice.create",
                "invoice",
                invoice.id,
                {
                    "mode": mode,
                    "commission_ids": [
                        link.commission_id
                        for link
                        in invoice.commission_links
                    ],
                },
            )
            db.session.commit()

        except ValueError as exc:
            db.session.rollback()
            flash(
                str(exc),
                "danger",
            )

            return render_invoice_create(company_codes, commissions)

        flash(
            "Rechnungsentwurf angelegt.",
            "success",
        )

        return redirect(
            url_for(
                "admin.invoice_edit",
                invoice_id=invoice.id,
            )
        )

    return render_invoice_create(company_codes, commissions)


def invoice_contract_recipients(agreement_id: int) -> list[dict]:
    try:
        return contract_recipients(WowiportService(), agreement_id)
    except Exception as exc:
        current_app.logger.exception("invoice_contract_recipients_failed agreement_id=%s", agreement_id)
        raise ValueError("Die Empfängeranschrift konnte nicht aus Wowiport geladen werden.") from exc


def invoice_contract_results(search_text: str, *, existing_issuer_id: int | None = None) -> list[dict]:
    if len(search_text) < 2:
        return []
    company_erp_ids = []
    for company in WowiCompanyCode.query.filter_by(active=True).all():
        try:
            tenant_billing_issuer(company, existing_issuer_id=existing_issuer_id)
        except ValueError:
            continue
        company_erp_ids.append(company.erp_id)
    return search_invoice_contracts(search_text, company_erp_ids)


def render_invoice_create(company_codes, commissions):
    search_text = request.args.get("q", "").strip()[:160]
    selection = None
    try:
        agreement_id = optional_form_int(request.args.get("agreement_id"), "Mietvertrag")
        if agreement_id is not None:
            agreement = tenant_billing_agreement(agreement_id)
            company, issuer = invoice_agreement_companies(agreement)
            recipients = invoice_contract_recipients(agreement_id)
            if not recipients:
                raise ValueError("Für den Mietvertrag wurde keine Empfängeranschrift gefunden.")
            selection = {
                "agreement_id": agreement_id, "agreement_number": agreement.id_num,
                "issuer": issuer, "company": company, "recipients": recipients,
            }
    except ValueError as exc:
        flash(str(exc), "danger")

    return render_template(
        "admin/invoice_create.html", company_codes=company_codes, commissions=commissions,
        search_text=search_text, contract_results=invoice_contract_results(search_text), contract_selection=selection,
    )


@admin_bp.route(
    "/admin/invoices/<int:invoice_id>",
    methods=["GET", "POST"],
)
@invoice_required
def invoice_edit(
    invoice_id: int,
) -> Any:
    saving = request.method == "POST"
    item = db.get_or_404(Invoice, invoice_id, populate_existing=saving, with_for_update=saving)
    editable = (
        item.status
        == InvoiceStatus.DRAFT.value
    )

    if saving:
        if not editable:
            flash(
                "Die Rechnungsdaten können nach dem Erstellen der Rechnung nicht geändert werden.",
                "danger",
            )

            return redirect(
                url_for(
                    "admin.invoice_edit",
                    invoice_id=item.id,
                )
            )

        try:
            previous_company_id = item.company_code_id
            previous_agreement_id = item.billing_contract_erp_id
            previous_agreement_number = item.billing_contract_number
            save_invoice_draft_form(
                item
            )
            save_invoice_positions_form(
                item
            )

            removed_agreement = request.form.get("remove_agreement") == "1"
            if removed_agreement or request.form.get("pending_agreement_erp_id", "").strip():
                action = "invoice.agreement.remove" if removed_agreement else "invoice.agreement.assign"
                audit(action, "invoice", item.id, {
                    "agreement_erp_id": item.billing_contract_erp_id,
                    "previous_agreement_erp_id": previous_agreement_id,
                    "previous_agreement_number": previous_agreement_number,
                    "previous_company_code_id": previous_company_id,
                    "company_code_id": item.company_code_id,
                })
            audit(
                "invoice.update",
                "invoice",
                item.id,
            )
            db.session.commit()

        except ValueError as exc:
            db.session.rollback()
            flash(
                str(exc),
                "danger",
            )

            return render_template(
                "admin/invoice_draft.html",
                **invoice_edit_context(item),
            )

        flash(
            "Rechnungsentwurf gespeichert.",
            "success",
        )

        return redirect(
            url_for(
                "admin.invoice_edit",
                invoice_id=item.id,
            )
        )

    return render_template(
        "admin/invoice_draft.html",
        **invoice_edit_context(item),
    )


def document_preview_response(content: bytes, filename: str):
    response = send_file(BytesIO(content), mimetype="application/pdf", download_name=filename, as_attachment=False)
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def draft_pdf_preview(kind: str, document_id: int):
    item = db.get_or_404(Invoice if kind == "invoice" else Offer, document_id)
    if item.status != "draft":
        return jsonify(error="Die Vorschau ist nur für bearbeitbare Entwürfe verfügbar."), 409
    try:
        if kind == "invoice":
            save_invoice_draft_form(item)
            save_invoice_positions_form(item)
        else:
            save_offer_draft_form(item)
            save_offer_positions_form(item)
        content = render_document_preview(kind, item)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except Exception:
        current_app.logger.exception("document_preview_failed document_type=%s document_id=%s", kind, document_id)
        return jsonify(error="Die PDF-Vorschau konnte nicht erstellt werden."), 502
    finally:
        db.session.rollback()
    return document_preview_response(content, "Rechnung-Vorschau.pdf" if kind == "invoice" else "Angebot-Vorschau.pdf")


@admin_bp.post("/admin/invoices/<int:invoice_id>/preview")
@invoice_required
def invoice_preview(invoice_id: int):
    return draft_pdf_preview("invoice", invoice_id)


@admin_bp.post("/admin/offers/<int:offer_id>/preview")
@offer_required
def offer_preview(offer_id: int):
    return draft_pdf_preview("offer", offer_id)


def commission_preview_context(data=None, commission=None):
    if commission is not None:
        crafts = CraftActivity.query.filter(CraftActivity.erp_id.in_([position.craft_activity_erp_id for position in commission.positions])).order_by(CraftActivity.name).all()
        external = ExternalCraftsman.query.filter_by(craftsman_erp_id=commission.craftsman_erp_id).first() if commission.user is None else None
        return {
            "reference": commission.erp_idnum or f"#{commission.id}", "title": commission.title or commission.display_title,
            "object_label": commission.use_unit_idnum or commission.building_idnum or commission.economic_unit_idnum,
            "address": commission.apartment_address, "tenant_name": commission.tenant_name,
            "craft_name": ", ".join(craft.name for craft in crafts),
            "craftsman_name": commission.user.display_name if commission.user else external.display_name if external else "",
            "contact_person": commission.contact_person, "damage_location": commission.damage_location,
            "reporter": commission.reporter, "other_affected": commission.other_affected,
            "execution_from": format_local_datetime(commission.execution_from), "execution_to": format_local_datetime(commission.execution_to),
            "positions": [{"article_number": position.display_article_number, "description": position.commission_text or position.description or "",
                           "internal_description": position.internal_description or "", "quantity": str(position.units).replace(".", ",")} for position in commission.positions],
        }
    try:
        craft_id = int(data.get("craft_activity_id") or "")
        object_id = int(data.get("object_id") or "")
    except (ValueError, TypeError) as exc:
        raise ValueError("Bitte Gewerk und Objekt auswählen.") from exc
    craft = db.session.get(CraftActivity, craft_id)
    if craft is None or not craft.active or not craft.app_enabled:
        raise ValueError("Das Gewerk ist nicht verfügbar.")
    choice = get_commission_object_choice(str(data.get("object_type") or ""), object_id)
    rows = []
    for index, values in enumerate(data.get("positions") or []):
        try:
            service_id = int(values.get("service_catalog_id") or "")
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Bitte die Leistungsposition {index + 1} auswählen.") from exc
        service = TimeTaskTemplate.query.filter_by(erp_service_catalogue_id=service_id, craft_activity_id=craft.id, active=True).one_or_none()
        if service is None:
            raise ValueError(f"Die Leistungsposition {index + 1} ist nicht verfügbar.")
        number = values.get("article_number")
        number = (service.code or "") if number is None else str(number).strip()
        if len(number) > 120:
            raise ValueError(f"Die Artikelnummer der Position {index + 1} darf höchstens 120 Zeichen enthalten.")
        text = str(values.get("commission_text") or "").strip()
        if not text:
            raise ValueError(f"Der Auftragstext der Position {index + 1} fehlt.")
        units = required_position_decimal(str(values.get("units") or "1"), "Menge")
        if not units.is_finite() or units <= 0:
            raise ValueError("Die Menge muss größer als null sein.")
        rows.append({"article_number": number, "description": text, "internal_description": values.get("internal_description") or "",
                     "quantity": format(units, "f").replace(".", ",")})
    if not rows:
        raise ValueError("Bitte mindestens eine Auftragsposition erfassen.")
    craftsman_name = ""
    selected_id = data.get("external_craftsman_id") if data.get("assignment_type") == "external" else data.get("craftsman_user_id")
    if selected_id:
        try:
            craftsman = db.session.get(ExternalCraftsman if data.get("assignment_type") == "external" else User, int(selected_id))
            craftsman_name = craftsman.display_name if craftsman else ""
        except (TypeError, ValueError):
            pass
    reporter = None
    if choice["object_type"] in {"building", "economic_unit"} and any(data.get(key) not in (None, "") for key in ("reporter", "reporter_agreement_id", "reporter_contractor_id")):
        reporter, selection = resolve_commission_reporter(data)
    return {"reference": "", "title": data.get("title") or "Auftrag", "object_label": choice.get("label") or "",
            "address": (choice.get("use_unit") or choice.get("building") or choice.get("economic_unit") or {}).get("address") or "",
            "tenant_name": choice.get("tenant_name") or "", "craft_name": craft.name, "craftsman_name": craftsman_name,
            "contact_person": data.get("contact_person") or "", "damage_location": data.get("damage_location") or "",
            "reporter": reporter,
            "other_affected": data.get("other_affected") if choice["object_type"] in {"use_unit", "license_agreement"} else None,
            "execution_from": format_local_datetime(parse_local_datetime(data.get("execution_from"))),
            "execution_to": format_local_datetime(parse_local_datetime(data.get("execution_to"))), "positions": rows}


@admin_bp.post("/admin/commissions/preview")
@commission_creation_required
def commission_create_preview():
    try:
        source_offer_id = optional_form_int(request.form.get("source_offer_id"), "Angebot")
        if source_offer_id is not None:
            source_offer = db.session.get(Offer, source_offer_id)
            if source_offer is None:
                raise ValueError("Das Angebot wurde nicht gefunden.")
            offer_commission_draft_data(source_offer)
        content = render_document_preview("commission", commission_preview_context(read_commission_form()))
        return document_preview_response(content, "Auftrag-Vorschau.pdf")
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except Exception:
        current_app.logger.exception("commission_preview_failed")
        return jsonify(error="Die PDF-Vorschau konnte nicht erstellt werden."), 502


@admin_bp.get("/admin/commissions/<int:commission_id>/preview")
@foreman_required
def commission_preview(commission_id: int):
    item = db.get_or_404(Commission, commission_id)
    try:
        content = render_document_preview("commission", commission_preview_context(commission=item))
        return document_preview_response(content, "Auftrag-Vorschau.pdf")
    except Exception:
        current_app.logger.exception("commission_preview_failed commission_id=%s", commission_id)
        return jsonify(error="Die PDF-Vorschau konnte nicht erstellt werden."), 502


@admin_bp.post(
    "/admin/invoices/<int:invoice_id>/suggestions"
)
@invoice_required
def invoice_add_suggestions(
    invoice_id: int,
) -> Any:
    item = db.get_or_404(Invoice, invoice_id, populate_existing=True, with_for_update=True)

    try:
        added_count = add_invoice_suggestions(
            item
        )

        audit(
            "invoice.suggestions.add",
            "invoice",
            item.id,
            {
                "added_count": added_count,
            },
        )
        db.session.commit()

    except ValueError as exc:
        db.session.rollback()
        flash(
            str(exc),
            "danger",
        )

        return redirect(
            url_for(
                "admin.invoice_edit",
                invoice_id=item.id,
            )
        )

    if added_count:
        flash(
            f"{added_count} Positionen "
            "wurden ergänzt.",
            "success",
        )
    else:
        flash(
            "Es wurden keine neuen "
            "Positionen gefunden.",
            "info",
        )

    return redirect(
        url_for(
            "admin.invoice_edit",
            invoice_id=item.id,
        )
    )


@admin_bp.get(
    "/admin/invoices/<int:invoice_id>/recipients/search"
)
@invoice_required
def invoice_recipient_search(
    invoice_id: int,
) -> Any:
    invoice = db.get_or_404(
        Invoice,
        invoice_id,
    )
    if not invoice.can_edit_agreement:
        return jsonify(message="Mietverträge können nur für einen Entwurf ausgewählt werden."), 409
    search_text = request.args.get("q", "").strip()[:160]
    return jsonify(items=invoice_contract_results(search_text, existing_issuer_id=invoice.company_code_id))


@admin_bp.get(
    "/admin/invoices/<int:invoice_id>/recipient-candidates"
)
@invoice_required
def invoice_recipient_candidates(
    invoice_id: int,
) -> Any:
    invoice = db.get_or_404(
        Invoice,
        invoice_id,
    )
    if not invoice.can_edit_agreement:
        return jsonify(message="Mietverträge können nur für einen Entwurf ausgewählt werden."), 409
    try:
        license_agreement_id = required_form_int(request.args.get("license_agreement_id"), "Vertrag")
        agreement = tenant_billing_agreement(license_agreement_id)
        company, issuer = invoice_agreement_companies(agreement, invoice)
    except ValueError as exc:
        return jsonify(message=str(exc)), 400

    try:
        items = invoice_contract_recipients(agreement.erp_id)
    except ValueError as exc:
        return jsonify(message=str(exc)), 502

    if not items:
        return jsonify(
            {
                "message": (
                    "Für den Vertrag wurden keine "
                    "Rechnungsempfänger gefunden."
                ),
            }
        ), 404

    return jsonify(
        {
            "items": items,
            "assignment": {
                "issuer_name": issuer.seller_name or issuer.name,
                "recipient_name": (company.seller_name or company.name) if company.tenant_billing_issuer_id is not None else None,
            },
            "license_agreement": {
                "erp_id": agreement.erp_id,
                "id_num": agreement.id_num,
            },
        }
    )


@admin_bp.get("/admin/invoices/<int:invoice_id>/payment-information")
@invoice_required
def invoice_payment_information_fetch(invoice_id: int):
    invoice = db.get_or_404(Invoice, invoice_id)
    if invoice.status != InvoiceStatus.DRAFT.value:
        return jsonify(message="Zahlungsinformationen können nur für einen Entwurf abgerufen werden."), 409
    if invoice.tenant_billing_reference:
        return jsonify(message="Die Zahlungsinformationen zum Mietvertrag werden im Mieteranschreiben verwendet."), 409
    try:
        agreement_id = required_form_int(request.args.get("license_agreement_id"), "Vertrag")
        agreement = invoice_agreement(invoice, agreement_id)
    except ValueError as exc:
        return jsonify(message=str(exc)), 400
    try:
        values = payment_options_from_wowiport(WowiportService(), agreement.erp_id, invoice.company_code.seller_creditor_id or "")
    except Exception:
        current_app.logger.exception("invoice_payment_information_fetch_failed invoice_id=%s license_agreement_id=%s", invoice.id, agreement.erp_id)
        return jsonify(message="Die Zahlungsinformationen konnten nicht aus Wowiport geladen werden. Bitte erneut versuchen oder über „ändern“ eingeben."), 502
    response = jsonify(values)
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    return response


@admin_bp.post(
    "/admin/invoices/<int:invoice_id>/delete"
)
@invoice_required
def invoice_delete(
    invoice_id: int,
) -> Any:
    item = Invoice.query.filter_by(id=invoice_id).populate_existing().with_for_update().first_or_404()

    if (
        item.status
        != InvoiceStatus.DRAFT.value
    ):
        flash(
            "Nur Entwürfe können "
            "gelöscht werden.",
            "danger",
        )

        return redirect(
            url_for(
                "admin.invoice_edit",
                invoice_id=item.id,
            )
        )

    audit(
        "invoice.delete",
        "invoice",
        item.id,
        {
            "commission_ids": [
                link.commission_id
                for link
                in item.commission_links
            ],
        },
    )
    from .invoice_attachments import invoice_attachment_path, remove_invoice_attachment_files
    attachment_paths = [invoice_attachment_path(attachment) for attachment in item.pdf_attachments]
    db.session.delete(item)
    db.session.commit()
    remove_invoice_attachment_files(attachment_paths)

    flash(
        "Rechnungsentwurf gelöscht.",
        "success",
    )

    return redirect(
        url_for("admin.invoices")
    )


def invoice_edit_context(
    item: Invoice,
) -> dict[str, Any]:
    task_templates = (
        TimeTaskTemplate.query
        .join(CraftActivity, TimeTaskTemplate.craft_activity_id == CraftActivity.id)
        .filter(
            TimeTaskTemplate.active.is_(True),
            CraftActivity.active.is_(True),
            CraftActivity.app_enabled.is_(True),
        )
        .order_by(
            TimeTaskTemplate.title.asc(),
            TimeTaskTemplate.code.asc(),
        )
        .all()
    )

    catalogue_items = [
        {
            "id": task_template.id,
            "craft_activity_id": task_template.craft_activity_id,
            "code": task_template.code,
            "title": task_template.title,
        }
        for task_template in task_templates
    ]

    report_counts = work_report_counts(
        item
    )
    has_billed_work_reports = any(
        values[
            WORK_REPORT_SCOPE_BILLED
        ] > 0
        for values in report_counts.values()
    )
    has_any_work_reports = bool(report_counts)

    pending_delivery = None
    if item.status == InvoiceStatus.FINALIZED.value and item.completed_at is None:
        from .invoice_delivery import pending_invoice_completion

        pending_delivery = pending_invoice_completion(item)

    archive_url = None
    if item.dms_document_id:
        from .dms_service import invoice_dms_url

        archive_url = invoice_dms_url(item)

    dms_dispatch = None
    if item.status == InvoiceStatus.FINALIZED.value:
        from .invoice_dms_dispatch import existing_dispatch

        dms_dispatch = existing_dispatch(item.id)
    completion_mode_labels = {
        "direct": "Direktversand", "outlook": "Versand per Outlook", "print": "Kein Versand (nur Druck)",
        "manual": "Manuell als versendet bestätigt", "dms": "Übergabe über DMS",
    }
    return {
        "item": item,
        "completion_mode_label": completion_mode_labels.get(item.completion_mode),
        "completion_date_label": format_local_datetime(item.completed_at) if item.completed_at else None,
        "archive_date_label": format_local_datetime(item.dms_archived_at) if item.dms_archived_at else None,
        "invoice_dms_url": archive_url,
        "pending_delivery": pending_delivery,
        "dms_dispatch": dms_dispatch,
        "responsibility_users": User.query.filter(
            User.is_active_flag.is_(True), db.or_(User.is_admin.is_(True), User.can_manage_invoices.is_(True)),
        ).order_by(User.display_name, User.id).all(),
        "editable": (
            item.status
            == InvoiceStatus.DRAFT.value
        ),
        "task_templates": task_templates,
        "craft_activities": CraftActivity.query.filter_by(active=True, app_enabled=True).order_by(CraftActivity.name).all(),
        "catalogue_items": catalogue_items,
        "position_source_labels": {
            "time": "Zeit",
            "material": "Material",
            "catalog": "Katalog",
            "local_catalog": "Lokaler Katalog",
            "free": "Freie Position",
            "cancellation": "Storno",
        },
        "work_report_counts": report_counts,
        "has_billed_work_reports": (
            has_billed_work_reports
        ),
        "has_any_work_reports": (
            has_any_work_reports
        ),
    }


def save_invoice_positions_form(
    item: Invoice,
) -> None:
    descriptions = request.form.getlist(
        "position_description"
    )
    details = request.form.getlist(
        "position_details"
    )
    quantities = request.form.getlist(
        "position_quantity"
    )
    units = request.form.getlist(
        "position_unit"
    )
    unit_prices = request.form.getlist(
        "position_unit_price_net"
    )
    vat_rates = request.form.getlist(
        "position_vat_rate"
    )
    source_types = request.form.getlist(
        "position_source_type"
    )
    task_template_ids = request.form.getlist(
        "position_task_template_id"
    )
    inventory_item_ids = request.form.getlist(
        "position_inventory_item_id"
    )
    source_data_values = request.form.getlist(
        "position_source_data"
    )

    allowed_source_types = {
        InvoicePositionSource.TIME.value,
        InvoicePositionSource.MATERIAL.value,
        InvoicePositionSource.CATALOG.value,
        InvoicePositionSource.LOCAL_CATALOG.value,
        InvoicePositionSource.FREE.value,
    }
    rows = []

    for index, description in enumerate(
        descriptions
    ):
        description = description.strip()

        if not description:
            raise ValueError(
                f"Position {index + 1} "
                "benötigt eine Bezeichnung."
            )

        quantity = required_position_decimal(
            list_value(
                quantities,
                index,
            ),
            (
                f"Menge der Position "
                f"{index + 1}"
            ),
        )
        unit_price_net = (
            required_position_decimal(
                list_value(
                    unit_prices,
                    index,
                ),
                (
                    "Einzelpreis der Position "
                    f"{index + 1}"
                ),
            )
        )
        vat_rate = required_position_decimal(
            list_value(
                vat_rates,
                index,
            ),
            (
                "Umsatzsteuersatz der "
                f"Position {index + 1}"
            ),
        )

        if quantity <= 0:
            raise ValueError(
                f"Die Menge der Position "
                f"{index + 1} muss größer "
                "als null sein."
            )

        if (
            vat_rate < 0
            or vat_rate > 100
        ):
            raise ValueError(
                "Der Umsatzsteuersatz der "
                f"Position {index + 1} muss "
                "zwischen 0 und 100 liegen."
            )

        source_type = (
            list_value(
                source_types,
                index,
            )
            or InvoicePositionSource
            .FREE.value
        )

        if source_type not in allowed_source_types:
            source_type = (
                InvoicePositionSource
                .FREE.value
            )

        source_data = {}
        raw_source_data = list_value(
            source_data_values,
            index,
        )

        if raw_source_data:
            try:
                parsed_source_data = (
                    json.loads(
                        raw_source_data
                    )
                )

                if isinstance(
                    parsed_source_data,
                    dict,
                ):
                    source_data = (
                        parsed_source_data
                    )
            except json.JSONDecodeError:
                source_data = {}

        rows.append(
            {
                "source_type": source_type,
                "task_template_id": (
                    optional_position_id(
                        list_value(
                            task_template_ids,
                            index,
                        )
                    )
                ),
                "inventory_item_id": (
                    optional_position_id(
                        list_value(
                            inventory_item_ids,
                            index,
                        )
                    )
                ),
                "source_data": source_data,
                "description": description,
                "details": (
                    list_value(
                        details,
                        index,
                    )
                    or None
                ),
                "quantity": quantity,
                "unit": (
                    list_value(
                        units,
                        index,
                    )
                    or "Stk"
                ),
                "unit_price_net": (
                    unit_price_net
                ),
                "vat_rate": vat_rate,
            }
        )

    attach_article_fields(rows, item.positions)
    replace_invoice_positions(
        item,
        rows,
    )


def required_position_decimal(
    value: str,
    label: str,
) -> Decimal:
    result = optional_form_decimal(
        value,
        label,
    )

    if result is None:
        raise ValueError(
            f"{label} muss angegeben werden."
        )

    return result


def optional_position_id(
    value: str,
) -> int | None:
    if not value:
        return None

    try:
        return int(value)
    except ValueError:
        return None


def save_invoice_recipient_form(item: Invoice) -> None:
    license_agreement_id = (
        optional_form_int(
            request.form.get(
                "recipient_license_agreement_erp_id"
            ),
            "Vertrag",
        )
    )

    if license_agreement_id is None:
        item.recipient_license_agreement_erp_id = None
        item.recipient_license_agreement_idnum = None
    elif license_agreement_id != item.recipient_license_agreement_erp_id:
        agreement = invoice_agreement(
            item,
            license_agreement_id,
        )
        item.recipient_license_agreement_erp_id = (
            agreement.erp_id
        )
        item.recipient_license_agreement_idnum = (
            agreement.id_num
        )

    save_invoice_payment_form(item)
    item.recipient_person_erp_id = (
        optional_form_int(
            request.form.get(
                "recipient_person_erp_id"
            ),
            "Person",
        )
    )
    save_invoice_recipient_address_form(item)


def save_invoice_recipient_address_form(item: Invoice) -> None:
    recipient_type = request.form.get(
        "recipient_type",
        InvoiceRecipientType.PRIVATE.value,
    ).strip()

    if recipient_type not in {
        InvoiceRecipientType.PRIVATE.value,
        InvoiceRecipientType.BUSINESS.value,
    }:
        raise ValueError(
            "Die Empfängerart ist ungültig."
        )

    item.recipient_type = recipient_type
    item.recipient_name = optional_form_text(
        "recipient_name"
    )
    item.recipient_name_addition = (
        optional_form_text(
            "recipient_name_addition"
        )
    )
    item.recipient_street = optional_form_text(
        "recipient_street"
    )
    item.recipient_postal_code = (
        optional_form_text(
            "recipient_postal_code"
        )
    )
    item.recipient_town = optional_form_text(
        "recipient_town"
    )
    item.recipient_country_code = (
        request.form.get(
            "recipient_country_code",
            "DE",
        ).strip().upper()
        or "DE"
    )
    item.recipient_email = optional_form_text(
        "recipient_email"
    )
    item.recipient_vat_id = optional_form_text(
        "recipient_vat_id"
    )


def save_invoice_draft_form(item: Invoice) -> None:
    saved_contract_id = request.form.get("saved_contract_erp_id")
    if saved_contract_id is not None and saved_contract_id != str(item.billing_contract_erp_id or ""):
        raise ValueError("Der Mietvertrag des Entwurfs wurde geändert. Bitte laden Sie die Rechnung neu.")

    agreement_id = optional_form_int(request.form.get("pending_agreement_erp_id"), "Mietvertrag")
    remove_agreement = request.form.get("remove_agreement") == "1"
    if agreement_id is not None and remove_agreement:
        raise ValueError("Bitte wählen Sie aus, ob der Mietvertrag geändert oder entfernt werden soll.")
    if agreement_id is not None:
        if not item.can_edit_agreement:
            raise ValueError("Der Mietvertrag kann nur in einem Rechnungsentwurf geändert werden.")
        agreement = tenant_billing_agreement(agreement_id)
        person_id = optional_form_int(request.form.get("pending_agreement_person_id"), "Vertragspartner")
        recipient = choose_contract_recipient(invoice_contract_recipients(agreement_id), person_id)
        assign_invoice_agreement(item, agreement, recipient)
    elif remove_agreement:
        remove_invoice_agreement(item)
        if "recipient_type" in request.form:
            save_invoice_recipient_address_form(item)
    elif not item.tenant_billing_reference:
        save_invoice_recipient_form(item)
    elif not item.tenant_billing_to_company and "recipient_type" in request.form:
        save_invoice_recipient_address_form(item)

    if "insurance_claim_number" in request.form:
        claim_number = optional_form_text("insurance_claim_number")
        if claim_number and (len(claim_number) > 120 or any(ord(character) < 32 for character in claim_number)):
            raise ValueError("Die Versicherungsfall-Nr. muss einzeilig sein und darf höchstens 120 Zeichen enthalten.")
        item.insurance_claim_number = claim_number

    item.invoice_date = optional_form_date(
        request.form.get(
            "invoice_date"
        ),
        "Rechnungsdatum",
    )
    item.service_date_from = (
        optional_form_date(
            request.form.get(
                "service_date_from"
            ),
            "Leistungsbeginn",
        )
    )
    item.service_date_to = (
        optional_form_date(
            request.form.get(
                "service_date_to"
            ),
            "Leistungsende",
        )
    )
    item.due_date = None if item.tenant_billing_reference else optional_form_date(
        request.form.get("due_date"), "Fälligkeitsdatum",
    )
    item.introductory_text = (
        optional_form_text(
            "introductory_text"
        )
    )
    item.concluding_text = optional_form_text(
        "concluding_text"
    )

    if (
        len(item.recipient_country_code)
        != 2
    ):
        raise ValueError(
            "Der Ländercode des Empfängers "
            "muss zweistellig sein."
        )

    if (
        item.service_date_from
        and item.service_date_to
        and item.service_date_from
        > item.service_date_to
    ):
        raise ValueError(
            "Das Ende des Leistungszeitraums "
            "darf nicht vor dem Beginn liegen."
        )

    if (
        item.invoice_date
        and item.due_date
        and item.due_date
        < item.invoice_date
    ):
        raise ValueError(
            "Das Fälligkeitsdatum darf nicht "
            "vor dem Rechnungsdatum liegen."
        )


def invoice_agreement(
    invoice: Invoice,
    license_agreement_erp_id: int,
) -> WowiLicenseAgreement:
    agreement = (
        WowiLicenseAgreement.query
        .filter_by(
            erp_id=license_agreement_erp_id,
            active=True,
        )
        .one_or_none()
    )

    if (
        agreement is None
        or agreement.use_unit is None
        or agreement.use_unit.economic_unit
        is None
        or (
            agreement.use_unit
            .economic_unit
            .company_code_erp_id
            != invoice.company_code_erp_id
        )
    ):
        raise ValueError(
            "Der ausgewählte Vertrag gehört "
            "nicht zum Buchwerk der Rechnung."
        )

    if invoice.tenant_billing_reference or invoice.company_code.uses_tenant_charge_letter:
        raise ValueError("Bitte verwenden Sie für diesen Mietvertrag den Einstieg „Rechnung mit Mietvertrag“.")

    return agreement


def optional_form_date(
    value: str | None,
    label: str,
) -> date | None:
    if value is None or not value.strip():
        return None

    try:
        return date.fromisoformat(
            value.strip()
        )
    except ValueError as exc:
        raise ValueError(
            f"{label} ist ungültig."
        ) from exc


def selected_invoice_commission_ids(
) -> list[int]:
    commission_ids = []

    for value in request.form.getlist(
        "commission_ids"
    ):
        try:
            commission_id = int(value)
        except ValueError:
            continue

        if commission_id not in commission_ids:
            commission_ids.append(
                commission_id
            )

    if not commission_ids:
        raise ValueError(
            "Mindestens ein Auftrag "
            "muss ausgewählt werden."
        )

    return commission_ids


def commission_official_labels(
    items: list[Commission],
) -> dict[int, tuple[str, str]]:
    official_ids = {
        item.responsible_official_repair_erp_id
        for item in items
        if item.local_official_user_id is None
        and item.responsible_official_repair_erp_id is not None
    }
    official_names: dict[int, str] = {}

    if official_ids:
        rows = db.session.query(
            WowiEconomicUnitJurisdiction.responsible_official_erp_id,
            WowiEconomicUnitJurisdiction.responsible_name,
        ).filter(
            WowiEconomicUnitJurisdiction.responsible_official_erp_id.in_(
                official_ids
            ),
        ).order_by(
            WowiEconomicUnitJurisdiction.synced_at.desc(),
            WowiEconomicUnitJurisdiction.id.asc(),
        ).all()

        for official_id, name in rows:
            name = str(name or "").strip()
            if name:
                official_names.setdefault(official_id, name)

    result = {}
    for item in items:
        if (
            is_blacklisted("user", item.local_official_user_id)
            or is_blacklisted("official", item.responsible_official_repair_erp_id)
        ):
            result[item.id] = ("unassigned", "Nicht verfügbar")
            continue
        if item.local_official_user_id is not None:
            name = (
                item.local_official.display_name
                if item.local_official
                else f"Benutzer-ID {item.local_official_user_id}"
            )
            result[item.id] = (
                f"user:{item.local_official_user_id}",
                f"{name} (lokal)",
            )
        elif item.responsible_official_repair_erp_id is not None:
            official_id = item.responsible_official_repair_erp_id
            name = official_names.get(
                official_id,
                f"Sachbearbeiter-ID {official_id}",
            )
            result[item.id] = (
                f"erp:{official_id}",
                f"{name} (Wowiport)",
            )
        else:
            result[item.id] = ("unassigned", "Nicht zugewiesen")

    return result


def commission_craftsman_labels(items, external_craftsmen):
    result = {}
    for item in items:
        if item.user is not None:
            result[item.id] = (f"user:{item.user.id}", f"{item.user.display_name} (Intern)")
        elif item.craftsman_erp_id is not None:
            external = external_craftsmen.get(item.craftsman_erp_id)
            name = external.display_name if external else f"Handwerker-ID {item.craftsman_erp_id}"
            if item.assignment_type == CommissionAssignmentType.EXTERNAL.value:
                name += " (Extern)"
            result[item.id] = (f"erp:{item.craftsman_erp_id}", name)
        else:
            result[item.id] = ("unassigned", "Nicht zugewiesen")
    return result


@admin_bp.get("/admin/commissions")
@foreman_required
def commissions() -> Any:
    local_status_labels = {
        "recorded": "Erfasst",
        "waiting_for_approval": "Wartet auf Freigabe",
        "waiting_for_acceptance": "Warte auf Annahme",
        "open": "Beauftragt",
        "in_progress": "In Bearbeitung",
        "completion_review": "Nicht abgeschlossen",
        "rejected": "Abgelehnt",
        "tenant_charge_review": "Mieterbelastung",
        "done": "Erledigt",
        "awaiting_invoice": "Rechnungseingang erwartet",
        "canceled": "Storniert",
        "deleted": "Gelöscht",
    }
    creator_filter = str(request.args.get("creator") or "all").strip()
    if creator_filter not in {"mine", "all"} and not (creator_filter.isdecimal() and len(creator_filter) <= 18):
        creator_filter = "all"
    official_filter = str(
        request.args.get("official") or "all"
    ).strip()
    local_status_filter = str(
        request.args.get("local_status") or "all"
    ).strip()
    priority_filter = str(
        request.args.get("priority") or "all"
    ).strip()
    craftsman_filter = str(request.args.get("craftsman") or "all").strip()
    commission_type_filter = str(request.args.get("commission_type") or "all").strip()
    if commission_type_filter not in {"all", "unassigned"}:
        if commission_type_filter.isdecimal() and len(commission_type_filter) <= 18 and int(commission_type_filter) > 0:
            commission_type_filter = str(int(commission_type_filter))
        else:
            commission_type_filter = "all"

    if local_status_filter not in {"all", *local_status_labels}:
        local_status_filter = "all"
    if priority_filter not in {"all", "high", "normal"}:
        priority_filter = "all"
    view_mode = str(
        request.args.get("view")
        or "all"
    ).strip().casefold()
    assignment_type = str(
        request.args.get("assignment_type")
        or "all"
    ).strip().casefold()

    allowed_assignment_types = {
        "all",
        CommissionAssignmentType.INTERNAL.value,
        CommissionAssignmentType.EXTERNAL.value,
    }

    if assignment_type not in allowed_assignment_types:
        assignment_type = "all"

    query = Commission.query.options(
        selectinload(Commission.user),
        selectinload(Commission.local_official),
        selectinload(Commission.created_by),
    )

    creator_choices = User.query.join(Commission, Commission.created_by_user_id == User.id).distinct().order_by(User.display_name, User.id).all()
    if creator_filter != "all":
        query = query.filter(Commission.created_by_user_id == (current_user.id if creator_filter == "mine" else int(creator_filter)))

    if view_mode == "done":
        query = query.filter(
            Commission.deleted_at.is_(None),
            Commission.local_status
            == CommissionLocalStatus.DONE.value,
        )
    elif view_mode == "deleted":
        query = query.filter(
            or_(
                Commission.deleted_at.is_not(None),
                Commission.local_status
                == CommissionLocalStatus.DELETED.value,
            )
        )
    elif view_mode == "active":
        query = query.filter(
            Commission.deleted_at.is_(None),
            Commission.review_resolved_at.is_(None),
            Commission.local_status.notin_([
                CommissionLocalStatus.DONE.value,
                CommissionLocalStatus.CANCELED.value,
                CommissionLocalStatus.DELETED.value,
            ]),
        )
    else:
        view_mode = "all"

    if assignment_type != "all":
        query = query.filter(
            Commission.assignment_type
            == assignment_type
        )

    items = query.order_by(
        Commission.synced_at.desc()
    ).all()

    candidate_items = list(items)
    commission_type_labels = {
        item.erp_id: (item.code or "").strip() or f"Auftragsart {item.erp_id}"
        for item in WowiCommissionType.query.all()
    }
    commission_types_by_commission_id = {
        item.id: (
            str(item.commission_type_erp_id) if item.commission_type_erp_id is not None else "unassigned",
            commission_type_labels.get(item.commission_type_erp_id, f"Auftragsart {item.commission_type_erp_id}")
            if item.commission_type_erp_id is not None else "Ohne Auftragsart",
        )
        for item in candidate_items
    }
    commission_type_options = dict(commission_types_by_commission_id.values())
    if commission_type_filter != "all":
        commission_type_options.setdefault(commission_type_filter,
            "Ohne Auftragsart" if commission_type_filter == "unassigned" else
            commission_type_labels.get(int(commission_type_filter), f"Auftragsart {commission_type_filter}"))
        items = [item for item in items if commission_types_by_commission_id[item.id][0] == commission_type_filter]
    commission_type_choices = sorted(commission_type_options.items(), key=lambda choice: (choice[1].casefold(), choice[0]))
    officials_by_commission_id = commission_official_labels(candidate_items)
    official_options = dict(officials_by_commission_id.values())
    official_options.pop("unassigned", None)
    official_choices = sorted(
        official_options.items(),
        key=lambda choice: (choice[1].casefold(), choice[0]),
    )

    if official_filter != "all":
        items = [
            item for item in items
            if officials_by_commission_id[item.id][0] == official_filter
        ]
    if local_status_filter != "all":
        items = [
            item for item in items
            if item.display_local_status == local_status_filter
        ]
    if priority_filter != "all":
        items = [
            item for item in items
            if bool(item.is_high_priority) == (priority_filter == "high")
        ]

    external_craftsman_ids = {
        item.craftsman_erp_id
        for item in candidate_items
        if (
            item.assignment_type
            == CommissionAssignmentType.EXTERNAL.value
            and item.craftsman_erp_id is not None
        )
    }
    external_craftsmen_by_erp_id: dict[
        int,
        ExternalCraftsman,
    ] = {}

    if external_craftsman_ids:
        external_craftsmen_by_erp_id = {
            craftsman.craftsman_erp_id: craftsman
            for craftsman in (
                ExternalCraftsman.query
                .filter(
                    ExternalCraftsman
                    .craftsman_erp_id
                    .in_(
                        sorted(
                            external_craftsman_ids
                        )
                    )
                )
                .all()
            )
        }

    craftsmen_by_commission_id = commission_craftsman_labels(candidate_items, external_craftsmen_by_erp_id)
    craftsman_options = dict(craftsmen_by_commission_id.values())
    craftsman_choices = sorted(craftsman_options.items(), key=lambda choice: (choice[1].casefold(), choice[0]))
    if craftsman_filter != "all":
        items = [item for item in items if craftsmen_by_commission_id[item.id][0] == craftsman_filter]

    return render_template(
        "admin/commissions.html",
        format_execution_datetime=format_local_datetime,
        items=items,
        creator_filter=creator_filter,
        creator_choices=creator_choices,
        view_mode=view_mode,
        assignment_type=assignment_type,
        external_craftsmen_by_erp_id=(
            external_craftsmen_by_erp_id
        ),
        official_filter=official_filter,
        official_choices=official_choices,
        official_options=official_options,
        officials_by_commission_id=officials_by_commission_id,
        local_status_filter=local_status_filter,
        local_status_labels=local_status_labels,
        priority_filter=priority_filter,
        craftsman_filter=craftsman_filter,
        craftsman_options=craftsman_options,
        craftsman_choices=craftsman_choices,
        commission_type_filter=commission_type_filter,
        commission_type_choices=commission_type_choices,
        commission_types_by_commission_id=commission_types_by_commission_id,
        list_return_url=url_for("admin.commissions") + ("?" + request.full_path.partition("?")[2] if request.query_string else ""),
    )


@admin_bp.post("/admin/commissions/<int:commission_id>/priority")
@foreman_required
def commission_priority(commission_id: int) -> Any:
    item = db.get_or_404(Commission, commission_id)
    try:
        changed = set_manual_commission_priority(
            item, request.form.get("priority", ""),
        )
        db.session.commit()
        flash(
            "Priorität gespeichert. Wowiport ändert diese Priorität künftig nicht mehr."
            if changed else "Die Priorität ist unverändert.",
            "success" if changed else "info",
        )
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "danger")

    return redirect(
        commission_detail_url(item.id)
    )


@admin_bp.get(
    "/admin/commissions/<int:commission_id>"
)
@foreman_required
def commission_detail(
    commission_id: int,
) -> Any:
    item = db.get_or_404(
        Commission,
        commission_id,
    )

    process_infos: list[ProcessInfo] = []
    process_info_sync_error: str | None = None

    if item.crafts_process_erp_id is not None:
        try:
            sync_process_infos(
                item.crafts_process_erp_id,
                notify=True,
            )
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            process_info_sync_error = str(exc)
            current_app.logger.exception(
                "process_info_admin_sync_failed "
                "commission_id=%s",
                item.id,
            )

        process_infos = (
            ProcessInfo.query.filter(
                ProcessInfo.process_erp_id
                == item.crafts_process_erp_id,
                ProcessInfo.visible_filter(),
            )
            .order_by(
                ProcessInfo.erp_date
                .is_(None)
                .asc(),
                ProcessInfo.erp_date.asc(),
                ProcessInfo.created_at.asc(),
            )
            .all()
        )

    photos = (
        CommissionPhoto.query
        .filter(
            CommissionPhoto.commission_id
            == item.id,
            CommissionPhoto.active.is_(True),
            CommissionPhoto.deleted_at.is_(None),
        )
        .order_by(
            CommissionPhoto.created_at.desc(),
            CommissionPhoto.id.desc(),
        )
        .all()
    )

    attachments = CommissionAttachment.query.filter_by(
        commission_id=item.id, active=True,
    ).order_by(CommissionAttachment.created_at.desc(), CommissionAttachment.id.desc()).all()

    appointment_requests = (
        CommissionAppointmentRequest.query
        .filter_by(
            commission_id=item.id
        )
        .order_by(
            CommissionAppointmentRequest.created_at.desc(),
            CommissionAppointmentRequest.id.desc(),
        )
        .all()
    )

    history_entries = (
        AuditLog.query
        .filter_by(
            entity_type="commission",
            entity_id=str(item.id),
        )
        .order_by(
            AuditLog.created_at.desc(),
            AuditLog.id.desc(),
        )
        .limit(250)
        .all()
    )

    local_status_labels = {
        "recorded": "Erfasst",
        "waiting_for_approval": "Wartet auf Freigabe",
        "waiting_for_acceptance": "Warte auf Annahme",
        "open": "Beauftragt",
        "in_progress": "In Bearbeitung",
        "completion_review": "Nicht abgeschlossen",
        "rejected": "Abgelehnt",
        "tenant_charge_review": "Mieterbelastung",
        "done": "Erledigt",
        "awaiting_invoice": "Rechnungseingang erwartet",
        "canceled": "Storniert",
        "deleted": "Gelöscht",
    }

    appointment_status_labels = {
        "pending": "Rückmeldung ausstehend",
        "accepted": "Angenommen",
        "rejected": "Abgelehnt",
        "superseded": "Nicht mehr aktuell",
        "expired": "Abgelaufen",
        "cancelled": "Termin aufgehoben",
    }

    history_titles = {
        "commission.cancel": "Auftragsstorno im Web veranlasst",
        "commission.reject": "Auftrag vom Handwerker abgelehnt",
        "commission.review.acknowledged": "Prüfauftrag zur Kenntnis genommen",
        "commission.review.reissued": "Auftrag neu erteilt",
        "commission.create.web": "Auftrag im Web erfasst",
        "commission.create.ios": "Auftrag in der App erfasst",
        "commission.approval.requested": "Auftrag zur Freigabe vorgelegt",
        "commission.approval.approved": "Auftrag freigegeben",
        "commission.start": "Auftrag angenommen",
        "commission.complete": "Auftragsabschluss übermittelt",
        "commission.sync": "Auftrag synchronisiert",
        "appointment.sent": "Terminvorschläge gesendet",
        "appointment.accepted": "Termin bestätigt",
        "appointment.rejected": "Terminvorschläge abgelehnt",
        "appointment.cancelled": "Bestätigten Termin aufgehoben",
        "erp.outbox.succeeded": "Mit Wowiport synchronisiert",
        "erp.outbox.failed": "Wowiport-Synchronisierung fehlgeschlagen",
        "webhook.wowiport": "Änderung aus Wowiport übernommen",
        "commission.photo.upload": "Foto hinzugefügt",
        "commission.attachment.upload": "Datei hinzugefügt",
        "commission.priority.web": "Priorität im Web geändert",
        "commission.priority.wowiport": "Ticketpriorität aus Wowiport übernommen",
    }

    return render_template(
        "admin/commission_detail.html",
        item=item,
        list_return_url=commission_list_return_url(),
        manual_appointment_text=format_manual_appointment(item),
        format_execution_datetime=format_local_datetime,
        wowiport_url=commission_wowiport_url(item),
        can_approve=can_approve_commission(item, current_user),
        can_handle_review=item.requires_office_review and can_handle_commission_review(item, current_user),
        photos=photos,
        attachments=attachments,
        appointment_requests=appointment_requests,
        history_entries=history_entries,
        local_status_labels=local_status_labels,
        appointment_status_labels=appointment_status_labels,
        history_titles=history_titles,
        has_work_report=bool(
            item.time_entries
        ),
        process_infos=process_infos,
        process_info_sync_error=process_info_sync_error,
        office_users=User.query.filter(
            User.is_active_flag.is_(True),
            or_(
                User.is_admin.is_(True),
                User.is_foreman.is_(True),
            ),
        ).order_by(User.display_name.asc()).all(),
    )


@admin_bp.post("/admin/commissions/<int:commission_id>/cancel")
@foreman_required
def commission_cancel(commission_id: int) -> Any:
    commission = db.get_or_404(Commission, commission_id, populate_existing=True, with_for_update=True)
    try:
        cancel_commission(commission, user=current_user)
        db.session.commit()
        flash("Die Stornierung wurde vorgemerkt und wird an Wowiport übertragen.", "success")
    except CommissionWorkflowError as exc:
        db.session.rollback()
        flash(exc.message, "danger")
    return redirect(commission_detail_url(commission_id))


@admin_bp.post(
    "/admin/commissions/<int:commission_id>/infos"
)
@foreman_required
def commission_info_create(
    commission_id: int,
) -> Any:
    commission = db.get_or_404(
        Commission,
        commission_id,
    )
    information = None

    try:
        information = create_office_info(
            commission=commission,
            user=current_user,
            request_id=str(uuid4()),
            body=request.form.get("body"),
        )

        if information.state != "synced":
            information = write_outgoing_info(
                information.id
            )

    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "danger")

    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception(
            "process_info_admin_create_failed "
            "commission_id=%s",
            commission.id,
        )

        if information is None:
            flash(
                "Die Information konnte nicht "
                "gespeichert werden.",
                "danger",
            )
        else:
            flash(
                "Die Information wurde lokal gespeichert, "
                "aber die Übertragung an Wowiport konnte "
                f"nicht bestätigt werden: {exc}",
                "warning",
            )

    else:
        flash(
            "Die Information wurde in Wowiport gespeichert.",
            "success",
        )

    return redirect(
        commission_detail_url(commission.id, "zusatzinformationen")
    )


@admin_bp.get("/admin/commission-reviews")
@office_review_required
def commission_reviews() -> Any:
    selected_status = request.args.get(
        "status",
        "",
    ).strip()

    status_labels = {
        "rejected": "Abgelehnt",
        CommissionLocalStatus.WAITING_FOR_APPROVAL.value: "Freigabe ausstehend",
        CommissionLocalStatus.COMPLETION_REVIEW.value:
            "Nicht abgeschlossen",
        CommissionLocalStatus.TENANT_CHARGE_REVIEW.value:
            "Mieterbelastung",
        "overdue": "Durchführungszeitraum überschritten",
    }

    query = commission_review_query()

    if selected_status == "rejected":
        query = query.filter(Commission.rejected_at.is_not(None))
    elif selected_status == "overdue":
        query = query.filter(overdue_commission_filter())
    elif selected_status in status_labels:
        query = query.filter(
            Commission.local_status
            == selected_status
        )
        if selected_status == CommissionLocalStatus.COMPLETION_REVIEW.value:
            query = query.filter(Commission.rejected_at.is_(None))
    else:
        selected_status = ""

    items = query.order_by(
        (Commission.approval_state == "pending").desc(),
        Commission.completion_submitted_at.desc(),
        Commission.id.desc(),
    ).all()

    return render_template(
        "admin/commission_reviews.html",
        format_execution_datetime=format_local_datetime,
        items=items,
        selected_status=selected_status,
        status_labels=status_labels,
    )


@admin_bp.post("/admin/commissions/<int:commission_id>/acknowledge")
@office_review_required
def commission_acknowledge(commission_id: int) -> Any:
    try:
        item = review_commission(commission_id, current_user, lock=True, require_open=False)
        if item.review_resolved_at is not None:
            flash("Dieser Prüfauftrag wurde bereits bearbeitet.", "info")
        elif item.deleted_at is not None:
            raise ValueError("Dieser Prüfauftrag ist nicht mehr verfügbar.")
        else:
            resolve_commission_review(item, current_user)
            db.session.commit()
            flash("Zur Kenntnis genommen. Der Auftrag ist nicht mehr zur Prüfung offen.", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return redirect(url_for("admin.commission_reviews"))


@admin_bp.post("/admin/commissions/<int:commission_id>/approve")
@office_review_required
def commission_approve(commission_id: int) -> Any:
    try:
        item, changed = approve_commission(commission_id, current_user)
        db.session.commit()
        flash("Der Auftrag wurde freigegeben und zur Übertragung an Wowiport eingeplant." if changed else "Der Auftrag ist bereits freigegeben.", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return redirect(commission_detail_url(commission_id))


def commission_photo_for_admin(
    commission_id: int,
    photo_id: int,
) -> CommissionPhoto:
    db.get_or_404(
        Commission,
        commission_id,
    )

    photo = (
        CommissionPhoto.query
        .filter(
            CommissionPhoto.id == photo_id,
            CommissionPhoto.commission_id
            == commission_id,
            CommissionPhoto.active.is_(True),
            CommissionPhoto.deleted_at.is_(None),
        )
        .one_or_none()
    )

    if photo is None:
        abort(404)

    return photo


@admin_bp.get("/admin/tickets/<int:ticket_id>/attachments")
@foreman_required
def ticket_attachments(ticket_id: int) -> Any:
    ticket = TicketMirror.query.filter_by(id=ticket_id, deleted_at=None).first_or_404()
    try:
        items = ticket_attachment_choices(ticket, WowiportService())
    except Exception:
        current_app.logger.exception("ticket_attachment_list_failed ticket_id=%s", ticket_id)
        return jsonify({"message": "Die Ticketanhänge konnten nicht geladen werden. Bitte erneut versuchen."}), 502
    return jsonify({"items": [{
        **{key: item[key] for key in ("file_guid", "filename", "is_image", "is_pdf", "type_label")},
        "thumbnail_url": url_for("admin.ticket_attachment_content", ticket_id=ticket.id, file_guid=item["file_guid"], thumbnail="true") if item["is_image"] else None,
        "content_url": url_for("admin.ticket_attachment_content", ticket_id=ticket.id, file_guid=item["file_guid"]),
    } for item in items]})


@admin_bp.get("/admin/tickets/<int:ticket_id>/attachments/<string:file_guid>/content")
@foreman_required
def ticket_attachment_content(ticket_id: int, file_guid: str) -> Any:
    ticket = TicketMirror.query.filter_by(id=ticket_id, deleted_at=None).first_or_404()
    service = WowiportService()
    try:
        attachment = next((item for item in ticket_attachment_choices(ticket, service) if item["file_guid"] == file_guid), None)
        if attachment is None:
            abort(404)
        content = read_ticket_attachment(attachment, service)
        mimetype = "application/octet-stream"
        as_attachment = True
        filename = secure_filename(attachment["filename"]) or "ticketanhang"
        if request.args.get("thumbnail") == "true":
            content = render_ticket_photo(content, thumbnail=True)
            mimetype, as_attachment, filename = "image/jpeg", False, "vorschau.jpg"
        elif attachment["is_image"]:
            try:
                content = render_ticket_photo(content)
                mimetype, as_attachment, filename = "image/jpeg", False, "ticketfoto.jpg"
            except ValueError:
                pass
        elif attachment["is_pdf"] and content.lstrip().startswith(b"%PDF-"):
            mimetype, as_attachment = "application/pdf", False
    except (RuntimeError, ValueError):
        current_app.logger.exception("ticket_attachment_content_failed ticket_id=%s", ticket_id)
        return jsonify({"message": "Der Ticketanhang konnte nicht geöffnet werden."}), 502
    response = send_file(BytesIO(content), mimetype=mimetype, download_name=filename, as_attachment=as_attachment)
    response.headers["Cache-Control"] = "private, max-age=300"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@admin_bp.post(
    "/admin/commissions/<int:commission_id>/photos"
)
@admin_bp.post(
    "/admin/commissions/<int:commission_id>/attachments"
)
@foreman_required
def commission_photo_upload(
    commission_id: int,
) -> Any:
    commission = db.get_or_404(
        Commission,
        commission_id,
    )

    uploads = [upload for upload in request.files.getlist("photo") if upload.filename]

    if not uploads:
        flash(
            "Bitte eine Datei auswählen.",
            "danger",
        )
        return redirect(
            commission_detail_url(commission.id)
        )

    stored_paths = []
    duplicates = 0
    try:
        for upload in uploads:
            _, duplicate = save_web_commission_upload(
                commission, upload, request.form.get("photo_caption"), stored_paths,
            )
            duplicates += int(duplicate)
        db.session.commit()
        stored_paths.clear()
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    else:
        if duplicates == len(uploads):
            flash(
                "Die ausgewählten Dateien sind bereits vorhanden.",
                "warning",
            )
        else:
            flash(
                "Die Dateien wurden gespeichert, sind in der App verfügbar und werden an Wowiport übertragen.",
                "success",
            )
    finally:
        for path in stored_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                current_app.logger.exception("commission_attachment_cleanup_failed")

    return redirect(
        commission_detail_url(commission.id)
    )


@admin_bp.get("/admin/commissions/<int:commission_id>/attachments/<int:attachment_id>/content")
@foreman_required
def commission_attachment_content(commission_id: int, attachment_id: int) -> Any:
    attachment = CommissionAttachment.query.filter_by(
        id=attachment_id, commission_id=commission_id, active=True,
    ).first_or_404()
    path = commission_attachment_path(attachment.storage_name)
    if not path.is_file():
        abort(404)
    response = send_file(
        path, mimetype=attachment.mime_type, download_name=attachment.original_filename,
        as_attachment=attachment.mime_type not in ("application/pdf", "image/jpeg", "image/png", "image/gif", "image/webp"),
        conditional=True, max_age=3600,
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.cache_control.private = True
    response.cache_control.public = False
    return response


@admin_bp.get(
    "/admin/commissions/<int:commission_id>/photos/"
    "<int:photo_id>/thumbnail"
)
@foreman_required
def commission_photo_thumbnail(
    commission_id: int,
    photo_id: int,
) -> Any:
    photo = commission_photo_for_admin(
        commission_id,
        photo_id,
    )

    path = commission_photo_path(
        photo.thumbnail_storage_name
    )

    if not path.is_file():
        abort(404)

    return send_file(
        path,
        mimetype="image/jpeg",
        as_attachment=False,
        conditional=True,
        max_age=3600,
    )


@admin_bp.get(
    "/admin/commissions/<int:commission_id>/photos/"
    "<int:photo_id>/content"
)
@foreman_required
def commission_photo_content(
    commission_id: int,
    photo_id: int,
) -> Any:
    photo = commission_photo_for_admin(
        commission_id,
        photo_id,
    )

    path = commission_photo_path(
        photo.storage_name
    )

    if not path.is_file():
        abort(404)

    return send_file(
        path,
        mimetype=photo.mime_type,
        as_attachment=False,
        download_name=photo.original_filename,
        conditional=True,
        max_age=3600,
    )


@admin_bp.post(
    "/admin/commissions/<int:commission_id>/sync"
)
@foreman_required
def commission_sync(commission_id: int) -> Any:
    item = db.get_or_404(Commission, commission_id)
    next_url = safe_next_url(
        "admin.commissions"
    )

    if item.erp_id is None:
        flash(
            "Der Auftrag besitzt noch keine Wowiport-ID. "
            "Die Übertragung wird automatisch wiederholt.",
            "warning",
        )

        return redirect(next_url)

    sync_commission_from_erp(item.erp_id)
    audit(
        "commission.sync",
        "commission",
        item.id,
    )
    db.session.commit()

    flash(
        "Auftrag wurde synchronisiert.",
        "success",
    )

    return redirect(next_url)


@admin_bp.route(
    "/admin/inventory-locations",
    methods=["GET", "POST"],
)
@foreman_required
def inventory_locations() -> Any:
    if request.method == "POST":
        item = InventoryLocation()

        try:
            save_inventory_location_form(
                item
            )
            db.session.add(item)
            db.session.flush()

            audit(
                "inventory.location.create",
                "inventory_location",
                item.id,
            )

            db.session.commit()
            flash(
                "Lagerort gespeichert.",
                "success",
            )
        except ValueError as exc:
            db.session.rollback()
            flash(
                str(exc),
                "danger",
            )

        return redirect(
            url_for(
                "admin.inventory_locations"
            )
        )

    items = InventoryLocation.query.order_by(
        InventoryLocation.is_default.desc(),
        InventoryLocation.active.desc(),
        InventoryLocation.name.asc(),
    ).all()

    return render_template(
        "admin/inventory_locations.html",
        items=items,
        location_types=InventoryLocationType,
    )


@admin_bp.post(
    "/admin/inventory-locations/<int:item_id>"
)
@foreman_required
def inventory_location_update(
    item_id: int,
) -> Any:
    item = db.get_or_404(
        InventoryLocation,
        item_id,
    )

    try:
        save_inventory_location_form(
            item
        )

        audit(
            "inventory.location.update",
            "inventory_location",
            item.id,
        )

        db.session.commit()
        flash(
            "Lagerort aktualisiert.",
            "success",
        )
    except ValueError as exc:
        db.session.rollback()
        flash(
            str(exc),
            "danger",
        )

    return redirect(
        url_for(
            "admin.inventory_locations"
        )
    )


@admin_bp.route(
    "/admin/inventory-classifications",
    methods=["GET", "POST"],
)
@foreman_required
def inventory_classifications() -> Any:
    if request.method == "POST":
        entity_type = request.form.get(
            "entity_type",
            "",
        ).strip()

        try:
            if entity_type == "group":
                raw_entity_id = request.form.get(
                    "entity_id",
                    "",
                ).strip()
                item_group = (
                    db.get_or_404(
                        InventoryItemGroup,
                        int(raw_entity_id),
                    )
                    if raw_entity_id
                    else InventoryItemGroup()
                )

                save_inventory_group_form(
                    item_group
                )

                if item_group.id is None:
                    db.session.add(
                        item_group
                    )

                db.session.flush()

                audit(
                    "inventory.group.save",
                    "inventory_item_group",
                    item_group.id,
                )

                message = (
                    "Artikelgruppe gespeichert."
                )

            elif entity_type == "type":
                raw_entity_id = request.form.get(
                    "entity_id",
                    "",
                ).strip()
                item_type = (
                    db.get_or_404(
                        InventoryItemType,
                        int(raw_entity_id),
                    )
                    if raw_entity_id
                    else InventoryItemType()
                )

                save_inventory_type_form(
                    item_type
                )

                if item_type.id is None:
                    db.session.add(
                        item_type
                    )

                db.session.flush()

                audit(
                    "inventory.type.save",
                    "inventory_item_type",
                    item_type.id,
                )

                message = (
                    "Artikelart gespeichert."
                )

            else:
                raise ValueError(
                    "Die Art des Datensatzes ist ungültig."
                )

            db.session.commit()
            flash(
                message,
                "success",
            )

        except (ValueError, IntegrityError) as exc:
            db.session.rollback()

            flash(
                (
                    str(exc)
                    if isinstance(exc, ValueError)
                    else (
                        "Code oder Bezeichnung "
                        "ist bereits vergeben."
                    )
                ),
                "danger",
            )

        return redirect(
            url_for(
                "admin.inventory_classifications"
            )
        )

    groups = (
        InventoryItemGroup.query
        .order_by(
            InventoryItemGroup.active.desc(),
            InventoryItemGroup.name.asc(),
        )
        .all()
    )

    item_types = (
        InventoryItemType.query
        .join(
            InventoryItemGroup,
            InventoryItemType.group_id
            == InventoryItemGroup.id,
        )
        .order_by(
            InventoryItemGroup.name.asc(),
            InventoryItemType.active.desc(),
            InventoryItemType.name.asc(),
        )
        .all()
    )

    return render_template(
        "admin/inventory_classifications.html",
        groups=groups,
        item_types=item_types,
    )


@admin_bp.get("/admin/inventory")
@foreman_required
def inventory() -> Any:
    items = (
        InventoryItem.query
        .order_by(
            InventoryItem.name.asc()
        )
        .all()
    )

    return render_template(
        "admin/inventory.html",
        items=items,
    )


@admin_bp.route(
    "/admin/inventory-items",
    methods=["GET", "POST"],
)
@foreman_required
def inventory_items() -> Any:
    if request.method == "POST":
        item = InventoryItem()

        try:
            save_inventory_form(item)
            db.session.add(item)
            db.session.flush()

            audit(
                "inventory.create",
                "inventory_item",
                item.id,
            )

            db.session.commit()

        except (
            ValueError,
            IntegrityError,
        ) as exc:
            db.session.rollback()

            flash(
                (
                    str(exc)
                    if isinstance(exc, ValueError)
                    else (
                        "Artikelnummer oder EAN "
                        "ist bereits vergeben."
                    )
                ),
                "danger",
            )

            return redirect(
                url_for(
                    "admin.inventory_items"
                )
            )

        flash(
            "Artikel gespeichert.",
            "success",
        )

        return redirect(
            url_for(
                "admin.inventory_item_edit",
                item_id=item.id,
            )
        )

    items = (
        InventoryItem.query
        .order_by(
            InventoryItem.active.desc(),
            InventoryItem.name.asc(),
        )
        .all()
    )

    item_types = (
        InventoryItemType.query
        .join(
            InventoryItemGroup,
            InventoryItemType.group_id
            == InventoryItemGroup.id,
        )
        .filter(
            InventoryItemType.active.is_(True),
            InventoryItemGroup.active.is_(True),
        )
        .order_by(
            InventoryItemGroup.name.asc(),
            InventoryItemType.name.asc(),
        )
        .all()
    )

    return render_template(
        "admin/inventory_items.html",
        items=items,
        item_types=item_types,
    )


@admin_bp.route(
    "/admin/inventory-items/<int:item_id>",
    methods=["GET", "POST"],
)
@foreman_required
def inventory_item_edit(
    item_id: int,
) -> Any:
    item = db.get_or_404(
        InventoryItem,
        item_id,
    )

    if request.method == "POST":
        try:
            save_inventory_form(item)

            audit(
                "inventory.update",
                "inventory_item",
                item.id,
            )

            db.session.commit()

        except (
            ValueError,
            IntegrityError,
        ) as exc:
            db.session.rollback()

            flash(
                (
                    str(exc)
                    if isinstance(exc, ValueError)
                    else (
                        "Artikelnummer oder EAN "
                        "ist bereits vergeben."
                    )
                ),
                "danger",
            )

        else:
            flash(
                "Artikel gespeichert.",
                "success",
            )

        return redirect(
            url_for(
                "admin.inventory_item_edit",
                item_id=item.id,
            )
        )

    barcodes = (
        InventoryItemBarcode.query
        .filter_by(
            item_id=item.id
        )
        .order_by(
            InventoryItemBarcode.active.desc(),
            InventoryItemBarcode.kind.asc(),
            InventoryItemBarcode.code.asc(),
        )
        .all()
    )

    barcode_kinds = [
        ("ean", "EAN"),
        ("qr", "QR-Code"),
        ("other", "Sonstiger Code"),
    ]

    item_types = (
        InventoryItemType.query
        .join(
            InventoryItemGroup,
            InventoryItemType.group_id
            == InventoryItemGroup.id,
        )
        .order_by(
            InventoryItemGroup.name.asc(),
            InventoryItemType.active.desc(),
            InventoryItemType.name.asc(),
        )
        .all()
    )

    return render_template(
        "admin/inventory_item_edit.html",
        item=item,
        item_types=item_types,
        barcodes=barcodes,
        barcode_kinds=barcode_kinds,
    )


@admin_bp.route(
    "/admin/inventory/<int:item_id>",
    methods=["GET", "POST"],
)
@foreman_required
def inventory_item(
    item_id: int,
) -> Any:
    item = db.get_or_404(
        InventoryItem,
        item_id,
    )

    if request.method == "POST":
        if (
            request.form.get("form_name")
            != "movement"
        ):
            abort(400)

        try:
            movement = InventoryMovement(
                item=item,
                user=current_user,
                movement_type=request.form.get(
                    "movement_type",
                    InventoryMovementType.STORED.value,
                ),
                quantity=float(
                    request.form.get(
                        "quantity",
                        0,
                    )
                ),
                serial_number=(
                    request.form.get(
                        "serial_number",
                        "",
                    ).strip()
                    or None
                ),
                note=(
                    request.form.get(
                        "note",
                        "",
                    ).strip()
                    or None
                ),
            )

            apply_inventory_movement(
                item,
                movement,
            )

            audit(
                "inventory.movement",
                "inventory_item",
                item.id,
                {
                    "movement_type": (
                        movement.movement_type
                    ),
                    "quantity": (
                        movement.quantity
                    ),
                    "source_location_id": (
                        movement.source_location_id
                    ),
                    "target_location_id": (
                        movement.target_location_id
                    ),
                },
            )

            db.session.commit()

        except (
            TypeError,
            ValueError,
        ) as exc:
            db.session.rollback()

            flash(
                str(exc),
                "danger",
            )

        else:
            flash(
                "Bestandsbuchung gespeichert.",
                "success",
            )

        return redirect(
            url_for(
                "admin.inventory_item",
                item_id=item.id,
            )
        )

    movements = (
        InventoryMovement.query
        .filter_by(
            item_id=item.id
        )
        .order_by(
            InventoryMovement.occurred_at.desc()
        )
        .all()
    )

    locations = (
        InventoryLocation.query
        .filter(
            InventoryLocation.active.is_(True)
        )
        .order_by(
            InventoryLocation.is_default.desc(),
            InventoryLocation.name.asc(),
        )
        .all()
    )

    stocks_by_location_id = {
        stock.location_id: stock.quantity
        for stock in item.stocks
    }

    location_stocks = [
        {
            "location": location,
            "quantity": stocks_by_location_id.get(
                location.id,
                0,
            ),
        }
        for location in locations
    ]

    movement_type_labels = {
        InventoryMovementType.WITHDRAWAL.value: (
            "Entnahme"
        ),
        InventoryMovementType.STORED.value: (
            "Eingelagert"
        ),
        InventoryMovementType.ORDERED.value: (
            "Bestellt"
        ),
        InventoryMovementType.CORRECTION.value: (
            "Korrektur"
        ),
        InventoryMovementType.CANCELLATION.value: (
            "Storno"
        ),
        InventoryMovementType.TRANSFER.value: (
            "Umbuchung"
        ),
    }

    return render_template(
        "admin/inventory_item.html",
        item=item,
        movements=movements,
        locations=locations,
        location_stocks=location_stocks,
        movement_type_labels=(
            movement_type_labels
        ),
    )


@admin_bp.post(
    "/admin/inventory-items/<int:item_id>/barcodes"
)
@foreman_required
def inventory_barcode_create(
    item_id: int,
) -> Any:
    item = db.get_or_404(
        InventoryItem,
        item_id,
    )

    code = request.form.get(
        "code",
        "",
    ).strip()

    kind = request.form.get(
        "kind",
        "ean",
    ).strip()

    description = (
        request.form.get(
            "description",
            "",
        ).strip()
        or None
    )

    if not code:
        flash(
            "Der Code darf nicht leer sein.",
            "danger",
        )

        return redirect(
            url_for(
                "admin.inventory_item_edit",
                item_id=item.id,
            )
        )

    if kind not in {
        "ean",
        "qr",
        "other",
    }:
        kind = "other"

    if inventory_barcode_conflicts(
        code,
        item.id,
    ):
        flash(
            "Dieser Code ist bereits einem Artikel zugeordnet.",
            "danger",
        )

        return redirect(
            url_for(
                "admin.inventory_item_edit",
                item_id=item.id,
            )
        )

    barcode = InventoryItemBarcode(
        item=item,
        code=code,
        kind=kind,
        description=description,
        active=True,
    )

    db.session.add(barcode)
    db.session.flush()

    audit(
        "inventory.barcode.create",
        "inventory_item",
        item.id,
        {
            "barcode_id": barcode.id,
            "code": barcode.code,
            "kind": barcode.kind,
        },
    )

    db.session.commit()

    flash(
        "Code hinzugefügt.",
        "success",
    )

    return redirect(
        url_for(
            "admin.inventory_item_edit",
            item_id=item.id,
        )
    )


@admin_bp.post(
    "/admin/inventory-items/<int:item_id>/barcodes/"
    "<int:barcode_id>"
)
@foreman_required
def inventory_barcode_update(
    item_id: int,
    barcode_id: int,
) -> Any:
    item = db.get_or_404(
        InventoryItem,
        item_id,
    )

    barcode = (
        InventoryItemBarcode.query
        .filter_by(
            id=barcode_id,
            item_id=item.id,
        )
        .one_or_none()
    )

    if barcode is None:
        abort(404)

    code = request.form.get(
        "code",
        "",
    ).strip()

    kind = request.form.get(
        "kind",
        "ean",
    ).strip()

    description = (
        request.form.get(
            "description",
            "",
        ).strip()
        or None
    )

    if not code:
        flash(
            "Der Code darf nicht leer sein.",
            "danger",
        )

        return redirect(
            url_for(
                "admin.inventory_item_edit",
                item_id=item.id,
            )
        )

    if kind not in {
        "ean",
        "qr",
        "other",
    }:
        kind = "other"

    if inventory_barcode_conflicts(
        code,
        item.id,
        excluded_barcode_id=barcode.id,
    ):
        flash(
            "Dieser Code ist bereits einem Artikel zugeordnet.",
            "danger",
        )

        return redirect(
            url_for(
                "admin.inventory_item_edit",
                item_id=item.id,
            )
        )

    barcode.code = code
    barcode.kind = kind
    barcode.description = description
    barcode.active = (
        "active" in request.form
    )

    audit(
        "inventory.barcode.update",
        "inventory_item",
        item.id,
        {
            "barcode_id": barcode.id,
            "code": barcode.code,
            "kind": barcode.kind,
            "active": barcode.active,
        },
    )

    db.session.commit()

    flash(
        "Code aktualisiert.",
        "success",
    )

    return redirect(
        url_for(
            "admin.inventory_item_edit",
            item_id=item.id,
        )
    )


@admin_bp.post(
    "/admin/inventory-items/<int:item_id>/barcodes/"
    "<int:barcode_id>/delete"
)
@foreman_required
def inventory_barcode_delete(
    item_id: int,
    barcode_id: int,
) -> Any:
    item = db.get_or_404(
        InventoryItem,
        item_id,
    )

    barcode = (
        InventoryItemBarcode.query
        .filter_by(
            id=barcode_id,
            item_id=item.id,
        )
        .one_or_none()
    )

    if barcode is None:
        abort(404)

    audit(
        "inventory.barcode.delete",
        "inventory_item",
        item.id,
        {
            "barcode_id": barcode.id,
            "code": barcode.code,
            "kind": barcode.kind,
        },
    )

    db.session.delete(barcode)
    db.session.commit()

    flash(
        "Code entfernt.",
        "success",
    )

    return redirect(
        url_for(
            "admin.inventory_item_edit",
            item_id=item.id,
        )
    )


@admin_bp.get("/admin/inventory/<int:item_id>/qr.png")
@foreman_required
def inventory_qr(item_id: int) -> Response:
    item = db.get_or_404(InventoryItem, item_id)
    image = qrcode.make(item.qr_code_payload)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    return Response(buffer.getvalue(), mimetype="image/png")


@admin_bp.route(
    "/admin/work-time-profiles",
    methods=["GET", "POST"],
)
@admin_required
def work_time_profiles() -> Any:
    raw_item_id = request.values.get(
        "profile_id",
        "",
    ).strip()
    item = (
        db.get_or_404(
            WorkTimeProfile,
            int(raw_item_id),
        )
        if raw_item_id.isdigit()
        else WorkTimeProfile()
    )

    if request.method == "POST":
        try:
            save_work_time_profile_form(item)

            if item.id is None:
                db.session.add(item)

            db.session.flush()
            audit(
                (
                    "work_time_profile.update"
                    if raw_item_id
                    else "work_time_profile.create"
                ),
                "work_time_profile",
                item.id,
            )
            db.session.commit()
        except (ValueError, IntegrityError) as exc:
            db.session.rollback()
            flash(
                (
                    str(exc)
                    if isinstance(exc, ValueError)
                    else (
                        "Ein Arbeitszeitprofil mit "
                        "diesem Namen existiert bereits."
                    )
                ),
                "danger",
            )
        else:
            flash(
                "Arbeitszeitprofil gespeichert.",
                "success",
            )
            return redirect(
                url_for(
                    "admin.work_time_profiles"
                )
            )

    items = (
        WorkTimeProfile.query
        .order_by(
            WorkTimeProfile.active.desc(),
            WorkTimeProfile.name.asc(),
        )
        .all()
    )

    return render_template(
        "admin/work_time_profiles.html",
        item=item,
        items=items,
        weekdays=WORK_TIME_WEEKDAYS,
    )


@admin_bp.get("/admin/work-time-reports")
@office_review_required
def work_time_reports() -> Any:
    period = request.args.get(
        "period",
        "day",
    ).strip()

    if period not in {
        "day",
        "week",
        "month",
    }:
        period = "day"

    anchor_text = request.args.get(
        "anchor",
        date.today().isoformat(),
    ).strip()

    try:
        anchor_date = date.fromisoformat(
            anchor_text
        )
    except ValueError:
        anchor_date = date.today()

    selected_user_id = request.args.get(
        "user_id",
        "",
    ).strip()
    user_choices = (
        User.query
        .filter(
            User.is_active_flag.is_(True),
            User.craftsman_erp_id.isnot(None),
        )
        .order_by(User.display_name.asc())
        .all()
    )
    selected_users = user_choices

    if selected_user_id.isdigit():
        selected_users = [
            item
            for item in user_choices
            if item.id == int(selected_user_id)
        ]

    period_start, period_end = (
        work_time_period_bounds(
            period,
            anchor_date,
        )
    )
    evaluation_end = min(
        period_end,
        date.today(),
    )
    rows = work_time_rows(
        selected_users,
        period_start,
        evaluation_end,
    )

    return render_template(
        "admin/work_time_reports.html",
        rows=rows,
        user_choices=user_choices,
        selected_user_id=selected_user_id,
        period=period,
        anchor_date=anchor_date,
        period_start=period_start,
        period_end=period_end,
        evaluation_end=evaluation_end,
    )


@admin_bp.post("/admin/work-time-overrides")
@foreman_required
def work_time_override_save() -> Any:
    raw_user_id = request.form.get(
        "user_id",
        "",
    ).strip()

    if not raw_user_id.isdigit():
        abort(400)

    user = db.get_or_404(
        User,
        int(raw_user_id),
    )

    if user.craftsman_erp_id is None:
        abort(400)

    try:
        work_date = date.fromisoformat(
            request.form.get(
                "work_date",
                "",
            ).strip()
        )
    except ValueError:
        flash(
            "Das Datum ist ungültig.",
            "danger",
        )
        return redirect_work_time_report()

    item = (
        WorkTimeTargetOverride.query
        .filter_by(
            user_id=user.id,
            work_date=work_date,
        )
        .one_or_none()
    )
    action = request.form.get(
        "action",
        "save",
    )

    if action == "delete":
        if item is not None:
            audit(
                "work_time_override.delete",
                "work_time_target_override",
                item.id,
            )
            db.session.delete(item)
            db.session.commit()

        flash(
            "Tagesabweichung entfernt.",
            "success",
        )
        return redirect_work_time_report()

    raw_target_minutes = request.form.get(
        "target_minutes",
        "",
    ).strip()
    reason = request.form.get(
        "reason",
        "",
    ).strip()

    try:
        target_minutes = int(
            raw_target_minutes
        )
    except ValueError:
        flash(
            "Die abweichende SOLL-Zeit ist ungültig.",
            "danger",
        )
        return redirect_work_time_report()

    if not 0 <= target_minutes <= 1440:
        flash(
            "Die SOLL-Zeit muss zwischen 0 und "
            "1440 Minuten liegen.",
            "danger",
        )
        return redirect_work_time_report()

    if not reason:
        flash(
            "Bitte einen Grund für die Abweichung angeben.",
            "danger",
        )
        return redirect_work_time_report()

    if item is None:
        item = WorkTimeTargetOverride(
            user=user,
            work_date=work_date,
        )
        db.session.add(item)

    item.target_minutes = target_minutes
    item.reason = reason
    item.created_by_user_id = current_user.id
    db.session.flush()

    audit(
        "work_time_override.save",
        "work_time_target_override",
        item.id,
        {
            "user_id": user.id,
            "work_date": work_date.isoformat(),
            "target_minutes": target_minutes,
        },
    )
    db.session.commit()

    flash(
        "Tagesabweichung gespeichert.",
        "success",
    )
    return redirect_work_time_report()


def redirect_work_time_report() -> Any:
    return redirect(
        url_for(
            "admin.work_time_reports",
            period=request.form.get(
                "period",
                "day",
            ),
            anchor=request.form.get(
                "anchor",
                date.today().isoformat(),
            ),
            user_id=request.form.get(
                "report_user_id",
                "",
            ),
        )
    )


def selected_reporting_period() -> ReportPeriod:
    try:
        return resolve_reporting_period(
            request.args
        )
    except ValueError as exc:
        flash(str(exc), "danger")
        return resolve_reporting_period({})


@admin_bp.get(
    "/admin/reports/service-positions"
)
@foreman_required
def report_service_positions() -> Any:
    period = selected_reporting_period()
    report = build_service_position_report(
        period
    )

    return render_template(
        "admin/report_service_positions.html",
        period=period,
        rows=report["rows"],
        summary=report["summary"],
    )


@admin_bp.get(
    "/admin/reports/commissions-by-craftsman"
)
@foreman_required
def report_commissions_by_craftsman() -> Any:
    period = selected_reporting_period()
    report = (
        build_craftsman_commission_report(
            period
        )
    )

    return render_template(
        (
            "admin/"
            "report_commissions_by_craftsman.html"
        ),
        period=period,
        rows=report["rows"],
        summary=report["summary"],
    )


@admin_bp.get(
    "/admin/reports/installed-materials"
)
@foreman_required
def report_installed_materials() -> Any:
    period = selected_reporting_period()
    report = build_installed_material_report(
        period
    )

    return render_template(
        "admin/report_installed_materials.html",
        period=period,
        rows=report["rows"],
        summary=report["summary"],
    )


@admin_bp.route("/admin/time-templates", methods=["GET", "POST"])
@foreman_required
def time_templates() -> Any:
    craft_activities = (
        CraftActivity.query
        .filter(
            CraftActivity.active.is_(True),
            CraftActivity.app_enabled.is_(True),
        )
        .order_by(
            CraftActivity.name.asc()
        )
        .all()
    )

    if request.method == "POST":
        item = TimeTaskTemplate()
        save_time_template_form(item)
        db.session.add(item)
        db.session.flush()
        audit("service_position.create", "time_task_template", item.id)
        db.session.commit()
        flash("Leistungsposition gespeichert.", "success")
        return redirect(url_for("admin.time_templates"))

    search_text = request.args.get("q", "").strip()
    raw_craft_activity_id = request.args.get("craft_activity_id", "").strip()
    status_filter = request.args.get("status", "active").strip()
    source_filter = request.args.get("source", "").strip()

    query = (
        TimeTaskTemplate.query
        .outerjoin(
            CraftActivity,
            (
                    TimeTaskTemplate.craft_activity_id
                    == CraftActivity.id
            ),
        )
        .filter(
            or_(
                TimeTaskTemplate
                .craft_activity_id
                .is_(None),
                and_(
                    CraftActivity.active.is_(
                        True
                    ),
                    CraftActivity.app_enabled.is_(
                        True
                    ),
                ),
            )
        )
    )

    if search_text:
        for term in [item.strip() for item in search_text.split() if item.strip()]:
            like_term = f"%{term}%"
            query = query.filter(or_(
                TimeTaskTemplate.code.ilike(like_term),
                TimeTaskTemplate.title.ilike(like_term),
                TimeTaskTemplate.erp_idnum.ilike(like_term),
                CraftActivity.code.ilike(like_term),
                CraftActivity.name.ilike(like_term),
            ))

    if raw_craft_activity_id:
        query = query.filter(TimeTaskTemplate.craft_activity_id == int(raw_craft_activity_id))

    if status_filter == "active":
        query = query.filter(TimeTaskTemplate.active.is_(True))
    elif status_filter == "inactive":
        query = query.filter(TimeTaskTemplate.active.is_(False))

    if source_filter:
        query = query.filter(TimeTaskTemplate.source == source_filter)

    items = query.order_by(
        TimeTaskTemplate.craft_activity_id.is_(None).asc(),
        CraftActivity.name.asc(),
        TimeTaskTemplate.title.asc(),
        TimeTaskTemplate.code.asc(),
    ).all()

    return render_template(
        "admin/time_templates.html",
        items=items,
        craft_activities=craft_activities,
        search_text=search_text,
        selected_craft_activity_id=raw_craft_activity_id,
        status_filter=status_filter,
        source_filter=source_filter,
    )


@admin_bp.route("/admin/time-templates/<int:item_id>", methods=["GET", "POST"])
@foreman_required
def time_template_edit(item_id: int) -> Any:
    item = db.get_or_404(TimeTaskTemplate, item_id)
    craft_activities = (
        CraftActivity.query
        .filter(
            CraftActivity.active.is_(True),
            or_(
                CraftActivity.app_enabled.is_(
                    True
                ),
                CraftActivity.id
                == item.craft_activity_id,
            ),
        )
        .order_by(
            CraftActivity.name.asc()
        )
        .all()
    )
    inventory_items = InventoryItem.query.filter_by(active=True).order_by(InventoryItem.name.asc()).all()
    inventory_item_types = (
        InventoryItemType.query
        .join(
            InventoryItemGroup,
            InventoryItemType.group_id
            == InventoryItemGroup.id,
        )
        .order_by(
            InventoryItemGroup.name.asc(),
            InventoryItemType.name.asc(),
        )
        .all()
    )

    inventory_items = (
        InventoryItem.query
        .order_by(
            InventoryItem.name.asc()
        )
        .all()
    )

    if request.method == "POST":
        try:
            save_time_template_form(
                item
            )
            save_expected_materials_form(
                item
            )

            audit(
                "service_position.update",
                "time_task_template",
                item.id,
            )

            db.session.commit()

            flash(
                "Leistungsposition gespeichert.",
                "success",
            )

            return redirect(
                url_for(
                    "admin.time_templates"
                )
            )

        except (ValueError, IntegrityError) as exc:
            db.session.rollback()

            flash(
                (
                    str(exc)
                    if isinstance(exc, ValueError)
                    else (
                        "Die Materialvorgabe "
                        "konnte nicht gespeichert werden."
                    )
                ),
                "danger",
            )

            return redirect(
                url_for(
                    "admin.time_template_edit",
                    item_id=item.id,
                )
            )

    return render_template("admin/time_template_form.html",
                           item=item,
                           craft_activities=craft_activities,
                           inventory_items=inventory_items,
                           inventory_item_types=inventory_item_types,
                           )


@admin_bp.get("/admin/performance-reviews")
@office_review_required
def performance_reviews() -> Any:
    status_filter = request.args.get(
        "status",
        TimeReviewStatus.NEEDS_REVIEW.value,
    ).strip()
    search_text = request.args.get(
        "q",
        "",
    ).strip().lower()
    rows = performance_review_rows()
    inspection_times = (
        select(TimeEntry)
        .where(TimeEntry.safety_inspection_job_id.isnot(None))
        .options(
            selectinload(TimeEntry.user),
            selectinload(TimeEntry.safety_inspection_job).selectinload(SafetyInspectionJob.group),
        )
        .order_by(TimeEntry.service_date.desc(), TimeEntry.id.desc())
    )

    if status_filter != "all":
        rows = [
            row
            for row in rows
            if row["status"] == status_filter
        ]

    if search_text:
        terms = search_text.split()
        inspection_times = (
            inspection_times
            .join(TimeEntry.user)
            .join(TimeEntry.safety_inspection_job)
            .join(SafetyInspectionJob.group)
            .join(SafetyInspectionJob.template_version)
            .join(SafetyInspectionTemplateVersion.template)
        )
        for term in terms:
            inspection_times = inspection_times.where(or_(
                User.display_name.icontains(term, autoescape=True),
                SafetyInspectionGroup.name.icontains(term, autoescape=True),
                SafetyInspectionJob.object_address.icontains(term, autoescape=True),
                SafetyInspectionTemplate.name.icontains(term, autoescape=True),
            ))

        rows = [
            row
            for row in rows
            if all(
                term in " ".join([
                    str(
                        row["commission"].erp_idnum
                        or ""
                    ),
                    str(
                        row["commission"].code
                        or ""
                    ),
                    str(
                        row["commission"].title
                        or ""
                    ),
                    str(
                        row["task_template"].code
                        if row["task_template"]
                        else ""
                    ),
                    str(
                        row["task_template"].title
                        if row["task_template"]
                        else ""
                    ),
                    *[
                        entry.user.display_name if entry.user else "Nicht verfügbar"
                        for entry in row["entries"]
                    ],
                    *[
                        str(line["label"])
                        for line
                        in row["material_lines"]
                    ],
                ]).lower()
                for term in terms
            )
        ]

    return render_template(
        "admin/performance_reviews.html",
        rows=rows,
        inspection_times=db.paginate(
            inspection_times,
            page=request.args.get("inspection_page", 1, type=int),
            per_page=50,
            error_out=False,
        ),
        status_filter=status_filter,
        search_text=request.args.get(
            "q",
            "",
        ).strip(),
    )


@admin_bp.post(
    "/admin/performance-reviews/"
    "<int:commission_id>/<string:scope_key>/"
    "<string:decision>"
)
@office_review_required
def performance_review_decision(
    commission_id: int,
    scope_key: str,
    decision: str,
) -> Any:
    try:
        item = review_performance_scope(
            commission_id,
            scope_key,
            decision,
            current_user,
        )
    except ValueError as exc:
        flash(str(exc), "danger")
    else:
        audit(
            "performance.review",
            "commission_performance_review",
            item.id,
            {
                "commission_id": commission_id,
                "scope_key": scope_key,
                "decision": decision,
            },
        )
        db.session.commit()
        flash(
            "Zeit- und Materialprüfung aktualisiert.",
            "success",
        )

    return redirect(
        url_for(
            "admin.performance_reviews",
            status=request.form.get(
                "status",
                "needs_review",
            ),
            q=request.form.get("q", ""),
        )
    )


@admin_bp.get("/admin/time-entries")
@office_review_required
def time_entries() -> Any:
    return redirect(
        url_for("admin.performance_reviews")
    )


@admin_bp.get("/admin/material-movements")
@office_review_required
def material_movements() -> Any:
    return redirect(
        url_for("admin.performance_reviews")
    )


@admin_bp.post(
    "/admin/service-catalog/sync"
)
@foreman_required
def service_catalog_sync() -> Any:
    result = sync_catalogs_from_wowiport()

    audit(
        "service_catalog.sync",
        metadata={
            "craft_activities_created": (
                result.craft_activities_created
            ),
            "craft_activities_updated": (
                result.craft_activities_updated
            ),
            "service_positions_created": (
                result.service_positions_created
            ),
            "service_positions_updated": (
                result.service_positions_updated
            ),
            (
                "service_positions_skipped_"
                "disabled_craft_activity"
            ): (
                result
                .service_positions_skipped_disabled_craft_activity
            ),
            "service_positions_without_craft_activity": (
                result
                .service_positions_without_craft_activity
            ),
            "service_positions_deactivated": (
                result
                .service_positions_deactivated
            ),
        },
    )
    db.session.commit()

    flash(
        "Sync abgeschlossen: "
        f"{result.craft_activities_created} "
        "Gewerke neu, "
        f"{result.craft_activities_updated} "
        "Gewerke aktualisiert, "
        f"{result.service_positions_created} "
        "Leistungspositionen neu, "
        f"{result.service_positions_updated} "
        "Leistungspositionen aktualisiert, "
        f"{result.service_positions_skipped_disabled_craft_activity} "
        "wegen global deaktiviertem Gewerk "
        "übersprungen, "
        f"{result.service_positions_without_craft_activity} "
        "ohne Gewerk, "
        f"{result.service_positions_deactivated} "
        "deaktiviert.",
        "success",
    )

    return redirect(
        url_for("admin.time_templates")
    )


def save_work_time_profile_form(
    item: WorkTimeProfile,
) -> None:
    name = request.form.get(
        "name",
        "",
    ).strip()

    if not name:
        raise ValueError(
            "Bitte einen Namen für das Profil angeben."
        )

    item.name = name
    item.active = "active" in request.form

    for field_name, label in WORK_TIME_WEEKDAYS:
        setattr(
            item,
            f"{field_name}_minutes",
            parse_work_time_value(
                request.form.get(
                    f"{field_name}_time",
                    "00:00",
                ),
                label,
            ),
        )


def parse_work_time_value(
    raw_value: str,
    label: str,
) -> int:
    parts = raw_value.strip().split(":")

    if len(parts) != 2:
        raise ValueError(
            f"Die SOLL-Zeit für {label} ist ungültig."
        )

    try:
        hours = int(parts[0])
        minutes = int(parts[1])
    except ValueError as exc:
        raise ValueError(
            f"Die SOLL-Zeit für {label} ist ungültig."
        ) from exc

    total_minutes = hours * 60 + minutes

    if (
        hours < 0
        or minutes < 0
        or minutes > 59
        or total_minutes > 1440
    ):
        raise ValueError(
            f"Die SOLL-Zeit für {label} ist ungültig."
        )

    return total_minutes


def save_user_work_time_profile_assignment(
    item: User,
) -> None:
    raw_profile_id = request.form.get(
        "work_time_profile_id",
        "",
    ).strip()

    if not raw_profile_id and item.craftsman_erp_id is None:
        return

    if not raw_profile_id.isdigit():
        raise ValueError(
            "Für einen Handwerker muss ein "
            "Arbeitszeitprofil ausgewählt werden."
        )

    profile = db.session.get(
        WorkTimeProfile,
        int(raw_profile_id),
    )

    if profile is None:
        raise ValueError(
            "Das ausgewählte Arbeitszeitprofil ist "
            "nicht verfügbar."
        )

    try:
        effective_from = date.fromisoformat(
            request.form.get(
                "work_time_profile_effective_from",
                date.today().isoformat(),
            )
        )
    except ValueError as exc:
        raise ValueError(
            "Das Startdatum des Arbeitszeitprofils "
            "ist ungültig."
        ) from exc

    exact_assignment = (
        UserWorkTimeProfileAssignment.query
        .filter_by(
            user_id=item.id,
            effective_from=effective_from,
        )
        .one_or_none()
    )
    existing_assignment = (
        current_work_time_assignment(
            item.id,
            effective_from,
        )
    )

    if (
        existing_assignment is not None
        and existing_assignment.profile_id
        == profile.id
    ):
        return

    if (
        exact_assignment is not None
        and exact_assignment.profile_id
        == profile.id
    ):
        return

    if not profile.active:
        raise ValueError(
            "Das ausgewählte Arbeitszeitprofil ist "
            "nicht aktiv."
        )

    if exact_assignment is not None:
        exact_assignment.profile = profile
        exact_assignment.created_by_user_id = (
            current_user.id
        )
        return

    db.session.add(
        UserWorkTimeProfileAssignment(
            user=item,
            profile=profile,
            effective_from=effective_from,
            created_by_user_id=current_user.id,
        )
    )


def user_work_time_profile_context(
    item: User,
) -> dict[str, Any]:
    assignments = []
    current_assignment = None

    if item.id is not None:
        assignments = (
            UserWorkTimeProfileAssignment.query
            .filter_by(user_id=item.id)
            .order_by(
                UserWorkTimeProfileAssignment
                .effective_from
                .desc(),
                UserWorkTimeProfileAssignment.id.desc(),
            )
            .all()
        )
        current_assignment = (
            current_work_time_assignment(
                item.id,
                date.today(),
            )
        )

    return {
        "work_time_profiles": (
            WorkTimeProfile.query
            .order_by(
                WorkTimeProfile.active.desc(),
                WorkTimeProfile.name.asc(),
            )
            .all()
        ),
        "current_work_time_assignment": (
            current_assignment
        ),
        "work_time_assignments": assignments,
        "work_time_effective_date": (
            date.today().isoformat()
        ),
    }


def user_commission_visibility_context(
    item: User,
) -> dict[str, Any]:
    query = User.query.filter(
        User.is_active_flag.is_(True),
        User.craftsman_erp_id.isnot(None),
    )

    if item.id is not None:
        query = query.filter(
            User.id != item.id
        )

    return {
        "commission_visibility_users": (
            query.order_by(
                User.display_name.asc()
            ).all()
        ),
        (
            "selected_commission_visibility_"
            "user_ids"
        ): {
            visible_user.id
            for visible_user
            in item.visible_commission_users
        },
    }


def save_user_commission_visibility(
    item: User,
) -> list[int]:
    try:
        selected_user_ids = {
            int(value)
            for value in request.form.getlist(
                "visible_commission_user_ids"
            )
        }
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "Die ausgewählten Handwerker sind ungültig."
        ) from exc

    if item.id is not None:
        selected_user_ids.discard(item.id)

    if not selected_user_ids:
        item.visible_commission_users = []
        return []

    selected_users = (
        User.query
        .filter(
            User.id.in_(selected_user_ids),
            User.is_active_flag.is_(True),
            User.craftsman_erp_id.isnot(None),
        )
        .order_by(User.display_name.asc())
        .all()
    )

    if len(selected_users) != len(
        selected_user_ids
    ):
        raise ValueError(
            "Mindestens ein ausgewählter "
            "Handwerker ist nicht verfügbar."
        )

    item.visible_commission_users = selected_users

    return sorted(selected_user_ids)


def save_user_form(item: User) -> None:
    save_admin_push_preferences(item, request.form)
    item.email = request.form.get("email", "").strip().lower()
    item.display_name = request.form.get("display_name", "").strip()
    for attribute, label in (("first_name", "Vorname"), ("last_name", "Nachname")):
        if attribute in request.form:
            value = request.form.get(attribute, "").strip()
            if len(value) > 255:
                raise ValueError(f"Das Feld „{label}“ darf höchstens 255 Zeichen enthalten.")
            setattr(item, attribute, value or None)
    if "phone" in request.form:
        phone = request.form.get("phone", "").strip()
        if len(phone) > 80:
            raise ValueError("Die Telefonnummer darf höchstens 80 Zeichen enthalten.")
        item.phone = phone or None
    if "entra_oid" in request.form:
        item.entra_oid = request.form.get("entra_oid") or None
    if "paperwork_user_id" in request.form or "paperwork_d3id" in request.form:
        from .invoice_dms_dispatch import save_paperwork_signer

        save_paperwork_signer(
            item,
            request.form.get("paperwork_user_id", "").strip() or None,
            request.form.get("paperwork_d3id", "").strip() or None,
        )
    raw_craftsman_id = request.form.get("craftsman_erp_id", "").strip()
    item.craftsman_erp_id = int(raw_craftsman_id) if raw_craftsman_id else None
    item.is_admin = "is_admin" in request.form
    item.is_foreman = "is_foreman" in request.form
    item.can_manage_invoices = (
        "can_manage_invoices"
        in request.form
    )
    item.can_manage_offers = (
        "can_manage_offers"
        in request.form
    )
    item.can_manage_safety_inspections = (
        "can_manage_safety_inspections"
        in request.form
    )
    item.can_review_safety_inspections = (
        "can_review_safety_inspections"
        in request.form
    )
    item.is_active_flag = "is_active_flag" in request.form
    item.exclude_from_wowiport_sync = (
        "exclude_from_wowiport_sync"
        in request.form
    )
    password = request.form.get("password", "")
    if password:
        item.set_password(password)
    save_user_approval_settings(item, request.form)


def save_user_craft_activities(
    item: User,
) -> None:
    selected_activity_ids = set()

    for value in request.form.getlist(
        "craft_activity_ids"
    ):
        try:
            selected_activity_ids.add(
                int(value)
            )
        except ValueError:
            continue

    current_activity_ids = {
        activity.id
        for activity in item.craft_activities
    }
    retained_activity_ids = set()

    for value in request.form.getlist(
        "retained_craft_activity_ids"
    ):
        try:
            activity_id = int(value)
        except ValueError:
            continue

        if activity_id in current_activity_ids:
            retained_activity_ids.add(
                activity_id
            )

    selected_activities = []

    if selected_activity_ids:
        selected_activities = (
            CraftActivity.query
            .filter(
                CraftActivity.id.in_(
                    selected_activity_ids
                ),
                CraftActivity.active.is_(True),
                CraftActivity.app_enabled.is_(
                    True
                ),
            )
            .all()
        )

    retained_activities = []

    if retained_activity_ids:
        retained_activities = (
            CraftActivity.query
            .filter(
                CraftActivity.id.in_(
                    retained_activity_ids
                )
            )
            .all()
        )

    activities_by_id = {
        activity.id: activity
        for activity in (
            selected_activities
            + retained_activities
        )
    }

    item.craft_activities = sorted(
        activities_by_id.values(),
        key=lambda activity: (
            activity.name.lower(),
            activity.id,
        ),
    )


def jurisdiction_person_candidates(
) -> list[dict[str, Any]]:
    candidates: dict[
        int,
        dict[str, Any],
    ] = {}
    blocked_person_ids = blacklist_ids("person")

    def add_candidate(
            erp_id: int,
            name: str | None,
            usage_count: int = 1,
    ) -> None:
        if erp_id in blocked_person_ids:
            return
        candidate = candidates.setdefault(
            erp_id,
            {
                "erp_id": erp_id,
                "name": None,
                "usage_count": 0,
                "mapped_user_id": None,
                "mapped_user_name": None,
            },
        )

        normalized_name = (
            name.strip()
            if name and name.strip()
            else None
        )

        if (
                candidate["name"] is None
                and normalized_name is not None
        ):
            candidate["name"] = normalized_name

        candidate["usage_count"] += usage_count

    officials = (
        WowiResponsibleOfficial.query
        .filter(
            WowiResponsibleOfficial.active.is_(True),
            WowiResponsibleOfficial.person_erp_id.isnot(None),
        )
        .all()
    )

    for official in officials:
        add_candidate(
            official.person_erp_id,
            official.name,
            usage_count=0,
        )

    jurisdictions = (
        WowiEconomicUnitJurisdiction.query
        .filter(
            WowiEconomicUnitJurisdiction.active
            .is_(True),
            WowiEconomicUnitJurisdiction
            .responsible_person_erp_id
            .isnot(None),
        )
        .all()
    )

    for jurisdiction in jurisdictions:
        add_candidate(
            jurisdiction
            .responsible_person_erp_id,
            jurisdiction.responsible_name,
        )

    economic_units = (
        WowiEconomicUnit.query
        .filter(
            WowiEconomicUnit.active.is_(True),
            WowiEconomicUnit
            .universal_responsibility
            .is_(True),
            WowiEconomicUnit
            .universal_responsible_person_erp_id
            .isnot(None),
        )
        .all()
    )

    for economic_unit in economic_units:
        add_candidate(
            economic_unit
            .universal_responsible_person_erp_id,
            None,
        )

    mappings = (
        UserJurisdictionPersonMapping.query
        .filter_by(active=True)
        .all()
    )

    for mapping in mappings:
        if mapping.jurisdiction_person_erp_id in blocked_person_ids:
            continue
        candidate = candidates.setdefault(
            mapping.jurisdiction_person_erp_id,
            {
                "erp_id": (
                    mapping
                    .jurisdiction_person_erp_id
                ),
                "name": (
                    mapping.person_name_snapshot
                ),
                "usage_count": 0,
                "mapped_user_id": None,
                "mapped_user_name": None,
            },
        )
        candidate["mapped_user_id"] = (
            mapping.user_id
        )
        candidate["mapped_user_name"] = (
            mapping.user.display_name
            if mapping.user is not None
            else None
        )

        if (
            not candidate["name"]
            and mapping.person_name_snapshot
        ):
            candidate["name"] = (
                mapping.person_name_snapshot
            )

    result = list(candidates.values())

    for candidate in result:
        if not candidate["name"]:
            candidate["name"] = (
                "Wowiport-Person "
                f"{candidate['erp_id']}"
            )

    return sorted(
        result,
        key=lambda candidate: (
            candidate["name"].lower(),
            candidate["erp_id"],
        ),
    )


def save_user_jurisdiction_person_mappings(
    item: User,
) -> list[int]:
    selected_ids = set()

    for value in request.form.getlist(
        "jurisdiction_person_erp_ids"
    ):
        try:
            erp_id = int(value)
        except ValueError as exc:
            raise ValueError(
                "Eine Wowiport-Personen-ID "
                "ist ungültig."
            ) from exc

        if erp_id <= 0:
            raise ValueError(
                "Eine Wowiport-Personen-ID "
                "ist ungültig."
            )

        selected_ids.add(erp_id)

    candidates = jurisdiction_person_candidates()
    candidate_names = {
        candidate["erp_id"]: candidate["name"]
        for candidate in candidates
    }
    allowed_ids = set(candidate_names)

    unknown_ids = selected_ids - allowed_ids

    if unknown_ids:
        raise ValueError(
            "Mindestens eine ausgewählte "
            "Wowiport-Person ist nicht mehr "
            "im lokalen Wowiport-Verzeichnis verfügbar."
        )

    current_mappings = (
        UserJurisdictionPersonMapping.query
        .filter_by(
            user_id=item.id,
            active=True,
        )
        .all()
    )

    for mapping in current_mappings:
        if (
            mapping.jurisdiction_person_erp_id
            not in selected_ids
        ):
            mapping.active = False

    for erp_id in selected_ids:
        with include_blacklisted():
            mapping = (
                UserJurisdictionPersonMapping.query
                .filter_by(
                    jurisdiction_person_erp_id=erp_id
                )
                .one_or_none()
            )

        if (
                mapping is not None
                and mapping.active
                and mapping.user_id != item.id
                and not is_blacklisted("user", mapping.user_id)
        ):
            other_user_name = (
                mapping.user.display_name
                if mapping.user is not None
                else "einem anderen Benutzer"
            )
            raise ValueError(
                f"Die Wowiport-Person {erp_id} "
                f"ist bereits {other_user_name} "
                "zugeordnet."
            )

        if mapping is None:
            mapping = (
                UserJurisdictionPersonMapping(
                    user_id=item.id,
                    jurisdiction_person_erp_id=(
                        erp_id
                    ),
                )
            )
            db.session.add(mapping)

        mapping.user_id = item.id
        mapping.person_name_snapshot = (
            candidate_names.get(erp_id)
        )
        mapping.active = True

    return sorted(selected_ids)


def save_inventory_location_form(
    item: InventoryLocation,
) -> None:
    code = request.form.get(
        "code",
        "",
    ).strip().upper()
    name = request.form.get(
        "name",
        "",
    ).strip()
    location_type = request.form.get(
        "location_type",
        InventoryLocationType.WAREHOUSE.value,
    ).strip()
    active = "active" in request.form
    requested_default = (
        "is_default" in request.form
    )

    if not code:
        raise ValueError(
            "Der Lagerort benötigt einen Code."
        )

    if not name:
        raise ValueError(
            "Der Lagerort benötigt einen Namen."
        )

    if location_type not in {
        value.value
        for value in InventoryLocationType
    }:
        raise ValueError(
            "Die Lagerortart ist ungültig."
        )

    duplicate_query = InventoryLocation.query.filter(
        InventoryLocation.code == code
    )

    if item.id is not None:
        duplicate_query = duplicate_query.filter(
            InventoryLocation.id != item.id
        )

    if duplicate_query.first() is not None:
        raise ValueError(
            "Dieser Lagerort-Code ist bereits vergeben."
        )

    current_default = (
        item.is_default
        if item.id is not None
        else False
    )
    default_exists = InventoryLocation.query.filter(
        InventoryLocation.is_default.is_(True),
        InventoryLocation.id != item.id,
    ).first() is not None

    if not default_exists and item.id is None:
        requested_default = True

    if current_default and not requested_default:
        raise ValueError(
            "Bitte zuerst einen anderen Standard-Lagerort festlegen."
        )

    if requested_default and not active:
        raise ValueError(
            "Der Standard-Lagerort muss aktiv sein."
        )

    if requested_default:
        InventoryLocation.query.filter(
            InventoryLocation.id != item.id
        ).update(
            {
                InventoryLocation.is_default: False
            },
            synchronize_session=False,
        )

    item.code = code
    item.name = name
    item.location_type = location_type
    item.active = active
    item.is_default = requested_default


def inventory_barcode_conflicts(
    code: str,
    item_id: int,
    excluded_barcode_id: int | None = None,
) -> bool:
    barcode_query = (
        InventoryItemBarcode.query
        .filter(
            InventoryItemBarcode.code == code
        )
    )

    if excluded_barcode_id is not None:
        barcode_query = barcode_query.filter(
            InventoryItemBarcode.id
            != excluded_barcode_id
        )

    if barcode_query.first() is not None:
        return True

    other_item = (
        InventoryItem.query
        .filter(
            InventoryItem.id != item_id,
            or_(
                InventoryItem.item_number == code,
                InventoryItem.ean == code,
                InventoryItem.qr_code_payload == code,
            ),
        )
        .first()
    )

    return other_item is not None


def invoice_rule_company_code_from_request(
) -> WowiCompanyCode | None:
    raw_company_code_id = (
        request.values.get(
            "company_code_id",
            "",
        ).strip()
    )

    if not raw_company_code_id:
        return None

    try:
        company_code_id = int(
            raw_company_code_id
        )
    except ValueError as exc:
        raise ValueError(
            "Das ausgewählte Buchwerk "
            "ist ungültig."
        ) from exc

    company_code = db.session.get(
        WowiCompanyCode,
        company_code_id,
    )

    if (
        company_code is None
        or not company_code.active
    ):
        raise ValueError(
            "Das ausgewählte Buchwerk "
            "ist nicht verfügbar."
        )

    return company_code


def invoice_billing_rules_url(
    company_code: WowiCompanyCode | None,
) -> str:
    if company_code is None:
        return url_for(
            "admin.invoice_billing_rules"
        )

    return url_for(
        "admin.invoice_billing_rules",
        company_code_id=company_code.id,
    )


def save_invoice_time_rules(
    company_code: WowiCompanyCode | None,
    existing_rules: dict[
        str,
        InvoiceBillingRule,
    ],
) -> None:
    for sort_order, time_type in enumerate(
        INVOICE_TIME_TYPES
    ):
        time_type_key = time_type["key"]
        state = request.form.get(
            f"time_state_{time_type_key}",
            "inherit",
        )
        rule = existing_rules.get(
            time_type_key
        )

        if (
            company_code is not None
            and state == "inherit"
        ):
            if rule is not None:
                db.session.delete(rule)

            continue

        if state not in {
            "enabled",
            "disabled",
        }:
            raise ValueError(
                "Der Status einer Zeitart "
                "ist ungültig."
            )

        if rule is None:
            rule = InvoiceBillingRule(
                company_code=company_code,
                rule_type=(
                    InvoiceBillingRuleType
                    .TIME_TYPE.value
                ),
                time_type=time_type_key,
            )
            db.session.add(rule)

        hourly_rate = optional_form_decimal(
            request.form.get(
                f"time_rate_{time_type_key}"
            ),
            (
                "Stundensatz für "
                f"{time_type['label']}"
            ),
        )
        vat_rate = optional_form_decimal(
            request.form.get(
                f"time_vat_{time_type_key}"
            ),
            (
                "Umsatzsteuersatz für "
                f"{time_type['label']}"
            ),
        )

        validate_invoice_rule_values(
            hourly_rate,
            vat_rate,
        )

        rule.enabled = state == "enabled"
        rule.position_title = (
            request.form.get(
                f"time_title_{time_type_key}",
                "",
            ).strip()
            or None
        )
        rule.hourly_rate_net = hourly_rate
        rule.vat_rate = vat_rate
        rule.sort_order = sort_order


def save_invoice_task_rules(
    company_code: WowiCompanyCode | None,
    existing_rules: dict[
        int,
        InvoiceBillingRule,
    ],
    task_templates: list[
        TimeTaskTemplate
    ],
) -> None:
    for task_template in task_templates:
        state = request.form.get(
            (
                "task_state_"
                f"{task_template.id}"
            ),
            "inherit",
        )
        rule = existing_rules.get(
            task_template.id
        )

        if state == "inherit":
            if rule is not None:
                db.session.delete(rule)

            continue

        if state not in {
            "enabled",
            "disabled",
        }:
            raise ValueError(
                "Der Status einer "
                "Leistungsposition ist ungültig."
            )

        if rule is None:
            rule = InvoiceBillingRule(
                company_code=company_code,
                rule_type=(
                    InvoiceBillingRuleType
                    .TASK_TEMPLATE.value
                ),
                task_template=task_template,
            )
            db.session.add(rule)

        hourly_rate = optional_form_decimal(
            request.form.get(
                (
                    "task_rate_"
                    f"{task_template.id}"
                )
            ),
            (
                "Stundensatz für "
                f"{task_template.title}"
            ),
        )
        vat_rate = optional_form_decimal(
            request.form.get(
                (
                    "task_vat_"
                    f"{task_template.id}"
                )
            ),
            (
                "Umsatzsteuersatz für "
                f"{task_template.title}"
            ),
        )

        validate_invoice_rule_values(
            hourly_rate,
            vat_rate,
        )

        sort_order = optional_form_int(
            request.form.get(
                (
                    "task_sort_"
                    f"{task_template.id}"
                )
            ),
            (
                "Sortierung für "
                f"{task_template.title}"
            ),
        )

        rule.enabled = state == "enabled"
        rule.position_title = (
            request.form.get(
                (
                    "task_title_"
                    f"{task_template.id}"
                ),
                "",
            ).strip()
            or None
        )
        rule.hourly_rate_net = hourly_rate
        rule.vat_rate = vat_rate
        rule.sort_order = (
            sort_order
            if sort_order is not None
            else 100
        )


def validate_invoice_rule_values(
    hourly_rate: Decimal | None,
    vat_rate: Decimal | None,
) -> None:
    if (
        hourly_rate is not None
        and hourly_rate < 0
    ):
        raise ValueError(
            "Ein Stundensatz darf "
            "nicht negativ sein."
        )

    if (
        vat_rate is not None
        and (
            vat_rate < 0
            or vat_rate > 100
        )
    ):
        raise ValueError(
            "Ein Umsatzsteuersatz muss "
            "zwischen 0 und 100 liegen."
        )



def save_company_code_invoice_form(
    item: WowiCompanyCode,
) -> None:
    invoice_number_mask = (
        request.form.get(
            "invoice_number_mask",
            "",
        ).strip()
    )

    if not invoice_number_mask:
        raise ValueError(
            "Die Rechnungsnummernmaske "
            "muss angegeben werden."
        )

    if "<counter>" not in invoice_number_mask:
        raise ValueError(
            "Die Rechnungsnummernmaske "
            "muss <counter> enthalten."
        )

    counter_digits = required_form_int(
        request.form.get(
            "invoice_counter_digits"
        ),
        "Anzahl der Ziffern",
    )

    if (
        counter_digits < 1
        or counter_digits > 12
    ):
        raise ValueError(
            "Die Anzahl der Ziffern muss "
            "zwischen 1 und 12 liegen."
        )

    offer_number_mask = (
        request.form.get(
            "offer_number_mask",
            "",
        ).strip()
    )

    if not offer_number_mask:
        raise ValueError(
            "Die Angebotsnummernmaske "
            "muss angegeben werden."
        )

    if (
        "<year>" not in offer_number_mask
        or "<counter>" not in offer_number_mask
    ):
        raise ValueError(
            "Die Angebotsnummernmaske muss "
            "<year> und <counter> enthalten."
        )

    offer_counter_digits = required_form_int(
        request.form.get(
            "offer_counter_digits"
        ),
        "Ziffern des Angebotszählers",
    )

    if (
        offer_counter_digits < 1
        or offer_counter_digits > 12
    ):
        raise ValueError(
            "Die Ziffern des Angebotszählers "
            "müssen zwischen 1 und 12 liegen."
        )

    payment_terms_days = optional_form_int(
        request.form.get(
            "invoice_payment_terms_days"
        ),
        "Zahlungsziel",
    )

    if (
        payment_terms_days is not None
        and payment_terms_days < 0
    ):
        raise ValueError(
            "Das Zahlungsziel darf "
            "nicht negativ sein."
        )

    hourly_rate = optional_form_decimal(
        request.form.get(
            "invoice_default_hourly_rate_net"
        ),
        "Stundensatz",
    )
    vat_rate = optional_form_decimal(
        request.form.get(
            "invoice_default_vat_rate"
        ),
        "Umsatzsteuersatz",
    )
    material_markup = optional_form_decimal(
        request.form.get(
            "invoice_material_markup_percent"
        ),
        "Materialaufschlag",
    )

    for value, label in (
        (hourly_rate, "Stundensatz"),
        (vat_rate, "Umsatzsteuersatz"),
        (
            material_markup,
            "Materialaufschlag",
        ),
    ):
        if value is not None and value < 0:
            raise ValueError(
                f"{label} darf nicht "
                "negativ sein."
            )

    country_code = (
        request.form.get(
            "seller_country_code",
            "DE",
        ).strip().upper()
        or "DE"
    )

    if len(country_code) != 2:
        raise ValueError(
            "Der Ländercode muss "
            "zweistellig sein."
        )

    if "tenant_charge_letter_setting" in request.form:
        item.tenant_charge_letter_enabled = "tenant_charge_letter_enabled" in request.form
    if "tenant_billing_issuer_id" in request.form:
        issuer_id = optional_form_int(request.form.get("tenant_billing_issuer_id"), "Ausstellendes Buchwerk")
        if issuer_id is not None and (issuer_id == item.id or db.session.get(WowiCompanyCode, issuer_id) is None):
            raise ValueError("Bitte wählen Sie ein anderes Buchwerk als Aussteller aus.")
        item.tenant_billing_issuer_id = issuer_id
    if "tenant_billing_recipient_email" in request.form:
        from .settings import validate_debug_email_address

        email = request.form.get("tenant_billing_recipient_email", "").strip()
        try:
            item.tenant_billing_recipient_email = validate_debug_email_address(email) or None
        except ValueError as exc:
            raise ValueError("Bitte eine gültige E-Mail-Adresse für den Rechnungseingang eingeben.") from exc

    item.document_creation_disabled = "document_creation_disabled" in request.form
    item.invoice_number_mask = (
        invoice_number_mask
    )
    item.invoice_counter_digits = (
        counter_digits
    )
    item.offer_number_mask = (
        offer_number_mask
    )
    item.offer_counter_digits = (
        offer_counter_digits
    )
    item.invoice_default_hourly_rate_net = (
        hourly_rate
    )
    item.invoice_default_vat_rate = (
        vat_rate
    )
    item.invoice_material_markup_percent = (
        material_markup
    )
    item.invoice_payment_terms_days = (
        payment_terms_days
    )
    item.seller_name = optional_form_text(
        "seller_name"
    )
    item.seller_street = optional_form_text(
        "seller_street"
    )
    item.seller_postal_code = (
        optional_form_text(
            "seller_postal_code"
        )
    )
    item.seller_town = optional_form_text(
        "seller_town"
    )
    item.seller_country_code = country_code
    item.seller_vat_id = optional_form_text(
        "seller_vat_id"
    )
    item.seller_tax_number = (
        optional_form_text(
            "seller_tax_number"
        )
    )
    item.seller_email = optional_form_text(
        "seller_email"
    )
    item.seller_phone = optional_form_text(
        "seller_phone"
    )
    item.seller_iban = optional_form_text(
        "seller_iban"
    )
    item.seller_bic = optional_form_text(
        "seller_bic"
    )
    item.seller_creditor_id = optional_form_text("seller_creditor_id")


def invoice_stationery_directory() -> Path:
    directory = (
        Path(current_app.instance_path)
        / "invoice_stationery"
    )
    directory.mkdir(
        parents=True,
        exist_ok=True,
    )
    return directory


def validate_company_code_stationery(upload):
    from pypdf import PdfReader
    content = upload.stream.read(20 * 1024 * 1024 + 1)
    upload.stream.seek(0)
    if len(content) > 20 * 1024 * 1024:
        raise ValueError("Der Briefbogen darf höchstens 20 MB groß sein.")
    try:
        reader = PdfReader(BytesIO(content))
        if not content.startswith(b"%PDF-") or reader.is_encrypted or not reader.pages:
            raise ValueError("Ungültiger Briefbogen.")
    except Exception as exc:
        raise ValueError("Der Briefbogen muss eine lesbare, unverschlüsselte PDF-Datei mit mindestens einer Seite sein.") from exc


def store_company_code_stationery(
    item: WowiCompanyCode,
    upload: Any,
    *, attribute="stationery_filename",
) -> None:
    validate_company_code_stationery(upload)

    directory = invoice_stationery_directory()
    filename = (
        f"company-code-{item.id}-"
        f"{uuid4().hex}.pdf"
    )
    file_path = directory / filename

    upload.save(file_path)

    previous_filename = getattr(item, attribute)
    setattr(item, attribute, filename)

    if (
        previous_filename
        and previous_filename != filename
    ):
        previous_path = (
            directory
            / previous_filename
        )
        previous_path.unlink(
            missing_ok=True
        )


def remove_company_code_stationery(
    item: WowiCompanyCode,
    *, attribute="stationery_filename",
) -> None:
    if not getattr(item, attribute):
        return

    file_path = (
        invoice_stationery_directory()
        / getattr(item, attribute)
    )
    file_path.unlink(
        missing_ok=True
    )
    setattr(item, attribute, None)


def optional_form_text(
    field_name: str,
) -> str | None:
    return (
        request.form.get(
            field_name,
            "",
        ).strip()
        or None
    )


def optional_form_int(
    value: str | None,
    label: str,
) -> int | None:
    if value is None or not value.strip():
        return None

    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(
            f"{label} ist ungültig."
        ) from exc


def optional_form_decimal(
    value: str | None,
    label: str,
) -> Decimal | None:
    if value is None or not value.strip():
        return None

    normalized = (
        value.strip()
        .replace(" ", "")
        .replace(",", ".")
    )

    try:
        return Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError(
            f"{label} ist ungültig."
        ) from exc


def save_inventory_group_form(
    item_group: InventoryItemGroup,
) -> None:
    code = request.form.get(
        "code",
        "",
    ).strip()
    name = request.form.get(
        "name",
        "",
    ).strip()

    if not code:
        raise ValueError(
            "Der Code der Artikelgruppe fehlt."
        )

    if not name:
        raise ValueError(
            "Die Bezeichnung der Artikelgruppe fehlt."
        )

    item_group.code = code
    item_group.name = name
    item_group.active = (
        "active"
        in request.form
    )


def save_inventory_type_form(
    item_type: InventoryItemType,
) -> None:
    code = request.form.get(
        "code",
        "",
    ).strip()
    name = request.form.get(
        "name",
        "",
    ).strip()
    unit = request.form.get(
        "unit",
        "Stk",
    ).strip()
    raw_group_id = request.form.get(
        "group_id",
        "",
    ).strip()

    if not code:
        raise ValueError(
            "Der Code der Artikelart fehlt."
        )

    if not name:
        raise ValueError(
            "Die Bezeichnung der Artikelart fehlt."
        )

    if not unit:
        raise ValueError(
            "Die Einheit der Artikelart fehlt."
        )

    if not raw_group_id:
        raise ValueError(
            "Bitte eine Artikelgruppe auswählen."
        )

    try:
        group_id = int(
            raw_group_id
        )
    except ValueError as exc:
        raise ValueError(
            "Die Artikelgruppe ist ungültig."
        ) from exc

    item_group = db.session.get(
        InventoryItemGroup,
        group_id,
    )

    if item_group is None:
        raise ValueError(
            "Die Artikelgruppe wurde nicht gefunden."
        )

    item_type.code = code
    item_type.name = name
    item_type.unit = unit
    item_type.serial_number_required = (
        "serial_number_required"
        in request.form
    )
    item_type.group = item_group
    item_type.active = (
        "active"
        in request.form
    )


def save_inventory_form(
    item: InventoryItem,
) -> None:
    item.item_number = request.form.get(
        "item_number",
        "",
    ).strip()
    item.name = request.form.get(
        "name",
        "",
    ).strip()

    if not item.item_number:
        raise ValueError(
            "Die Artikelnummer fehlt."
        )

    if not item.name:
        raise ValueError(
            "Der Artikelname fehlt."
        )

    raw_item_type_id = request.form.get(
        "item_type_id",
        "",
    ).strip()

    if raw_item_type_id:
        try:
            item_type_id = int(
                raw_item_type_id
            )
        except ValueError as exc:
            raise ValueError(
                "Die Artikelart ist ungültig."
            ) from exc

        item_type = db.session.get(
            InventoryItemType,
            item_type_id,
        )

        if item_type is None:
            raise ValueError(
                "Die Artikelart wurde nicht gefunden."
            )

        item.item_type = item_type
        item.unit = item_type.unit

    else:
        item.item_type = None
        item.unit = request.form.get(
            "unit",
            "Stk",
        ).strip()

    if not item.unit:
        raise ValueError(
            "Die Einheit fehlt."
        )

    item.ean = (
        request.form.get(
            "ean",
            "",
        ).strip()
        or None
    )
    item.reorder_level = float(
        request.form.get(
            "reorder_level",
            0,
        )
    )

    billing_base_price_net = (
        optional_form_decimal(
            request.form.get(
                "billing_base_price_net"
            ),
            "Netto-Verkaufspreis",
        )
    )
    billing_vat_rate = (
        optional_form_decimal(
            request.form.get(
                "billing_vat_rate"
            ),
            "Umsatzsteuersatz",
        )
    )

    if (
        billing_base_price_net is not None
        and billing_base_price_net < 0
    ):
        raise ValueError(
            "Der Netto-Verkaufspreis "
            "darf nicht negativ sein."
        )

    if (
        billing_vat_rate is not None
        and billing_vat_rate < 0
    ):
        raise ValueError(
            "Der Umsatzsteuersatz "
            "darf nicht negativ sein."
        )

    item.billing_base_price_net = (
        billing_base_price_net
    )
    item.billing_vat_rate = (
        billing_vat_rate
    )
    item.active = (
        "active"
        in request.form
    )


def save_time_template_form(item: TimeTaskTemplate) -> None:
    item.code = request.form.get("code", "").strip()
    item.title = request.form.get("title", "").strip()
    item.suggested_minutes = int(request.form.get("suggested_minutes", 0) or 0)
    item.review_when_shorter = (
            "review_when_shorter"
            in request.form
    )
    item.active = "active" in request.form
    item.free_time_enabled = (
        "free_time_enabled"
        in request.form
    )

    raw_craft_activity_id = request.form.get("craft_activity_id", "").strip()
    item.craft_activity_id = int(raw_craft_activity_id) if raw_craft_activity_id else None

    if not item.source:
        item.source = "manual"


def save_expected_materials_form(
    item: TimeTaskTemplate,
) -> None:
    targets = request.form.getlist(
        "expected_target"
    )
    quantities = request.form.getlist(
        "expected_quantity"
    )
    notes = request.form.getlist(
        "expected_note"
    )
    submitted_materials = {}

    for index, raw_target in enumerate(
        targets
    ):
        raw_target = raw_target.strip()

        if not raw_target:
            continue

        target_kind, separator, raw_target_id = (
            raw_target.partition(":")
        )

        if (
            not separator
            or target_kind not in {
                "type",
                "item",
            }
        ):
            raise ValueError(
                "Die Materialvorgabe ist ungültig."
            )

        try:
            target_id = int(
                raw_target_id
            )
            quantity = float(
                (
                    quantities[index]
                    if index < len(quantities)
                    else "1"
                )
                or 1
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Die Menge der Materialvorgabe ist ungültig."
            ) from exc

        if quantity <= 0:
            raise ValueError(
                "Die erwartete Materialmenge "
                "muss größer als 0 sein."
            )

        target_key = (
            target_kind,
            target_id,
        )

        if target_key in submitted_materials:
            raise ValueError(
                "Dieselbe Materialvorgabe "
                "wurde mehrfach ausgewählt."
            )

        target = db.session.get(
            (
                InventoryItemType
                if target_kind == "type"
                else InventoryItem
            ),
            target_id,
        )

        if target is None:
            raise ValueError(
                "Die ausgewählte Materialvorgabe "
                "wurde nicht gefunden."
            )

        submitted_materials[
            target_key
        ] = {
            "target": target,
            "quantity": quantity,
            "required": (
                f"expected_required_{index}"
                in request.form
            ),
            "note": (
                notes[index].strip()
                if (
                    index < len(notes)
                    and notes[index].strip()
                )
                else None
            ),
        }

    selected_type_ids = {
        target_id
        for target_kind, target_id
        in submitted_materials
        if target_kind == "type"
    }

    for target_key, values in (
        submitted_materials.items()
    ):
        target_kind, target_id = target_key

        if target_kind != "item":
            continue

        inventory_item = values["target"]

        if (
            inventory_item.item_type_id is not None
            and inventory_item.item_type_id
            in selected_type_ids
        ):
            raise ValueError(
                "Eine Artikelart und ein konkreter "
                "Artikel dieser Art dürfen nicht "
                "gleichzeitig erwartet werden."
            )

    existing_materials = {}

    for material in item.expected_materials:
        if material.item_type_id is not None:
            key = (
                "type",
                material.item_type_id,
            )
        else:
            key = (
                "item",
                material.item_id,
            )

        existing_materials[key] = material

    for target_key, values in (
        submitted_materials.items()
    ):
        target_kind, target_id = target_key
        material = existing_materials.pop(
            target_key,
            None,
        )

        if material is None:
            material = TimeTaskExpectedMaterial()
            item.expected_materials.append(
                material
            )

        if target_kind == "type":
            material.item = None
            material.item_type = values["target"]
        else:
            material.item_type = None
            material.item = values["target"]

        material.quantity = values["quantity"]
        material.required = values["required"]
        material.note = values["note"]

    for material in existing_materials.values():
        item.expected_materials.remove(
            material
        )


def save_appointment_window_form(
    item: AppointmentWindow,
) -> None:
    code = (
        request.form.get(
            "code",
            "",
        )
        .strip()
        .upper()
        .replace(" ", "-")
    )

    name = request.form.get(
        "name",
        "",
    ).strip()

    raw_start_time = request.form.get(
        "start_time",
        "",
    ).strip()

    raw_end_time = request.form.get(
        "end_time",
        "",
    ).strip()

    if not code:
        raise ValueError(
            "Der Code darf nicht leer sein."
        )

    if not name:
        raise ValueError(
            "Der Name darf nicht leer sein."
        )

    try:
        start_time = time.fromisoformat(
            raw_start_time
        )
        end_time = time.fromisoformat(
            raw_end_time
        )
    except ValueError as exc:
        raise ValueError(
            "Die Uhrzeiten sind ungültig."
        ) from exc

    if start_time >= end_time:
        raise ValueError(
            "Die Endzeit muss nach der Startzeit liegen."
        )

    duplicate_query = (
        AppointmentWindow.query
        .filter(
            AppointmentWindow.code == code
        )
    )

    if item.id is not None:
        duplicate_query = duplicate_query.filter(
            AppointmentWindow.id != item.id
        )

    if duplicate_query.first() is not None:
        raise ValueError(
            "Dieser Code ist bereits vorhanden."
        )

    try:
        sort_order = int(
            request.form.get(
                "sort_order",
                0,
            )
            or 0
        )
    except ValueError as exc:
        raise ValueError(
            "Die Sortierung ist ungültig."
        ) from exc

    item.code = code
    item.name = name
    item.start_time = start_time
    item.end_time = end_time
    item.sort_order = sort_order
    item.active = (
        "active" in request.form
    )


@admin_bp.route(
    "/admin/appointment-windows",
    methods=["GET", "POST"],
)
@foreman_required
def appointment_windows() -> Any:
    if request.method == "POST":
        item = AppointmentWindow()

        try:
            save_appointment_window_form(
                item
            )

            db.session.add(item)
            db.session.flush()

            audit(
                "appointment.window.create",
                "appointment_window",
                item.id,
            )

            db.session.commit()

            flash(
                "Zeitfenster gespeichert.",
                "success",
            )

        except ValueError as exc:
            db.session.rollback()

            flash(
                str(exc),
                "danger",
            )

        return redirect(
            url_for(
                "admin.appointment_windows"
            )
        )

    items = (
        AppointmentWindow.query
        .order_by(
            AppointmentWindow.sort_order.asc(),
            AppointmentWindow.start_time.asc(),
            AppointmentWindow.name.asc(),
        )
        .all()
    )

    return render_template(
        "admin/appointment_windows.html",
        items=items,
    )


@admin_bp.post(
    "/admin/appointment-windows/<int:item_id>"
)
@foreman_required
def appointment_window_update(
    item_id: int,
) -> Any:
    item = db.get_or_404(
        AppointmentWindow,
        item_id,
    )

    try:
        save_appointment_window_form(
            item
        )

        audit(
            "appointment.window.update",
            "appointment_window",
            item.id,
        )

        db.session.commit()

        flash(
            "Zeitfenster aktualisiert.",
            "success",
        )

    except ValueError as exc:
        db.session.rollback()

        flash(
            str(exc),
            "danger",
        )

    return redirect(
        url_for(
            "admin.appointment_windows"
        )
    )


def inventory_location_from_form(
    field_name: str,
    label: str,
) -> InventoryLocation:
    raw_location_id = request.form.get(
        field_name,
        "",
    ).strip()

    if not raw_location_id:
        raise ValueError(
            f"{label} fehlt."
        )

    try:
        location_id = int(raw_location_id)
    except ValueError as exc:
        raise ValueError(
            f"{label} ist ungültig."
        ) from exc

    return get_inventory_location(
        location_id
    )


def apply_inventory_movement(
    item: InventoryItem,
    movement: InventoryMovement,
) -> None:
    movement_type = movement.movement_type

    allowed_types = {
        InventoryMovementType.WITHDRAWAL.value,
        InventoryMovementType.STORED.value,
        InventoryMovementType.ORDERED.value,
        InventoryMovementType.CORRECTION.value,
        InventoryMovementType.TRANSFER.value,
    }

    if movement_type not in allowed_types:
        raise ValueError(
            "Die Buchungsart ist ungültig."
        )

    if movement_type == InventoryMovementType.WITHDRAWAL.value:
        movement.source_location = (
            inventory_location_from_form(
                "source_location_id",
                "Der Quell-Lagerort",
            )
        )

    elif movement_type == InventoryMovementType.TRANSFER.value:
        movement.source_location = (
            inventory_location_from_form(
                "source_location_id",
                "Der Quell-Lagerort",
            )
        )
        movement.target_location = (
            inventory_location_from_form(
                "target_location_id",
                "Der Ziel-Lagerort",
            )
        )

    else:
        movement.target_location = (
            inventory_location_from_form(
                "target_location_id",
                "Der Ziel-Lagerort",
            )
        )

    book_inventory_movement(movement)


def read_commission_form() -> dict[str, Any]:
    position_texts = request.form.getlist(
        "position_text"
    )
    service_catalog_ids = request.form.getlist(
        "position_service_catalog_id"
    )
    position_units = request.form.getlist(
        "position_units"
    )
    position_internal_descriptions = (
        request.form.getlist(
            "position_internal_description"
        )
    )

    position_count = max(
        len(position_texts),
        len(service_catalog_ids),
        len(position_units),
        len(position_internal_descriptions),
        1,
    )

    article_numbers = request.form.getlist("position_article_number")
    positions = []

    for position_index in range(
        position_count
    ):
        commission_text = list_value(
            position_texts,
            position_index,
        )
        service_catalog_id = list_value(
            service_catalog_ids,
            position_index,
        )
        units = (
            list_value(
                position_units,
                position_index,
            )
            or "1"
        )
        internal_description = list_value(
            position_internal_descriptions,
            position_index,
        )

        if not any(
            [
                commission_text,
                service_catalog_id,
            ]
        ):
            continue

        positions.append(
            {
                "article_number": list_value(article_numbers, position_index) if article_numbers else None,
                "commission_text": (
                    commission_text
                ),
                "internal_description": (
                    internal_description
                ),
                "description": (
                    internal_description
                    or commission_text
                ),
                "units": units,
                "service_catalog_id": (
                    service_catalog_id
                ),
            }
        )

    return {
        "object_type": request.form.get(
            "object_type"
        ),
        "object_id": request.form.get(
            "object_id"
        ),
        "commission_type_id": request.form.get(
            "commission_type_id"
        ),
        "craft_activity_id": request.form.get(
            "craft_activity_id"
        ),
        "crafts_process_id": request.form.get(
            "crafts_process_id"
        ),
        "creditor_id": request.form.get(
            "creditor_id"
        ),
        "department_id": request.form.get(
            "department_id"
        ),
        "responsible_official_repair_id": (
            request.form.get(
                "responsible_official_repair_id"
            )
        ),
        "use_unit_idnum": request.form.get(
            "use_unit_idnum"
        ),
        "building_idnum": request.form.get(
            "building_idnum"
        ),
        "license_agreement_idnum": (
            request.form.get(
                "license_agreement_idnum"
            )
        ),
        "title": request.form.get(
            "title"
        ),
        "damage_location": request.form.get(
            "damage_location"
        ),
        "contact_person": request.form.get(
            "contact_person"
        ),
        "reporter": request.form.get("reporter"),
        "reporter_agreement_id": request.form.get("reporter_agreement_id"),
        "reporter_contractor_id": request.form.get("reporter_contractor_id"),
        "other_affected": request.form.get("other_affected"),
        "execution_from": request.form.get(
            "execution_from"
        ),
        "execution_to": request.form.get(
            "execution_to"
        ),
        "positions": positions,
        "assignment_type": request.form.get(
            "assignment_type",
            "internal",
        ),
        "external_craftsman_id": (
            request.form.get(
                "external_craftsman_id"
            )
        ),
        "craftsman_user_id": request.form.get(
            "craftsman_user_id"
        ),
        "company_code_erp_id": request.form.get(
            "company_code_erp_id"
        ),
        "priority": request.form.get(
            "priority",
            "normal",
        ),
        "notify_tenants": request.form.get("notify_tenants"),
        "notify_on_completion": request.form.get("notify_on_completion") == "true",
        "is_retrospective": request.form.get("is_retrospective", "false"),
    }


def list_value(
    values: list[str],
    index: int,
) -> str:
    if index >= len(values):
        return ""

    return values[index].strip()


def required_form_int(
    value: str | None,
    label: str,
) -> int:
    if value is None or not value.strip():
        raise ValueError(
            f"{label} muss angegeben werden."
        )

    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(
            f"{label} ist ungültig."
        ) from exc


@admin_bp.post(
    "/admin/sync-health/outbox/<int:operation_id>/retry"
)
@admin_required
def sync_health_retry_outbox(
    operation_id: int,
) -> Any:
    operation = db.get_or_404(
        ErpOutboxOperation,
        operation_id,
    )

    if (
        operation.state
        != ErpOutboxState.FAILED.value
    ):
        flash(
            "Die ERP-Operation ist nicht fehlgeschlagen.",
            "warning",
        )
        return redirect(
            url_for("admin.sync_health")
        )

    operation.state = (
        ErpOutboxState.PENDING.value
    )
    operation.available_at = utc_now()
    operation.locked_at = None
    operation.last_error = None

    commission = operation.commission
    if not commission.has_pending_cancellation or operation.operation_type == ErpOperationType.CANCEL_COMMISSION_FROM_OFFICE.value:
        commission.erp_sync_state = CommissionSyncState.PENDING.value
        commission.erp_sync_message = None

    audit(
        "erp.outbox.retry",
        "commission",
        operation.commission_id,
        {
            "operation_id": operation.id,
            "operation_type": (
                operation.operation_type
            ),
            "attempt_count": (
                operation.attempt_count
            ),
        },
    )

    db.session.commit()

    flash(
        "Die ERP-Operation wurde erneut eingeplant.",
        "success",
    )

    return redirect(
        url_for("admin.sync_health")
    )


@admin_bp.post(
    "/admin/sync-health/webhooks/<int:event_id>/retry"
)
@admin_required
def sync_health_retry_webhook(
    event_id: int,
) -> Any:
    event = db.get_or_404(
        WebhookEvent,
        event_id,
    )

    if event.processed:
        flash(
            "Der Webhook wurde bereits verarbeitet.",
            "warning",
        )
        return redirect(
            url_for("admin.sync_health")
        )

    if event.processing_at is not None:
        flash(
            "Der Webhook wird momentan verarbeitet.",
            "warning",
        )
        return redirect(
            url_for("admin.sync_health")
        )

    event.next_attempt_at = utc_now()
    event.processing_at = None
    event.message = None

    audit(
        "webhook.retry",
        "webhook_event",
        event.id,
        {
            "entity_type": event.entity_type,
            "entity_id": event.entity_id,
            "attempt_count": event.attempt_count,
        },
    )

    db.session.commit()

    flash(
        "Der Webhook wurde erneut eingeplant.",
        "success",
    )

    return redirect(
        url_for("admin.sync_health")
    )


def _store_invoice_archive_failure(
    invoice_id: int,
    error_message: str,
) -> None:
    invoice = db.session.get(
        Invoice,
        invoice_id,
    )

    if invoice is None:
        return

    if (
        invoice.dms_archive_status
        != InvoiceArchiveStatus.ARCHIVED.value
    ):
        invoice.dms_archive_status = (
            InvoiceArchiveStatus.FAILED.value
        )
        invoice.dms_archive_attempted_at = (
            utc_now()
        )
        invoice.dms_archive_error = (
            error_message[:4000]
        )

    audit(
        "invoice.archive_failed",
        "invoice",
        invoice.id,
        {
            "invoice_number": (
                invoice.invoice_number
            ),
            "error": error_message[:4000],
        },
    )

    db.session.commit()


@admin_bp.post(
    "/admin/invoices/<int:invoice_id>/archive"
)
@login_required
@invoice_required
def invoice_archive(invoice_id: int) -> Any:
    from .dms_client import DmsError
    from .dms_service import retry_latest_document

    db.get_or_404(Invoice, invoice_id)
    try:
        document = retry_latest_document("invoice", invoice_id)
        db.session.commit()
        flash(
            "Bereits archiviert."
            if document.status == "archived"
            else "Archivierungsauftrag vorgemerkt.",
            "success",
        )
    except DmsError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return redirect(
        url_for("admin.invoice_edit", invoice_id=invoice_id)
    )


@admin_bp.post(
    "/admin/invoices/<int:invoice_id>/cancel"
)
@login_required
@invoice_required
def invoice_cancel(
    invoice_id: int,
) -> Any:
    original_invoice = db.session.execute(
        select(Invoice)
        .where(
            Invoice.id == invoice_id
        )
        .with_for_update()
    ).scalar_one_or_none()

    if original_invoice is None:
        abort(404)

    from .invoice_dms_dispatch import existing_dispatch

    dispatch = existing_dispatch(original_invoice.id, lock=True)
    if dispatch and dispatch.status == "processing":
        flash("Die DMS-Übergabe läuft gerade. Bitte versuchen Sie es nach Abschluss der Übertragung erneut.", "warning")
        return redirect(url_for("admin.invoice_edit", invoice_id=invoice_id))

    document_result = None
    cancellation_invoice = None

    try:
        (
            cancellation_invoice,
            document_result,
        ) = create_cancellation_invoice(
            original_invoice,
            request.form.get(
                "reason",
                "",
            ),
            current_user,
        )
        db.session.commit()
    except IntegrityError:
        db.session.rollback()

        if document_result is not None:
            remove_invoice_documents(
                document_result
            )

        current_app.logger.exception(
            "invoice_cancellation_conflict "
            "invoice_id=%s",
            invoice_id,
        )
        flash(
            "Die Rechnung wurde gleichzeitig "
            "bereits storniert. Bitte laden Sie "
            "die Seite neu.",
            "danger",
        )
    except (
        InvoiceFinalizationError,
        InvoiceDocumentError,
    ) as exc:
        db.session.rollback()

        if document_result is not None:
            remove_invoice_documents(
                document_result
            )

        flash(
            str(exc),
            "danger",
        )
    except Exception:
        db.session.rollback()

        if document_result is not None:
            remove_invoice_documents(
                document_result
            )

        current_app.logger.exception(
            "invoice_cancellation_failed "
            "invoice_id=%s",
            invoice_id,
        )
        flash(
            "Die Stornorechnung konnte nicht "
            "erstellt werden.",
            "danger",
        )
    else:
        flash(
            "Die Rechnung wurde storniert. "
            "Die Stornorechnung "
            f"{cancellation_invoice.invoice_number} "
            "wurde erstellt.",
            "success",
        )

        return redirect(
            url_for(
                "admin.invoice_edit",
                invoice_id=(
                    cancellation_invoice.id
                ),
            )
        )

    return redirect(
        url_for(
            "admin.invoice_edit",
            invoice_id=invoice_id,
        )
    )


@admin_bp.get("/admin/external-craftsmen")
@admin_required
def external_craftsmen() -> Any:
    items = (
        ExternalCraftsman.query
        .options(selectinload(ExternalCraftsman.craft_activities))
        .order_by(
            ExternalCraftsman.display_name.asc(),
            ExternalCraftsman.craftsman_erp_id.asc(),
        )
        .all()
    )
    internal_craftsman_erp_ids = {
        row[0]
        for row in db.session.query(
            User.craftsman_erp_id
        )
        .filter(
            User.craftsman_erp_id.is_not(None),
            User.is_active_flag.is_(True),
        )
        .all()
    }

    return render_template(
        "admin/external_craftsmen.html",
        items=items,
        portal_light_default=settings.get(SettingKeys.EXTERNAL_PORTAL_LIGHT_DEFAULT),
        internal_craftsman_erp_ids=(
            internal_craftsman_erp_ids
        ),
        craft_activities=CraftActivity.query.order_by(
            CraftActivity.name.asc(),
            CraftActivity.id.asc(),
        ).all(),
    )


@admin_bp.post(
    "/admin/external-craftsmen/sync"
)
@admin_required
def external_craftsmen_sync() -> Any:
    try:
        service = WowiportService()
        sync_craft_activities_from_wowiport(
            service
        )
        db.session.flush()
        rows = service.get_craftsmen_for_user_sync()
        result = (
            sync_external_craftsmen_from_wowiport(
                rows
            )
        )
        audit(
            "external_craftsman.sync",
            "external_craftsman",
            metadata={
                "seen": result.seen,
                "created": result.created,
                "updated": result.updated,
                "deactivated": result.deactivated,
            },
        )
        db.session.commit()
        flash(
            (
                f"{result.seen} Handwerker geprüft, "
                f"{result.created} neu, "
                f"{result.updated} aktualisiert und "
                f"{result.deactivated} deaktiviert."
            ),
            "success",
        )
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception(
            "external_craftsman_sync_failed"
        )
        flash(
            f"Wowiport-Aktualisierung fehlgeschlagen: {exc}",
            "danger",
        )

    return redirect(
        url_for("admin.external_craftsmen")
    )


@admin_bp.post(
    "/admin/external-craftsmen/<int:craftsman_id>/craft-activities"
)
@admin_required
def external_craftsman_craft_activities(craftsman_id: int) -> Any:
    craftsman = db.get_or_404(ExternalCraftsman, craftsman_id)

    try:
        try:
            selected_ids = {
                int(value)
                for value in request.form.getlist("craft_activity_ids")
            }
        except (TypeError, ValueError) as exc:
            raise ValueError("Die Gewerkeauswahl ist ungültig.") from exc

        if any(activity_id <= 0 for activity_id in selected_ids):
            raise ValueError("Die Gewerkeauswahl ist ungültig.")

        activities = (
            CraftActivity.query
            .filter(CraftActivity.id.in_(sorted(selected_ids)))
            .order_by(CraftActivity.id.asc())
            .all()
        ) if selected_ids else []

        if len(activities) != len(selected_ids):
            raise ValueError("Mindestens ein Gewerk wurde nicht gefunden.")

        previous_ids = sorted(
            activity.id for activity in craftsman.craft_activities
        )
        craftsman.craft_activities = activities
        audit(
            "external_craftsman.craft_activities.update",
            "external_craftsman",
            craftsman.id,
            metadata={
                "previous_craft_activity_ids": previous_ids,
                "craft_activity_ids": sorted(selected_ids),
                "source": "web",
            },
        )
        db.session.commit()
        flash("Die Gewerke wurden gespeichert.", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "external_craftsman_craft_activities_failed"
        )
        flash("Die Gewerke konnten nicht gespeichert werden.", "danger")

    return redirect(url_for("admin.external_craftsmen"))


@admin_bp.post("/admin/external-craftsmen/<int:craftsman_id>/communication")
@admin_required
def external_craftsman_communication(craftsman_id: int) -> Any:
    craftsman = db.get_or_404(ExternalCraftsman, craftsman_id)
    try:
        values = validate_external_communication(request.form)
        previous = {name: getattr(craftsman, name) for name in values}
        for name, value in values.items():
            setattr(craftsman, name, value)
        audit("external_craftsman.communication.update", "external_craftsman", craftsman.id,
              {"previous": previous, "current": values})
        db.session.commit()
        flash("Die Kommunikationseinstellungen wurden gespeichert.", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return redirect(url_for("admin.external_craftsmen"))


@admin_bp.post("/admin/external-craftsmen/<int:craftsman_id>/assignment")
@admin_required
def external_craftsman_assignment(craftsman_id: int) -> Any:
    craftsman = db.get_or_404(ExternalCraftsman, craftsman_id)
    action = request.form.get("action")
    target_url = url_for("admin.external_craftsmen")

    if action not in {"enable", "disable"}:
        flash("Die Aktion ist ungültig.", "danger")
        return redirect(target_url)

    enabled = action == "enable"
    if enabled:
        if not craftsman.wowi_active:
            flash("Der Handwerker ist in Wowiport nicht aktiv.", "danger")
            return redirect(target_url)
        internal_user = User.query.filter_by(
            craftsman_erp_id=craftsman.craftsman_erp_id,
            is_active_flag=True,
        ).first()
        if internal_user is not None:
            flash("Für diesen Handwerker besteht bereits ein interner Benutzerzugang.", "danger")
            return redirect(target_url)

    craftsman.assignment_enabled = enabled
    audit(
        "external_craftsman.assignment.enable" if enabled else "external_craftsman.assignment.disable",
        "external_craftsman",
        craftsman.id,
        metadata={"assignment_enabled": enabled},
    )
    db.session.commit()
    flash(
        "Der Handwerker wurde zur Whitelist hinzugefügt."
        if enabled else "Der Handwerker wurde von der Whitelist entfernt.",
        "success",
    )
    return redirect(target_url)


@admin_bp.post(
    "/admin/external-craftsmen/"
    "<int:craftsman_id>/enable"
)
@admin_required
def external_craftsman_enable(
    craftsman_id: int,
) -> Any:
    craftsman = db.get_or_404(
        ExternalCraftsman,
        craftsman_id,
    )

    if not craftsman.wowi_active:
        flash(
            "Der Handwerker ist in Wowiport nicht aktiv.",
            "danger",
        )
        return redirect(
            url_for("admin.external_craftsmen")
        )

    if not craftsman.email:
        flash(
            "Für den Handwerker ist keine E-Mail-Adresse hinterlegt.",
            "danger",
        )
        return redirect(
            url_for("admin.external_craftsmen")
        )

    internal_user = User.query.filter_by(
        craftsman_erp_id=(
            craftsman.craftsman_erp_id
        ),
        is_active_flag=True,
    ).first()

    if internal_user is not None:
        flash(
            "Für diesen Handwerker besteht bereits ein interner Benutzerzugang.",
            "danger",
        )
        return redirect(
            url_for("admin.external_craftsmen")
        )

    craftsman.portal_enabled = True
    craftsman.username = (
        craftsman.username
        or default_external_username(craftsman)
    ).casefold()
    raw_token = create_invitation_token(
        craftsman,
        current_user.id,
    )
    audit(
        "external_craftsman.enable",
        "external_craftsman",
        craftsman.id,
    )
    db.session.commit()

    try:
        send_external_invitation_email(
            craftsman,
            raw_token,
        )
        craftsman.invited_at = utc_now()
        db.session.commit()
        flash(
            "Der Zugang wurde freigeschaltet und die Einladung versendet.",
            "success",
        )
    except Exception as exc:
        current_app.logger.exception(
            "external_craftsman_invitation_failed "
            "craftsman_id=%s",
            craftsman.id,
        )
        flash(
            (
                "Der Zugang wurde freigeschaltet, die Einladung "
                f"konnte aber nicht versendet werden: {exc}"
            ),
            "danger",
        )

    return redirect(
        url_for("admin.external_craftsmen")
    )


@admin_bp.post(
    "/admin/external-craftsmen/"
    "<int:craftsman_id>/invite"
)
@admin_required
def external_craftsman_invite(
    craftsman_id: int,
) -> Any:
    craftsman = db.get_or_404(
        ExternalCraftsman,
        craftsman_id,
    )

    if (
        not craftsman.portal_enabled
        or not craftsman.wowi_active
        or not craftsman.email
    ):
        flash(
            "Für diesen Handwerker kann keine Einladung versendet werden.",
            "danger",
        )
        return redirect(
            url_for("admin.external_craftsmen")
        )

    raw_token = create_invitation_token(
        craftsman,
        current_user.id,
    )
    db.session.commit()

    try:
        send_external_invitation_email(
            craftsman,
            raw_token,
        )
        craftsman.invited_at = utc_now()
        audit(
            "external_craftsman.invite",
            "external_craftsman",
            craftsman.id,
        )
        db.session.commit()
        flash(
            "Die Einladung wurde versendet.",
            "success",
        )
    except Exception as exc:
        current_app.logger.exception(
            "external_craftsman_invitation_failed "
            "craftsman_id=%s",
            craftsman.id,
        )
        flash(
            f"Die Einladung konnte nicht versendet werden: {exc}",
            "danger",
        )

    return redirect(
        url_for("admin.external_craftsmen")
    )


@admin_bp.post(
    "/admin/external-craftsmen/"
    "<int:craftsman_id>/disable"
)
@admin_required
def external_craftsman_disable(
    craftsman_id: int,
) -> Any:
    craftsman = db.get_or_404(
        ExternalCraftsman,
        craftsman_id,
    )
    now = utc_now()
    craftsman.portal_enabled = False
    craftsman.set_password(None)
    craftsman.failed_login_count = 0
    craftsman.locked_until = None

    ExternalCraftsmanAccessToken.query.filter(
        ExternalCraftsmanAccessToken.external_craftsman_id
        == craftsman.id,
        ExternalCraftsmanAccessToken.used_at.is_(None),
    ).update(
        {
            ExternalCraftsmanAccessToken.used_at: now,
        },
        synchronize_session=False,
    )

    audit(
        "external_craftsman.disable",
        "external_craftsman",
        craftsman.id,
    )
    db.session.commit()
    flash(
        "Der Portalzugang wurde gesperrt.",
        "success",
    )
    return redirect(
        url_for("admin.external_craftsmen")
    )


