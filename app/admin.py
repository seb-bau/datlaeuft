from __future__ import annotations

from io import BytesIO
from typing import Any

import qrcode
from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from .audit import audit
from .auth import admin_required, foreman_required
from .entra import import_entra_users
from .extensions import db
from .logging_service import configure_logging
from .settings import all_definitions, grouped_definitions, settings
from .catalog import sync_catalogs_from_wowiport
from sqlalchemy import or_
from .commission_service import create_pending_commission
from .models import (
    Commission,
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
)
from .inventory_service import (
    book_inventory_movement,
    get_default_inventory_location,
)
from .wowiport import (
    configured_commission_type_ids,
    sync_commission_from_erp,
)

admin_bp = Blueprint("admin", __name__)


@admin_bp.get("/")
@login_required
def root() -> Any:
    return redirect(url_for("admin.dashboard"))


@admin_bp.get("/admin")
@login_required
def dashboard() -> Any:
    open_count = Commission.query.filter(Commission.deleted_at.is_(None), Commission.local_status != "done").count()
    review_count = TimeEntry.query.filter_by(review_status=TimeReviewStatus.NEEDS_REVIEW.value).count()
    material_review_count = InventoryMovement.query.filter_by(review_status=TimeReviewStatus.NEEDS_REVIEW.value).count()
    low_stock_count = InventoryItem.query.filter(InventoryItem.stock_quantity <= InventoryItem.reorder_level).count()
    return render_template("admin/dashboard.html", open_count=open_count, review_count=review_count, material_review_count=material_review_count, low_stock_count=low_stock_count)


@admin_bp.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def settings_page() -> Any:
    if request.method == "POST":
        for definition in all_definitions():
            settings.set_from_form(definition, request.form.get(definition.key))
        audit("settings.update")
        db.session.commit()
        configure_logging(current_app._get_current_object())
        flash("Einstellungen gespeichert.", "success")
        return redirect(url_for("admin.settings_page"))
    return render_template("admin/settings.html", groups=grouped_definitions(), settings=settings)


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
    craft_activities = CraftActivity.query.filter_by(active=True).order_by(CraftActivity.name.asc()).all()

    if request.method == "POST":
        db.session.add(item)
        save_user_form(item)
        save_user_craft_activities(item)
        db.session.flush()
        audit("user.create", "user", item.id)
        db.session.commit()
        flash("Benutzer gespeichert.", "success")
        return redirect(url_for("admin.users"))

    return render_template("admin/user_form.html", item=item, craft_activities=craft_activities)


@admin_bp.route("/admin/users/<int:user_id>", methods=["GET", "POST"])
@admin_required
def user_edit(user_id: int) -> Any:
    item = db.get_or_404(User, user_id)
    craft_activities = CraftActivity.query.filter_by(active=True).order_by(CraftActivity.name.asc()).all()

    if request.method == "POST":
        save_user_form(item)
        save_user_craft_activities(item)
        audit("user.update", "user", item.id)
        db.session.commit()
        flash("Benutzer gespeichert.", "success")
        return redirect(url_for("admin.users"))

    return render_template("admin/user_form.html", item=item, craft_activities=craft_activities)


@admin_bp.route(
    "/admin/commissions/new",
    methods=["GET", "POST"],
)
@foreman_required
def commission_create() -> Any:
    service_positions = TimeTaskTemplate.query.join(
        CraftActivity
    ).filter(
        TimeTaskTemplate.active.is_(True),
        TimeTaskTemplate.erp_service_catalogue_id.isnot(None),
        CraftActivity.active.is_(True),
        CraftActivity.erp_id.isnot(None),
    ).order_by(
        TimeTaskTemplate.title.asc()
    ).all()

    company_codes = WowiCompanyCode.query.filter(
        WowiCompanyCode.active.is_(True)
    ).order_by(
        WowiCompanyCode.code.asc()
    ).all()

    allowed_type_ids = configured_commission_type_ids()

    if allowed_type_ids:
        commission_types = WowiCommissionType.query.filter(
            WowiCommissionType.active.is_(True),
            WowiCommissionType.erp_id.in_(allowed_type_ids),
        ).order_by(
            WowiCommissionType.code.asc(),
            WowiCommissionType.erp_id.asc(),
        ).all()
    else:
        commission_types = []

    users = User.query.filter(
        User.is_active_flag.is_(True),
        User.craftsman_erp_id.isnot(None),
    ).order_by(
        User.display_name.asc()
    ).all()

    position_rows = [{}]

    if request.method == "POST":
        form_data = read_commission_form()
        position_rows = form_data["positions"] or [{}]

        try:
            assigned_user_id = required_form_int(
                request.form.get("assigned_user_id"),
                "Handwerker",
            )

            assigned_user = User.query.filter_by(
                id=assigned_user_id,
                is_active_flag=True,
            ).one_or_none()

            if (
                assigned_user is None
                or assigned_user.craftsman_erp_id is None
            ):
                raise ValueError(
                    "Der ausgewählte Handwerker ist ungültig."
                )

            commission = create_pending_commission(
                data=form_data,
                assigned_user=assigned_user,
                created_by_user=current_user,
                source=CommissionSource.WEB,
            )

            db.session.flush()

            audit(
                "commission.create.web",
                "commission",
                commission.id,
                {
                    "public_id": commission.public_id,
                    "assigned_user_id": assigned_user.id,
                    "position_count": len(
                        commission.positions
                    ),
                },
            )

            db.session.commit()

            flash(
                "Der Auftrag wurde erfasst und zur "
                "Übertragung an Wowiport eingeplant.",
                "success",
            )

            return redirect(
                url_for("admin.commissions")
            )
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")

    return render_template(
        "admin/commission_form.html",
        service_positions=service_positions,
        company_codes=company_codes,
        commission_types=commission_types,
        users=users,
        position_rows=position_rows,
        form_values=request.form,
    )


@admin_bp.get("/admin/commissions")
@foreman_required
def commissions() -> Any:
    items = Commission.query.order_by(Commission.synced_at.desc()).all()
    return render_template("admin/commissions.html", items=items)


@admin_bp.post(
    "/admin/commissions/<int:commission_id>/sync"
)
@foreman_required
def commission_sync(commission_id: int) -> Any:
    item = db.get_or_404(Commission, commission_id)

    if item.erp_id is None:
        flash(
            "Der Auftrag besitzt noch keine Wowiport-ID. "
            "Die Übertragung wird automatisch wiederholt.",
            "warning",
        )
        return redirect(
            url_for("admin.commissions")
        )

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

    return redirect(
        url_for("admin.commissions")
    )


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
                note=request.form.get("note"),
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
                },
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

    return render_template(
        "admin/inventory_item.html",
        item=item,
        movements=movements,
        movement_types=InventoryMovementType,
        barcodes=barcodes,
        barcode_kinds=barcode_kinds,
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
    craft_activities = CraftActivity.query.filter_by(active=True).order_by(CraftActivity.name.asc()).all()

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

    query = TimeTaskTemplate.query.outerjoin(
        CraftActivity,
        TimeTaskTemplate.craft_activity_id == CraftActivity.id,
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
        CraftActivity.name.asc().nulls_last(),
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
    craft_activities = CraftActivity.query.filter_by(active=True).order_by(CraftActivity.name.asc()).all()
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
    items = query.order_by(TimeEntry.created_at.desc()).all()
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


@admin_bp.post("/admin/service-catalog/sync")
@foreman_required
def service_catalog_sync() -> Any:
    result = sync_catalogs_from_wowiport()
    audit("service_catalog.sync", metadata={
        "craft_activities_created": result.craft_activities_created,
        "craft_activities_updated": result.craft_activities_updated,
        "service_positions_created": result.service_positions_created,
        "service_positions_updated": result.service_positions_updated,
        "service_positions_skipped_unassigned_craft_activity": result.service_positions_skipped_unassigned_craft_activity,
        "service_positions_without_craft_activity": result.service_positions_without_craft_activity,
        "service_positions_deactivated": result.service_positions_deactivated,
    })
    db.session.commit()
    flash(
        f"Sync abgeschlossen: {result.craft_activities_created} Gewerke neu, "
        f"{result.craft_activities_updated} Gewerke aktualisiert, "
        f"{result.service_positions_created} Leistungspositionen neu, "
        f"{result.service_positions_updated} Leistungspositionen aktualisiert, "
        f"{result.service_positions_skipped_unassigned_craft_activity} wegen nicht zugeordnetem Gewerk übersprungen, "
        f"{result.service_positions_without_craft_activity} ohne Gewerk, "
        f"{result.service_positions_deactivated} deaktiviert.",
        "success",
    )
    return redirect(url_for("admin.time_templates"))


def save_user_form(item: User) -> None:
    item.email = request.form.get("email", "").strip().lower()
    item.display_name = request.form.get("display_name", "").strip()
    item.entra_oid = request.form.get("entra_oid") or None
    raw_craftsman_id = request.form.get("craftsman_erp_id", "").strip()
    item.craftsman_erp_id = int(raw_craftsman_id) if raw_craftsman_id else None
    item.is_admin = "is_admin" in request.form
    item.is_foreman = "is_foreman" in request.form
    item.is_active_flag = "is_active_flag" in request.form
    password = request.form.get("password", "")
    if password:
        item.set_password(password)


def save_user_craft_activities(item: User) -> None:
    selected_activity_ids = []

    for value in request.form.getlist("craft_activity_ids"):
        try:
            selected_activity_ids.append(int(value))
        except ValueError:
            continue

    if not selected_activity_ids:
        item.craft_activities = []
        return

    item.craft_activities = CraftActivity.query.filter(
        CraftActivity.id.in_(selected_activity_ids),
        CraftActivity.active.is_(True),
    ).order_by(CraftActivity.name.asc()).all()


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


def save_inventory_form(item: InventoryItem) -> None:
    item.item_number = request.form.get("item_number", "").strip()
    item.name = request.form.get("name", "").strip()
    item.unit = request.form.get("unit", "Stk").strip()
    item.ean = request.form.get("ean") or None
    item.reorder_level = float(request.form.get("reorder_level", 0))
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


def apply_inventory_movement(
    item: InventoryItem,
    movement: InventoryMovement,
) -> None:
    if movement.movement_type == InventoryMovementType.WITHDRAWAL.value:
        movement.source_location = (
            get_default_inventory_location()
        )
    else:
        movement.target_location = (
            get_default_inventory_location()
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
        "commission_type_id": request.form.get(
            "commission_type_id"
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
        "company_code_code": request.form.get(
            "company_code_code"
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
