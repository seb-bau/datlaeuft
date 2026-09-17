from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path

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
    foreman_required,
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
    search_commission_objects,
    sync_object_directory,
)
from sqlalchemy import (
    and_,
    or_,
    select,
)
from sqlalchemy.exc import IntegrityError
from .commission_service import create_pending_commission
from .models import (
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
)
from .ticket_service import (
    analyze_ticket_mirror,
    ticket_license_agreement_local_id,
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
    available_invoice_commissions,
    create_commission_invoice_draft,
    create_free_invoice_draft,
    replace_invoice_positions,
    InvoiceFinalizationError,
    finalize_invoice,
    create_cancellation_invoice,
)
from .invoice_document import (
    InvoiceDocumentError,
    create_invoice_document,
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
    reject_offer,
    release_offer,
    replace_offer_positions,
)
from datetime import date, time
from uuid import uuid4

from .commission_priority import set_manual_commission_priority

from .process_info_service import (
    create_outgoing_info,
    sync_process_infos,
    write_outgoing_info,
)

admin_bp = Blueprint("admin", __name__)
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
        Commission.local_status.in_([
            CommissionLocalStatus.COMPLETION_REVIEW.value,
            CommissionLocalStatus.TENANT_CHARGE_REVIEW.value,
        ]),
    )

    if current_user.is_admin:
        return query

    if current_user.is_foreman:
        return query.filter(
            or_(
                Commission.created_by_user_id
                == current_user.id,
                Commission.created_by_user_id.is_(None),
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
    open_count = Commission.query.filter(
        Commission.deleted_at.is_(None),
        Commission.local_status
        != CommissionLocalStatus.DONE.value,
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
        Commission.local_status
        == CommissionLocalStatus.COMPLETION_REVIEW.value
    ).count()

    tenant_charge_review_count = commission_reviews.filter(
        Commission.local_status
        == CommissionLocalStatus.TENANT_CHARGE_REVIEW.value
    ).count()

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
        )

    return render_template(
        "admin/dashboard.html",
        open_count=open_count,
        review_count=review_count,
        material_review_count=material_review_count,
        low_stock_count=low_stock_count,
        completion_review_count=completion_review_count,
        tenant_charge_review_count=tenant_charge_review_count,
        sync_problem_count=sync_problem_count,
    )


@admin_bp.get("/admin/sync-health")
@admin_required
def sync_health() -> Any:
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
    )


@admin_bp.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def settings_page() -> Any:
    if request.method == "POST":
        try:
            for definition in all_definitions():
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

    return render_template(
        "admin/settings.html",
        groups=grouped_definitions(),
        settings=settings,
    )


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
    item = db.get_or_404(
        WowiCompanyCode,
        item_id,
    )

    if request.method == "POST":
        try:
            save_company_code_invoice_form(
                item
            )

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
    )


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
        commission_creation_tasks()
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

    return render_template(
        "admin/ticket_settings.html",
        craft_activities=craft_activities,
        positions_by_craft=positions_by_craft,
        mappings=mappings,
        commission_types=commission_types,
        default_commission_type_id=(
            default_commission_type_id
        ),
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
def offer_archive(
    offer_id: int,
) -> Any:
    item = db.session.execute(
        select(Offer)
        .where(
            Offer.id == offer_id
        )
        .with_for_update()
    ).scalar_one_or_none()

    if item is None:
        abort(404)

    try:
        result = archive_offer(
            item,
            current_user,
        )
        db.session.commit()

    except OfferArchiveError as exc:
        db.session.rollback()

        store_offer_archive_failure(
            offer_id,
            str(exc),
        )

        current_app.logger.exception(
            "offer_archive_failed "
            "offer_id=%s",
            offer_id,
        )

        flash(
            str(exc),
            "danger",
        )

    except Exception:
        db.session.rollback()

        store_offer_archive_failure(
            offer_id,
            "Unerwarteter Fehler bei der "
            "d.velop-Archivierung.",
        )

        current_app.logger.exception(
            "offer_archive_failed "
            "offer_id=%s",
            offer_id,
        )

        flash(
            "Das Angebot konnte nicht nach "
            "d.velop übertragen werden.",
            "danger",
        )

    else:
        if result.already_archived:
            flash(
                "Das Angebot war bereits in "
                "d.velop archiviert. Die lokale "
                "Zuordnung wurde aktualisiert.",
                "success",
            )
        else:
            flash(
                "Das Angebot wurde erfolgreich "
                "in d.velop archiviert.",
                "success",
            )

    return redirect(
        url_for(
            "admin.offer_edit",
            offer_id=offer_id,
        )
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
    invoice = Invoice.query.get_or_404(
        invoice_id
    )
    document_result = None

    try:
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
            "Die Rechnung konnte nicht "
            "festgeschrieben werden.",
            "danger",
        )
    else:
        flash(
            "Die Rechnung wurde als "
            f"{invoice.invoice_number} "
            "festgeschrieben. Die verknüpften "
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


@admin_bp.get(
    "/admin/invoices/<int:invoice_id>/document"
)
@login_required
@invoice_required
def invoice_document(
    invoice_id: int,
) -> Any:
    invoice = Invoice.query.get_or_404(
        invoice_id
    )

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

    return send_file(
        document_path,
        mimetype="application/pdf",
        as_attachment=False,
        download_name=(
            f"{document_label}-"
            f"{invoice.invoice_number}.pdf"
        ),
    )


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
        as_attachment=False,
        download_name=work_report_filename(
            commission
        ),
    )


@admin_bp.get(
    "/admin/commissions/"
    "<int:commission_id>/work-report"
)
@foreman_required
def commission_work_report(
    commission_id: int,
) -> Any:
    commission = db.get_or_404(
        Commission,
        commission_id,
    )
    entries = (
        commission_work_report_entries(
            commission
        )
    )

    try:
        document = create_work_report_pdf(
            commission,
            entries,
            WORK_REPORT_SCOPE_ALL,
        )
    except WorkReportDocumentError as exc:
        flash(
            str(exc),
            "danger",
        )

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
        download_name=work_report_filename(
            commission
        ),
    )


@admin_bp.post(
    "/admin/invoices/<int:invoice_id>/document/regenerate"
)
@admin_required
def invoice_document_regenerate(
    invoice_id: int,
) -> Any:
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

    if (
        invoice.dms_archive_status
        != InvoiceArchiveStatus.NOT_ARCHIVED.value
        or invoice.dms_archive_key
        or invoice.dms_document_id
        or invoice.dms_archived_at is not None
    ):
        flash(
            "Die PDF-Datei kann nach einer begonnenen "
            "oder abgeschlossenen d.velop-Archivierung "
            "nicht mehr neu erzeugt werden.",
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

    if request.method == "POST":
        enabled_ids = set()

        for value in request.form.getlist(
            "enabled_craft_activity_ids"
        ):
            try:
                enabled_ids.add(int(value))
            except ValueError:
                continue

        for item in items:
            item.app_enabled = (
                item.active
                and item.id in enabled_ids
            )

        enabled_count = sum(
            1
            for item in items
            if item.app_enabled
        )

        audit(
            "craft_activities.app_access.update",
            metadata={
                "enabled_count": enabled_count,
                "total_count": len(items),
            },
        )
        db.session.commit()

        flash(
            f"{enabled_count} Gewerke sind "
            "für die Anwendung freigegeben.",
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

    if request.method == "POST":
        departments_by_id = {
            item.id: item
            for item in departments
        }
        selected_departments = {}

        for craft_activity in craft_activities:
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
        "Nutzungseinheiten.",
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
    return render_template("admin/users.html", items=items)


@admin_bp.route("/admin/users/import-entra", methods=["POST"])
@admin_required
def import_users_from_entra() -> Any:
    result = import_entra_users()
    audit("users.import_entra", metadata={"created": result.created, "updated": result.updated, "skipped": result.skipped})
    db.session.commit()
    flash(f"Import abgeschlossen: {result.created} neu, {result.updated} aktualisiert, {result.skipped} uebersprungen.", "success")
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
        db.session.add(item)
        save_user_form(item)
        save_user_craft_activities(item)
        db.session.flush()
        audit("user.create", "user", item.id)
        db.session.commit()
        flash("Benutzer gespeichert.", "success")
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
    )


def save_web_commission_photo(
    commission: Commission,
    upload: Any,
    caption: str | None,
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


@admin_bp.get("/admin/tickets")
@foreman_required
def tickets() -> Any:
    selected_state = request.args.get(
        "state",
        TicketMirrorState.NEW.value,
    )
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

    query = TicketMirror.query.filter(
        TicketMirror.deleted_at.is_(None)
    )

    valid_states = {
        item.value
        for item in TicketMirrorState
    }

    if selected_state in valid_states:
        query = query.filter(
            TicketMirror.state == selected_state
        )
    else:
        selected_state = "all"

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

    pagination = query.order_by(
        TicketMirror.received_at.desc(),
        TicketMirror.id.desc(),
    ).paginate(
        page=page,
        per_page=50,
        error_out=False,
    )

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

    return render_template(
        "admin/tickets.html",
        pagination=pagination,
        selected_state=selected_state,
        search_text=search_text,
        openai_configured=openai_configured,
    )


@admin_bp.post(
    "/admin/tickets/<int:ticket_id>/analyze"
)
@foreman_required
def ticket_analyze(
    ticket_id: int,
) -> Any:
    ticket = db.get_or_404(
        TicketMirror,
        ticket_id,
    )

    if (
        ticket.ai_state
        == TicketAiState.READY.value
        and ticket.ai_input_hash
        == ticket.source_signature
    ):
        return redirect(
            url_for(
                "admin.commission_create",
                ticket_id=ticket.id,
            )
        )

    if ticket.deleted_at is not None:
        abort(404)

    try:
        analyze_ticket_mirror(
            ticket,
            current_user.id,
        )
        audit(
            "ticket.ai.analyze",
            "ticket",
            ticket.erp_id,
            {
                "model": ticket.ai_model,
                "classification": (
                    ticket.ai_classification
                ),
                "craft_activity_id": (
                    ticket.ai_craft_activity_id
                ),
                "task_template_id": (
                    ticket.ai_task_template_id
                ),
            },

        )
        db.session.commit()
        return redirect(
            url_for(
                "admin.commission_create",
                ticket_id=ticket.id,
            )
        )
    except Exception as exc:
        db.session.rollback()

        failed_ticket = db.session.get(
            TicketMirror,
            ticket_id,
        )

        if failed_ticket is not None:
            failed_ticket.ai_state = (
                TicketAiState.FAILED.value
            )
            failed_ticket.ai_error = str(exc)[:2000]
            db.session.commit()

        current_app.logger.exception(
            "ticket_ai_analysis_failed ticket_id=%s",
            ticket_id,
        )
        flash(
            str(exc),
            "danger",
        )

    return redirect(
        url_for("admin.tickets")
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

    return redirect(
        url_for("admin.tickets")
    )


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

    return redirect(
        url_for("admin.tickets")
    )


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
@foreman_required
def commission_create() -> Any:
    service_positions = (
        commission_creation_tasks()
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
                url_for("admin.tickets")
            )

        if (
            source_ticket.state
            == TicketMirrorState.CONVERTED.value
        ):
            flash(
                "Aus diesem Ticket wurde bereits "
                "ein Auftrag erstellt.",
                "warning",
            )
            return redirect(
                url_for("admin.tickets")
            )

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

            if (
                source_ticket.ai_craft_activity_id
                in craft_activity_by_id
            ):
                form_values["craft_activity_id"] = (
                    source_ticket.ai_craft_activity_id
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
            commission = create_pending_commission(
                data=form_data,
                created_by_user=current_user,
                source=CommissionSource.WEB,
                allow_assignment_override=True,
            )

            db.session.flush()

            if source_ticket is not None:
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

            photo_upload = request.files.get(
                "photo"
            )

            if (
                photo_upload is not None
                and photo_upload.filename
            ):
                save_web_commission_photo(
                    commission=commission,
                    upload=photo_upload,
                    caption=request.form.get(
                        "photo_caption"
                    ),
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

            flash(
                "Der Auftrag wurde erfasst und "
                "zur Übertragung an Wowiport "
                "eingeplant.",
                "success",
            )

            return redirect(
                url_for("admin.commissions")
            )

        except ValueError as exc:
            db.session.rollback()
            flash(
                str(exc),
                "danger",
            )

    return render_template(
        "admin/commission_form.html",
        service_positions=service_positions,
        craft_activities=craft_activities,
        commission_types=commission_types,
        craftsman_users=craftsman_users,
        company_codes=company_codes,
        position_rows=position_rows,
        form_values=form_values,
        selected_object=selected_object,
        source_ticket=source_ticket,
        selected_service_position=(
            selected_service_position
        ),
        position_selection_source=(
            position_selection_source
        ),
        default_commission_type_id=(
            default_commission_type_id
        ),
    )


@admin_bp.get(
    "/admin/commissions/assignment-preview"
)
@foreman_required
def commission_assignment_preview() -> Any:
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
                WowiCompanyCode.active.is_(True),
            )
            .one_or_none()
        )

        if company_code is None:
            raise ValueError(
                "Das automatisch ermittelte Buchwerk "
                "ist nicht verfügbar."
            )

    except ValueError as exc:
        return jsonify(
            {
                "message": str(exc),
            }
        ), 400

    payload = {
        "company_code_erp_id": company_code.erp_id,
        "company_code_code": company_code.code,
        "company_code_name": company_code.name,
    }

    if craft_activity_id is None:
        return jsonify(payload)

    try:
        service_position = (
            commission_creation_tasks()
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

        assignment = resolve_commission_assignment(
            [service_position],
            object_context,
        )

    except ValueError as exc:
        payload["message"] = str(exc)
        return jsonify(payload), 400

    payload.update(
        {
            "craftsman_user_id": assignment.user.id,
            "craftsman_erp_id": (
                assignment.user.craftsman_erp_id
            ),
            "craftsman_name": (
                assignment.user.display_name
            ),
        }
    )
    return jsonify(payload)


@admin_bp.get(
    "/admin/commission-objects/search"
)
@foreman_required
def commission_object_search() -> Any:
    search_text = request.args.get(
        "q",
        "",
    ).strip()

    items = search_commission_objects(
        search_text,
        limit=30,
    )

    return jsonify(
        {
            "items": items,
        }
    )


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
    )


@admin_bp.route(
    "/admin/offers/new",
    methods=["GET", "POST"],
)
@offer_required
def offer_create() -> Any:
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
    item = db.get_or_404(
        Offer,
        offer_id,
    )

    try:
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


@admin_bp.post(
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
        commission = accept_offer(
            item,
            current_user,
        )

        enqueue_commission_push(
            commission=commission,
            event_type=(
                PushEventType.NEW_COMMISSION
            ),
            user=commission.user,
        )

        audit(
            "offer.accept",
            "offer",
            item.id,
            {
                "commission_id": (
                    commission.id
                ),
                "craftsman_user_id": (
                    commission.user_id
                ),
                "craftsman_erp_id": (
                    commission.craftsman_erp_id
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
                offer_id=item.id,
            )
        )

    flash(
        "Die Annahme wurde erfasst. "
        "Der Auftrag wurde angelegt und "
        "zur Übertragung an Wowiport "
        "eingeplant.",
        "success",
    )

    return redirect(
        url_for(
            "admin.offer_edit",
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
            OfferPositionSource.CATALOG.value:
                "Katalog",
            OfferPositionSource.FREE.value:
                "Freie Position",
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
    items = (
        Invoice.query
        .order_by(
            Invoice.created_at.desc(),
            Invoice.id.desc(),
        )
        .all()
    )

    status_labels = {
        InvoiceStatus.DRAFT.value:
            "Entwurf",
        InvoiceStatus.FINALIZED.value:
            "Festgeschrieben",
        InvoiceStatus.CANCELED.value:
            "Storniert",
    }

    return render_template(
        "admin/invoices.html",
        items=items,
        status_labels=status_labels,
    )


@admin_bp.route(
    "/admin/invoices/new",
    methods=["GET", "POST"],
)
@invoice_required
def invoice_create() -> Any:
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

            return render_template(
                "admin/invoice_create.html",
                company_codes=company_codes,
                commissions=commissions,
            )

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

    return render_template(
        "admin/invoice_create.html",
        company_codes=company_codes,
        commissions=commissions,
    )


@admin_bp.route(
    "/admin/invoices/<int:invoice_id>",
    methods=["GET", "POST"],
)
@invoice_required
def invoice_edit(
    invoice_id: int,
) -> Any:
    item = db.get_or_404(
        Invoice,
        invoice_id,
    )
    editable = (
        item.status
        == InvoiceStatus.DRAFT.value
    )

    if request.method == "POST":
        if not editable:
            flash(
                "Eine festgeschriebene Rechnung "
                "kann nicht geändert werden.",
                "danger",
            )

            return redirect(
                url_for(
                    "admin.invoice_edit",
                    invoice_id=item.id,
                )
            )

        try:
            save_invoice_draft_form(
                item
            )
            save_invoice_positions_form(
                item
            )

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


@admin_bp.post(
    "/admin/invoices/<int:invoice_id>/suggestions"
)
@invoice_required
def invoice_add_suggestions(
    invoice_id: int,
) -> Any:
    item = db.get_or_404(
        Invoice,
        invoice_id,
    )

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
    search_text = request.args.get(
        "q",
        "",
    ).strip()

    items = []

    for item in search_commission_objects(
        search_text,
        limit=50,
    ):
        if (
            item.get("object_type")
            != "license_agreement"
        ):
            continue

        company_code = (
            item.get("company_code")
            or {}
        )

        if (
            company_code.get("erp_id")
            != invoice.company_code_erp_id
        ):
            continue

        items.append(item)

    return jsonify(
        {
            "items": items,
        }
    )


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
    license_agreement_id = (
        required_form_int(
            request.args.get(
                "license_agreement_id"
            ),
            "Vertrag",
        )
    )

    agreement = invoice_agreement(
        invoice,
        license_agreement_id,
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
            "invoice_recipient_fetch_failed "
            "invoice_id=%s "
            "license_agreement_id=%s",
            invoice.id,
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
                    "Für den Vertrag wurden keine "
                    "Rechnungsempfänger gefunden."
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
    "/admin/invoices/<int:invoice_id>/delete"
)
@invoice_required
def invoice_delete(
    invoice_id: int,
) -> Any:
    item = db.get_or_404(
        Invoice,
        invoice_id,
    )

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
    db.session.delete(item)
    db.session.commit()

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
        .filter(
            TimeTaskTemplate.active.is_(True)
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
    has_any_work_reports = any(
        values[
            WORK_REPORT_SCOPE_ALL
        ] > 0
        for values in report_counts.values()
    )

    return {
        "item": item,
        "editable": (
            item.status
            == InvoiceStatus.DRAFT.value
        ),
        "task_templates": task_templates,
        "catalogue_items": catalogue_items,
        "position_source_labels": {
            "time": "Zeit",
            "material": "Material",
            "catalog": "Katalog",
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


def save_invoice_draft_form(
    item: Invoice,
) -> None:
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

    license_agreement_id = (
        optional_form_int(
            request.form.get(
                "recipient_license_agreement_erp_id"
            ),
            "Vertrag",
        )
    )

    if license_agreement_id is not None:
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
    else:
        item.recipient_license_agreement_erp_id = None
        item.recipient_license_agreement_idnum = None

    item.recipient_person_erp_id = (
        optional_form_int(
            request.form.get(
                "recipient_person_erp_id"
            ),
            "Person",
        )
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
    item.due_date = optional_form_date(
        request.form.get(
            "due_date"
        ),
        "Fälligkeitsdatum",
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


@admin_bp.get("/admin/commissions")
@foreman_required
def commissions() -> Any:
    view_mode = request.args.get(
        "view",
        "active",
    ).strip()

    query = Commission.query

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
    elif view_mode == "all":
        pass
    else:
        view_mode = "active"
        query = query.filter(
            Commission.deleted_at.is_(None),
            Commission.local_status.notin_([
                CommissionLocalStatus.DONE.value,
                CommissionLocalStatus.DELETED.value,
            ]),
        )

    items = query.order_by(
        Commission.synced_at.desc()
    ).all()

    return render_template(
        "admin/commissions.html",
        items=items,
        view_mode=view_mode,
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
        url_for("admin.commission_detail", commission_id=item.id)
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
                ProcessInfo.state != "deleted",
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
        "waiting_for_acceptance": "Warte auf Annahme",
        "open": "Beauftragt",
        "in_progress": "In Bearbeitung",
        "completion_review": "Nicht abgeschlossen",
        "tenant_charge_review": "Mieterbelastung",
        "done": "Erledigt",
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
        "commission.create.web": "Auftrag im Web erfasst",
        "commission.create.ios": "Auftrag in der App erfasst",
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
        "commission.priority.web": "Priorität im Web geändert",
        "commission.priority.wowiport": "Ticketpriorität aus Wowiport übernommen",
    }

    return render_template(
        "admin/commission_detail.html",
        item=item,
        photos=photos,
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
    )


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
        information = create_outgoing_info(
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
        url_for(
            "admin.commission_detail",
            commission_id=commission.id,
        )
        + "#zusatzinformationen"
    )


@admin_bp.get("/admin/commission-reviews")
@foreman_required
def commission_reviews() -> Any:
    selected_status = request.args.get(
        "status",
        "",
    ).strip()

    status_labels = {
        CommissionLocalStatus.COMPLETION_REVIEW.value:
            "Nicht abgeschlossen",
        CommissionLocalStatus.TENANT_CHARGE_REVIEW.value:
            "Mieterbelastung",
    }

    query = commission_review_query()

    if selected_status in status_labels:
        query = query.filter(
            Commission.local_status
            == selected_status
        )
    else:
        selected_status = ""

    items = query.order_by(
        Commission.completion_submitted_at.desc(),
        Commission.id.desc(),
    ).all()

    return render_template(
        "admin/commission_reviews.html",
        items=items,
        selected_status=selected_status,
        status_labels=status_labels,
    )


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


@admin_bp.post(
    "/admin/commissions/<int:commission_id>/photos"
)
@foreman_required
def commission_photo_upload(
    commission_id: int,
) -> Any:
    commission = db.get_or_404(
        Commission,
        commission_id,
    )

    upload = request.files.get("photo")

    if upload is None or not upload.filename:
        flash(
            "Bitte ein Foto auswählen.",
            "danger",
        )
        return redirect(
            url_for(
                "admin.commission_detail",
                commission_id=commission.id,
            )
        )

    try:
        photo, duplicate = save_web_commission_photo(
            commission=commission,
            upload=upload,
            caption=request.form.get(
                "photo_caption"
            ),
        )

        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    else:
        if duplicate:
            flash(
                "Dieses Foto ist bereits vorhanden.",
                "warning",
            )
        elif commission.erp_id is None:
            flash(
                "Das Foto wurde gespeichert und wird nach "
                "der Wowiport-Bestätigung übertragen.",
                "success",
            )
        else:
            flash(
                "Das Foto wurde gespeichert und zur "
                "Übertragung an Wowiport eingeplant.",
                "success",
            )

    return redirect(
        url_for(
            "admin.commission_detail",
            commission_id=commission.id,
        )
    )


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


@admin_bp.route("/admin/inventory", methods=["GET", "POST"])
@foreman_required
def inventory() -> Any:
    if request.method == "POST":
        item = InventoryItem()
        save_inventory_form(item)
        db.session.add(item)
        db.session.flush()
        audit("inventory.create", "inventory_item", item.id)
        db.session.commit()
        flash("Artikel gespeichert.", "success")
        return redirect(url_for("admin.inventory"))
    items = InventoryItem.query.order_by(InventoryItem.name.asc()).all()
    return render_template("admin/inventory.html", items=items)


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
        if request.form.get("form_name") == "movement":
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
                        "movement_type": movement.movement_type,
                        "quantity": movement.quantity,
                        "source_location_id": (
                            movement.source_location_id
                        ),
                        "target_location_id": (
                            movement.target_location_id
                        ),
                    },
                )

            except (TypeError, ValueError) as exc:
                db.session.rollback()

                flash(
                    str(exc),
                    "danger",
                )

                return redirect(
                    url_for(
                        "admin.inventory_item",
                        item_id=item.id,
                    )
                )

        else:
            save_inventory_form(item)

            audit(
                "inventory.update",
                "inventory_item",
                item.id,
            )

        db.session.commit()
        flash(
            "Artikel gespeichert.",
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
        .filter_by(item_id=item.id)
        .order_by(
            InventoryMovement.occurred_at.desc()
        )
        .all()
    )

    barcodes = (
        InventoryItemBarcode.query
        .filter_by(item_id=item.id)
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
        InventoryMovementType.WITHDRAWAL.value: "Entnahme",
        InventoryMovementType.STORED.value: "Eingelagert",
        InventoryMovementType.ORDERED.value: "Bestellt",
        InventoryMovementType.CORRECTION.value: "Korrektur",
        InventoryMovementType.CANCELLATION.value: "Storno",
        InventoryMovementType.TRANSFER.value: "Umbuchung",
    }

    return render_template(
        "admin/inventory_item.html",
        item=item,
        movements=movements,
        movement_types=InventoryMovementType,
        barcodes=barcodes,
        barcode_kinds=barcode_kinds,
        locations=locations,
        location_stocks=location_stocks,
        movement_type_labels=movement_type_labels,
    )


@admin_bp.post(
    "/admin/inventory/<int:item_id>/barcodes"
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
                "admin.inventory_item",
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
                "admin.inventory_item",
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
            "admin.inventory_item",
            item_id=item.id,
        )
    )


@admin_bp.post(
    "/admin/inventory/<int:item_id>/barcodes/<int:barcode_id>"
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
                "admin.inventory_item",
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
                "admin.inventory_item",
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
            "admin.inventory_item",
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

    if request.method == "POST":
        save_time_template_form(item)
        save_expected_materials_form(item)
        audit("service_position.update", "time_task_template", item.id)
        db.session.commit()
        flash("Leistungsposition gespeichert.", "success")
        return redirect(url_for("admin.time_templates"))

    return render_template("admin/time_template_form.html", item=item, craft_activities=craft_activities, inventory_items=inventory_items)


@admin_bp.get("/admin/time-entries")
@foreman_required
def time_entries() -> Any:
    status_filter = request.args.get("status", "needs_review").strip()
    review_level_filter = request.args.get("review_level", "").strip()
    search_text = request.args.get("q", "").strip()
    query = TimeEntry.query.join(Commission, TimeEntry.commission_id == Commission.id).join(User, TimeEntry.user_id == User.id).outerjoin(TimeTaskTemplate, TimeEntry.task_template_id == TimeTaskTemplate.id)
    if status_filter and status_filter != "all":
        query = query.filter(TimeEntry.review_status == status_filter)
    if review_level_filter:
        query = query.filter(TimeEntry.review_level == review_level_filter)
    if search_text:
        for term in [item.strip() for item in search_text.split() if item.strip()]:
            like_term = f"%{term}%"
            query = query.filter(or_(Commission.code.ilike(like_term), Commission.erp_idnum.ilike(like_term), Commission.title.ilike(like_term), TimeTaskTemplate.code.ilike(like_term), TimeTaskTemplate.title.ilike(like_term), User.display_name.ilike(like_term)))
    items = query.order_by(
        TimeEntry.service_date.desc(),
        TimeEntry.created_at.desc(),
        TimeEntry.id.desc(),
    ).all()
    return render_template("admin/time_entries.html", items=items, status_filter=status_filter, review_level_filter=review_level_filter, search_text=search_text)


@admin_bp.post("/admin/time-entries/<int:item_id>/<string:decision>")
@foreman_required
def time_entry_review(item_id: int, decision: str) -> Any:
    item = db.get_or_404(TimeEntry, item_id)
    if decision == "approve":
        item.review_status = TimeReviewStatus.APPROVED.value
    elif decision == "reject":
        item.review_status = TimeReviewStatus.REJECTED.value
    item.reviewed_by_user_id = current_user.id
    item.reviewed_at = utc_now()
    audit("time.review", "time_entry", item.id, {"decision": decision})
    db.session.commit()
    flash("Stundenzettel aktualisiert.", "success")
    return redirect(url_for("admin.time_entries"))


@admin_bp.get("/admin/material-movements")
@foreman_required
def material_movements() -> Any:
    status_filter = request.args.get("status", "needs_review").strip()
    review_level_filter = request.args.get("review_level", "").strip()
    source_filter = request.args.get("source", "").strip()
    search_text = request.args.get("q", "").strip()
    query = InventoryMovement.query.join(InventoryItem, InventoryMovement.item_id == InventoryItem.id).outerjoin(Commission, InventoryMovement.commission_id == Commission.id).outerjoin(User, InventoryMovement.user_id == User.id).outerjoin(TimeTaskTemplate, InventoryMovement.task_template_id == TimeTaskTemplate.id)
    if status_filter and status_filter != "all":
        query = query.filter(InventoryMovement.review_status == status_filter)
    if review_level_filter:
        query = query.filter(InventoryMovement.review_level == review_level_filter)
    if source_filter:
        query = query.filter(InventoryMovement.source == source_filter)
    if search_text:
        for term in [item.strip() for item in search_text.split() if item.strip()]:
            like_term = f"%{term}%"
            query = query.filter(or_(InventoryItem.item_number.ilike(like_term), InventoryItem.name.ilike(like_term), Commission.code.ilike(like_term), Commission.erp_idnum.ilike(like_term), Commission.title.ilike(like_term), TimeTaskTemplate.code.ilike(like_term), TimeTaskTemplate.title.ilike(like_term), User.display_name.ilike(like_term)))
    items = query.order_by(InventoryMovement.occurred_at.desc()).all()
    return render_template("admin/material_movements.html", items=items, status_filter=status_filter, review_level_filter=review_level_filter, source_filter=source_filter, search_text=search_text, material_sources=MaterialSource)


@admin_bp.post("/admin/material-movements/<int:item_id>/<string:decision>")
@foreman_required
def material_movement_review(item_id: int, decision: str) -> Any:
    item = db.get_or_404(InventoryMovement, item_id)
    if decision == "approve":
        item.review_status = TimeReviewStatus.APPROVED.value
    elif decision == "reject":
        item.review_status = TimeReviewStatus.REJECTED.value
    item.reviewed_by_user_id = current_user.id
    item.reviewed_at = utc_now()
    audit("material.review", "inventory_movement", item.id, {"decision": decision})
    db.session.commit()
    flash("Materialbuchung aktualisiert.", "success")
    return redirect(url_for("admin.material_movements"))


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


def save_user_form(item: User) -> None:
    item.email = request.form.get("email", "").strip().lower()
    item.display_name = request.form.get("display_name", "").strip()
    item.entra_oid = request.form.get("entra_oid") or None
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

    def add_candidate(
        erp_id: int,
        name: str | None,
    ) -> None:
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

        candidate["usage_count"] += 1

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

    if item.craftsman_erp_id is None:
        selected_ids.clear()

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
            "in den Zuständigkeiten vorhanden."
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


def store_company_code_stationery(
    item: WowiCompanyCode,
    upload: Any,
) -> None:
    prefix = upload.stream.read(5)
    upload.stream.seek(0)

    if prefix != b"%PDF-":
        raise ValueError(
            "Der Briefbogen muss eine "
            "gültige PDF-Datei sein."
        )

    directory = invoice_stationery_directory()
    filename = (
        f"company-code-{item.id}-"
        f"{uuid4().hex}.pdf"
    )
    file_path = directory / filename

    upload.save(file_path)

    previous_filename = (
        item.stationery_filename
    )
    item.stationery_filename = filename

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
) -> None:
    if not item.stationery_filename:
        return

    file_path = (
        invoice_stationery_directory()
        / item.stationery_filename
    )
    file_path.unlink(
        missing_ok=True
    )
    item.stationery_filename = None


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
    item.unit = request.form.get(
        "unit",
        "Stk",
    ).strip()
    item.ean = (
        request.form.get("ean")
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
    item.active = "active" in request.form


def save_time_template_form(item: TimeTaskTemplate) -> None:
    item.code = request.form.get("code", "").strip()
    item.title = request.form.get("title", "").strip()
    item.suggested_minutes = int(request.form.get("suggested_minutes", 0) or 0)
    item.active = "active" in request.form

    raw_craft_activity_id = request.form.get("craft_activity_id", "").strip()
    item.craft_activity_id = int(raw_craft_activity_id) if raw_craft_activity_id else None

    if not item.source:
        item.source = "manual"


def save_expected_materials_form(item: TimeTaskTemplate) -> None:
    item.expected_materials.clear()
    item_ids = request.form.getlist("expected_item_id")
    quantities = request.form.getlist("expected_quantity")
    notes = request.form.getlist("expected_note")
    seen_item_ids: set[int] = set()
    for index, raw_item_id in enumerate(item_ids):
        raw_item_id = raw_item_id.strip()
        if not raw_item_id:
            continue
        try:
            item_id = int(raw_item_id)
            quantity = float((quantities[index] if index < len(quantities) else "1") or 1)
        except (TypeError, ValueError):
            continue
        if item_id in seen_item_ids or quantity <= 0:
            continue
        seen_item_ids.add(item_id)
        item.expected_materials.append(TimeTaskExpectedMaterial(
            item_id=item_id,
            quantity=quantity,
            required=f"expected_required_{index}" in request.form,
            note=(notes[index].strip() if index < len(notes) and notes[index].strip() else None),
        ))


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
    position_codes = request.form.getlist(
        "position_code"
    )
    position_texts = request.form.getlist(
        "position_text"
    )
    position_internal_descriptions = request.form.getlist(
        "position_internal_description"
    )
    position_units = request.form.getlist(
        "position_units"
    )
    service_catalog_ids = request.form.getlist(
        "position_service_catalog_id"
    )

    position_count = max(
        len(position_codes),
        len(position_texts),
        len(position_internal_descriptions),
        len(position_units),
        len(service_catalog_ids),
        1,
    )

    positions = []

    for position_index in range(position_count):
        code = list_value(
            position_codes,
            position_index,
        )
        commission_text = list_value(
            position_texts,
            position_index,
        )
        internal_description = list_value(
            position_internal_descriptions,
            position_index,
        )
        units = list_value(
            position_units,
            position_index,
        )
        service_catalog_id = list_value(
            service_catalog_ids,
            position_index,
        )

        if not any(
            [
                code,
                commission_text,
                internal_description,
                service_catalog_id,
            ]
        ):
            continue

        positions.append(
            {
                "code": code,
                "commission_text": commission_text,
                "internal_description": internal_description,
                "description": commission_text,
                "units": units,
                "service_catalog_id": service_catalog_id,
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
        "creditor_id": request.form.get("creditor_id"),
        "department_id": request.form.get("department_id"),
        "responsible_official_repair_id": request.form.get(
            "responsible_official_repair_id"
        ),
        "use_unit_idnum": request.form.get(
            "use_unit_idnum"
        ),
        "building_idnum": request.form.get(
            "building_idnum"
        ),
        "license_agreement_idnum": request.form.get(
            "license_agreement_idnum"
        ),
        "title": request.form.get("title"),
        "damage_location": request.form.get(
            "damage_location"
        ),
        "contact_person": request.form.get(
            "contact_person"
        ),
        "execution_from": request.form.get(
            "execution_from"
        ),
        "execution_to": request.form.get(
            "execution_to"
        ),
        "positions": positions,
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

    operation.commission.erp_sync_state = (
        CommissionSyncState.PENDING.value
    )
    operation.commission.erp_sync_message = None

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
def invoice_archive(
    invoice_id: int,
) -> Any:
    invoice = db.session.execute(
        select(Invoice)
        .where(
            Invoice.id == invoice_id
        )
        .with_for_update()
    ).scalar_one_or_none()

    if invoice is None:
        abort(404)

    try:
        result = archive_invoice(
            invoice,
            current_user,
        )
        db.session.commit()
    except InvoiceArchiveError as exc:
        db.session.rollback()
        _store_invoice_archive_failure(
            invoice_id,
            str(exc),
        )
        current_app.logger.exception(
            "invoice_archive_failed "
            "invoice_id=%s",
            invoice_id,
        )
        flash(
            str(exc),
            "danger",
        )
    except Exception:
        db.session.rollback()
        _store_invoice_archive_failure(
            invoice_id,
            "Unerwarteter Fehler bei der "
            "d.velop-Archivierung.",
        )
        current_app.logger.exception(
            "invoice_archive_failed "
            "invoice_id=%s",
            invoice_id,
        )
        flash(
            "Die Rechnung konnte nicht nach "
            "d.velop übertragen werden.",
            "danger",
        )
    else:
        if result.already_archived:
            flash(
                "Die Rechnung war bereits in "
                "d.velop archiviert. Die lokale "
                "Zuordnung wurde aktualisiert.",
                "success",
            )
        else:
            flash(
                "Die Rechnung wurde erfolgreich "
                "in d.velop archiviert.",
                "success",
            )

    return redirect(
        url_for(
            "admin.invoice_edit",
            invoice_id=invoice_id,
        )
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
