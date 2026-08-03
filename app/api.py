from __future__ import annotations

from typing import Any

from flask import Blueprint, abort, current_app, g, jsonify, request
from flask_jwt_extended import create_access_token, get_jwt_identity, jwt_required
from sqlalchemy import or_

from .audit import audit
from .auth import resolve_microsoft_user, validate_microsoft_id_token
from .extensions import db
from .models import (
    Commission,
    CommissionLocalStatus,
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
    utc_now,
    CraftActivity,
    CommissionSource,
    WowiCommissionType,
    WowiCompanyCode,
    AuditLog
)
from .settings import SettingKeys, settings
from .utils import iso_datetime
from .wowiport import (
    WowiportService,
    configured_commission_type_ids,
)
from .catalog import get_user_visible_time_task, service_position_query_for_user
from .erp_sync import enqueue_erp_operation
from .models import CommissionSyncState, ErpOperationType

from .commission_service import (
    create_pending_commission,
    normalize_public_id,
    integer_set
)


api_bp = Blueprint("api", __name__, url_prefix="/api")
CANCELLATION_NOTE_PREFIX = "Storno zu Buchung #"

@api_bp.get("/mobile/config")
def mobile_config() -> Any:
    microsoft_config = None

    if mobile_microsoft_login_available():
        scopes = [
            item.strip()
            for item in settings.get(SettingKeys.MOBILE_MICROSOFT_SCOPES).split(",")
            if item.strip()
        ]

        microsoft_config = {
            "tenant_id": settings.get(SettingKeys.ENTRA_TENANT_ID).strip(),
            "client_id": settings.get(SettingKeys.MOBILE_MICROSOFT_CLIENT_ID).strip(),
            "redirect_uri": settings.get(SettingKeys.MOBILE_MICROSOFT_REDIRECT_URI).strip(),
            "scopes": scopes or ["User.Read"],
        }

    return jsonify({
        "password_login_enabled": settings.get(SettingKeys.PASSWORD_LOGIN_ENABLED),
        "microsoft": microsoft_config,
    })


@api_bp.post("/auth/password")
def api_password_login() -> Any:
    if not settings.get(SettingKeys.PASSWORD_LOGIN_ENABLED):
        abort(403)
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    user = User.query.filter_by(email=email).one_or_none()
    if user is None or not user.check_password(password) or not user.is_active:
        abort(401)
    return _issue_token(user, "api.password_login")


@api_bp.post("/auth/microsoft")
def api_microsoft_login() -> Any:
    if not mobile_microsoft_login_available():
        abort(403)
    data = request.get_json(silent=True) or {}
    id_token = str(data.get("id_token", ""))
    claims = validate_microsoft_id_token(id_token)
    user = resolve_microsoft_user(claims, settings.get(SettingKeys.MICROSOFT_AUTO_CREATE_USERS))
    if user is None or not user.is_active:
        abort(401)
    return _issue_token(user, "api.microsoft_login")


@api_bp.get("/me")
@jwt_required()
def me() -> Any:
    user = current_api_user()
    return jsonify({"user": serialize_user(user)})


@api_bp.get("/commissions")
@jwt_required()
def commissions() -> Any:
    user = current_api_user()
    scope = request.args.get("scope", "mine").strip()
    status = request.args.get("status", "").strip()
    user_id_filter = request.args.get("user_id", "").strip()

    query = Commission.query.filter(Commission.local_status != CommissionLocalStatus.DELETED.value)
    can_view_colleagues = can_view_colleague_commissions(user)

    if scope != "all" or not can_view_colleagues:
        query = query.filter_by(user_id=user.id)
    elif user_id_filter.isdigit():
        query = query.filter_by(user_id=int(user_id_filter))

    if status == "active":
        query = query.filter(
            Commission.local_status.in_(
                [
                    CommissionLocalStatus.RECORDED.value,
                    CommissionLocalStatus.WAITING_FOR_ACCEPTANCE.value,
                    CommissionLocalStatus.OPEN.value,
                    CommissionLocalStatus.IN_PROGRESS.value,
                    CommissionLocalStatus.COMPLETION_REVIEW.value,
                    CommissionLocalStatus.TENANT_CHARGE_REVIEW.value,
                ]
            )
        )
    elif status in {
        item.value
        for item in CommissionLocalStatus
    }:
        query = query.filter_by(
            local_status=status
        )

    query = query.order_by(Commission.execution_from.asc().nulls_last(), Commission.id.asc())
    return jsonify({"items": [serialize_commission(item, viewer=user) for item in query.all()]})


@api_bp.get("/commissions/users")
@jwt_required()
def commission_users() -> Any:
    user = current_api_user()

    if not can_view_colleague_commissions(user):
        return jsonify({"items": [serialize_user(user)]})

    items = User.query.join(
        Commission,
        Commission.user_id == User.id,
    ).filter(
        Commission.local_status != CommissionLocalStatus.DELETED.value,
        User.is_active_flag.is_(True),
    ).distinct().order_by(
        User.display_name.asc(),
    ).all()

    existing_ids = {item.id for item in items}
    if user.id not in existing_ids:
        items.insert(0, user)

    return jsonify({"items": [serialize_user(item) for item in items]})


@api_bp.get("/commissions/create-options")
@jwt_required()
def commission_create_options() -> Any:
    user = current_api_user()

    allowed_type_ids = integer_set(
        settings.get(
            SettingKeys.WOWI_ALLOWED_COMMISSION_TYPE_IDS
        )
    )

    if allowed_type_ids:
        commission_types = WowiCommissionType.query.filter(
            WowiCommissionType.active.is_(True),
            WowiCommissionType.erp_id.in_(
                allowed_type_ids
            ),
        ).order_by(
            WowiCommissionType.code.asc(),
            WowiCommissionType.erp_id.asc(),
        ).all()
    else:
        commission_types = []

    company_codes = WowiCompanyCode.query.filter(
        WowiCompanyCode.active.is_(True)
    ).order_by(
        WowiCompanyCode.code.asc()
    ).all()

    return jsonify(
        {
            "can_create": (
                user.craftsman_erp_id is not None
            ),
            "company_codes": [
                {
                    "id": item.erp_id,
                    "code": item.code,
                    "name": item.name,
                }
                for item in company_codes
            ],
            "commission_types": [
                {
                    "id": item.erp_id,
                    "code": item.code,
                }
                for item in commission_types
            ],
        }
    )


@api_bp.post("/commissions")
@jwt_required()
def create_commission() -> Any:
    user = current_api_user()
    data = request.get_json(silent=True) or {}

    if not isinstance(data, dict):
        return jsonify(
            {
                "message": "Die übermittelten Auftragsdaten sind ungültig."
            }
        ), 400

    try:
        commission = create_pending_commission(
            data=data,
            assigned_user=user,
            created_by_user=user,
            source=CommissionSource.IOS,
        )

        db.session.flush()

        audit(
            "commission.create.ios",
            "commission",
            commission.id,
            {
                "public_id": commission.public_id,
                "assigned_user_id": user.id,
                "position_count": len(
                    commission.positions
                ),
            },
        )

        db.session.commit()
    except ValueError as exc:
        db.session.rollback()

        return jsonify(
            {
                "message": str(exc)
            }
        ), 400

    return jsonify(
        {
            "item": serialize_commission(
                commission,
                include_details=True,
                viewer=user,
            )
        }
    ), 202


@api_bp.get("/commissions/<int:commission_id>")
@jwt_required()
def commission_detail(commission_id: int) -> Any:
    user = current_api_user()
    commission = viewable_commission(commission_id, user)
    return jsonify({"item": serialize_commission(commission, include_details=True, viewer=user)})


@api_bp.get(
    "/commissions/<int:commission_id>/history"
)
@jwt_required()
def commission_history(
    commission_id: int,
) -> Any:
    user = current_api_user()
    commission = viewable_commission(
        commission_id,
        user,
    )

    entries = commission_history_entries(
        commission
    )

    return jsonify(
        {
            "items": [
                serialize_history_entry(entry)
                for entry in entries
            ]
        }
    )


@api_bp.get("/commissions/<int:commission_id>/contacts")
@jwt_required()
def commission_contacts(commission_id: int) -> Any:
    user = current_api_user()
    commission = viewable_commission(commission_id, user)

    try:
        contact_items = WowiportService().get_commission_contact_items(commission.erp_id)
    except Exception as exc:
        current_app.logger.exception("commission_contacts_failed commission_id=%s erp_id=%s", commission.id, commission.erp_id)
        return jsonify({"contact_items": [], "message": str(exc)}), 502

    return jsonify({"contact_items": contact_items})


@api_bp.post("/commissions/<int:commission_id>/start")
@jwt_required()
def start_commission(commission_id: int) -> Any:
    user = current_api_user()
    commission = editable_commission(commission_id, user)

    if not commission.is_actionable:
        return jsonify({
            "message": "Der Auftrag wartet noch auf eine OPENWOWI-Auftragsnummer."
        }), 409

    if commission.local_status != CommissionLocalStatus.WAITING_FOR_ACCEPTANCE.value:
        return jsonify({
            "message": "Der Auftrag kann in diesem Status nicht gestartet werden."
        }), 409

    commission.local_status = CommissionLocalStatus.IN_PROGRESS.value
    commission.erp_sync_state = CommissionSyncState.PENDING.value

    enqueue_erp_operation(
        commission,
        ErpOperationType.ACCEPT_COMMISSION,
        f"commission:accepted:{commission.id}",
    )

    audit("commission.start", "commission", commission.id)
    db.session.commit()

    return jsonify({
        "item": serialize_commission(commission),
    }), 202


@api_bp.post("/commissions/<int:commission_id>/complete")
@jwt_required()
def complete_commission(
    commission_id: int,
) -> Any:
    user = current_api_user()
    commission = editable_commission(
        commission_id,
        user,
    )
    data = request.get_json(silent=True) or {}

    if not commission.is_actionable:
        return jsonify(
            {
                "message": (
                    "Der Auftrag wartet noch auf eine "
                    "OPENWOWI-Auftragsnummer."
                )
            }
        ), 409

    if (
        commission.local_status
        != CommissionLocalStatus.IN_PROGRESS.value
    ):
        return jsonify(
            {
                "message": (
                    "Nur ein Auftrag in Bearbeitung "
                    "kann abgeschlossen werden."
                )
            }
        ), 409

    if (
        "completed" not in data
        or not isinstance(
            data.get("completed"),
            bool,
        )
    ):
        return jsonify(
            {
                "message": (
                    "Bitte angeben, ob der Auftrag "
                    "abgeschlossen werden konnte."
                )
            }
        ), 400

    completed = data["completed"]
    tenant_charge = bool(
        data.get("tenant_charge", False)
    )
    reason = str(
        data.get("reason") or ""
    ).strip()

    if not completed:
        tenant_charge = False

        if not reason:
            return jsonify(
                {
                    "message": (
                        "Bitte einen Grund angeben, "
                        "warum der Auftrag nicht "
                        "abgeschlossen werden konnte."
                    )
                }
            ), 400

    commission.completion_successful = completed
    commission.tenant_charge_requested = (
        tenant_charge
    )
    commission.completion_reason = (
        reason or None
    )
    commission.completion_submitted_by = user
    commission.completion_submitted_at = (
        utc_now()
    )
    commission.erp_sync_state = (
        CommissionSyncState.PENDING.value
    )
    commission.erp_sync_message = None

    if not completed:
        commission.local_status = (
            CommissionLocalStatus.COMPLETION_REVIEW.value
        )
        commission.completed_at = None

        enqueue_erp_operation(
            commission,
            ErpOperationType.CANCEL_COMMISSION,
            f"commission:canceled:{commission.id}",
        )
    elif tenant_charge:
        commission.local_status = (
            CommissionLocalStatus.TENANT_CHARGE_REVIEW.value
        )
        commission.completed_at = utc_now()

        enqueue_erp_operation(
            commission,
            ErpOperationType.AWAIT_INVOICE,
            f"commission:await-invoice:{commission.id}",
        )
    else:
        commission.local_status = (
            CommissionLocalStatus.DONE.value
        )
        commission.completed_at = utc_now()

        enqueue_erp_operation(
            commission,
            ErpOperationType.COMPLETE_COMMISSION,
            f"commission:done:{commission.id}",
        )

    audit(
        "commission.complete",
        "commission",
        commission.id,
        {
            "completed": completed,
            "tenant_charge": tenant_charge,
            "reason": reason or None,
        },
    )

    db.session.commit()

    return jsonify(
        {
            "item": serialize_commission(
                commission,
                viewer=user,
            )
        }
    ), 202


@api_bp.get("/inventory/lookup")
@jwt_required()
def inventory_lookup() -> Any:
    current_api_user()
    code = request.args.get("code", "").strip()
    item = find_inventory_item(code)
    if item is None:
        abort(404)
    return jsonify({"item": serialize_inventory_item(item)})


@api_bp.get("/inventory/search")
@jwt_required()
def inventory_search() -> Any:
    current_api_user()

    search_text = request.args.get("q", "").strip()
    if len(search_text) < 2:
        return jsonify({"items": []})

    try:
        limit = int(request.args.get("limit", 12))
    except ValueError:
        limit = 12

    limit = min(max(limit, 1), 50)

    query = InventoryItem.query.filter(InventoryItem.active.is_(True))

    for term in [item.strip() for item in search_text.split() if item.strip()]:
        like_term = f"%{term}%"
        query = query.filter(or_(
            InventoryItem.name.ilike(like_term),
            InventoryItem.item_number.ilike(like_term),
            InventoryItem.ean.ilike(like_term),
            InventoryItem.qr_code_payload.ilike(like_term),
        ))

    items = query.order_by(
        InventoryItem.name.asc(),
        InventoryItem.item_number.asc(),
    ).limit(limit).all()

    return jsonify({"items": [serialize_inventory_item(item) for item in items]})


@api_bp.get("/commissions/<int:commission_id>/materials")
@jwt_required()
def commission_materials(commission_id: int) -> Any:
    user = current_api_user()
    commission = viewable_commission(commission_id, user)
    cancelled_ids = cancelled_inventory_movement_ids(commission.id)

    movements = (
        InventoryMovement.query
        .filter_by(commission_id=commission.id)
        .order_by(InventoryMovement.occurred_at.desc())
        .all()
    )

    return jsonify({
        "items": [
            serialize_inventory_movement(
                item,
                cancelled_ids=cancelled_ids,
                can_edit=can_edit_commission(commission, user),
            )
            for item in movements
        ]
    })


@api_bp.post("/commissions/<int:commission_id>/materials")
@jwt_required()
def add_commission_material(commission_id: int) -> Any:
    user = current_api_user()
    commission = editable_commission(commission_id, user)
    data = request.get_json(silent=True) or {}
    item = find_inventory_item(str(data.get("code", "")).strip())
    if item is None:
        abort(404)
    try:
        quantity = float(data.get("quantity", 1))
    except (TypeError, ValueError):
        abort(400)
    if quantity <= 0:
        abort(400)
    task = None
    if data.get("task_template_id"):
        task = get_user_visible_time_task(user, int(data["task_template_id"]))
        if task is None:
            abort(403)
    source = str(data.get("source") or MaterialSource.WAREHOUSE.value).strip()
    if source not in {item.value for item in MaterialSource}:
        abort(400)
    usage_status = str(data.get("usage_status") or MaterialUsageStatus.INSTALLED.value).strip()
    if usage_status not in {item.value for item in MaterialUsageStatus}:
        abort(400)
    movement = InventoryMovement(
        item=item,
        user=user,
        commission=commission,
        task_template=task,
        movement_type=InventoryMovementType.WITHDRAWAL.value,
        quantity=quantity,
        note=data.get("note"),
        source=source,
        usage_status=usage_status,
        deviation_reason=data.get("deviation_reason"),
    )
    db.session.add(movement)
    if source == MaterialSource.WAREHOUSE.value:
        item.stock_quantity -= quantity
    with db.session.no_autoflush:
        apply_material_review(movement)
    audit("inventory.withdrawal", "inventory_item", item.id,
          {"commission_id": commission.id, "quantity": quantity, "task_template_id": task.id if task else None,
           "source": source})
    db.session.commit()
    return jsonify({"item": serialize_inventory_movement(movement), "inventory_item": serialize_inventory_item(item)}), 201


@api_bp.post("/commissions/<int:commission_id>/materials/<int:movement_id>/cancel")
@jwt_required()
def cancel_commission_material(commission_id: int, movement_id: int) -> Any:
    user = current_api_user()
    commission = editable_commission(commission_id, user)

    movement = (
        InventoryMovement.query
        .filter_by(id=movement_id, commission_id=commission.id)
        .one_or_none()
    )

    if movement is None:
        abort(404)

    if movement.movement_type != InventoryMovementType.WITHDRAWAL.value:
        return jsonify({"message": "Nur Entnahmen können storniert werden."}), 400

    if movement.id in cancelled_inventory_movement_ids(commission.id):
        return jsonify({"message": "Diese Materialbuchung wurde bereits storniert."}), 409

    item = movement.item
    if movement.source == MaterialSource.WAREHOUSE.value:
        item.stock_quantity += movement.quantity

    note_parts = [f"{CANCELLATION_NOTE_PREFIX}{movement.id}"]

    if movement.note:
        note_parts.append(f"Originalnotiz: {movement.note}")

    cancellation = InventoryMovement(
        item=item,
        user=user,
        commission=commission,
        task_template=movement.task_template,
        movement_type=InventoryMovementType.CANCELLATION.value,
        quantity=movement.quantity,
        note=" · ".join(note_parts),
        source=movement.source,
        usage_status=MaterialUsageStatus.RETURNED.value,
        review_status=TimeReviewStatus.OK.value,
        review_level=ReviewLevel.GREEN.value,
    )

    db.session.add(cancellation)
    audit(
        "inventory.cancel_withdrawal",
        "inventory_item",
        item.id,
        {
            "commission_id": commission.id,
            "movement_id": movement.id,
            "quantity": movement.quantity,
        },
    )
    db.session.commit()

    cancelled_ids = cancelled_inventory_movement_ids(commission.id)

    return jsonify({
        "item": serialize_inventory_movement(
            cancellation,
            cancelled_ids=cancelled_ids,
            can_edit=can_edit_commission(commission, user),
        ),
        "inventory_item": serialize_inventory_item(item),
    }), 201


@api_bp.get("/time-tasks")
@jwt_required()
def time_tasks() -> Any:
    user = current_api_user()
    tasks = service_position_query_for_user(user).outerjoin(
        CraftActivity,
        TimeTaskTemplate.craft_activity_id == CraftActivity.id,
    ).order_by(
        CraftActivity.name.asc().nulls_last(),
        TimeTaskTemplate.title.asc(),
    ).all()
    return jsonify({"items": [serialize_time_task(item) for item in tasks]})


@api_bp.get("/time-tasks/search")
@jwt_required()
def time_tasks_search() -> Any:
    user = current_api_user()

    search_text = request.args.get("q", "").strip()
    if len(search_text) < 2:
        return jsonify({"items": []})

    try:
        limit = int(request.args.get("limit", 12))
    except ValueError:
        limit = 12

    limit = min(max(limit, 1), 50)

    query = service_position_query_for_user(user).outerjoin(
        CraftActivity,
        TimeTaskTemplate.craft_activity_id == CraftActivity.id,
    )

    for term in [item.strip() for item in search_text.split() if item.strip()]:
        like_term = f"%{term}%"
        query = query.filter(or_(
            TimeTaskTemplate.code.ilike(like_term),
            TimeTaskTemplate.title.ilike(like_term),
            TimeTaskTemplate.erp_idnum.ilike(like_term),
            CraftActivity.code.ilike(like_term),
            CraftActivity.name.ilike(like_term),
        ))

    items = query.order_by(
        CraftActivity.name.asc().nulls_last(),
        TimeTaskTemplate.title.asc(),
        TimeTaskTemplate.code.asc(),
    ).limit(limit).all()

    return jsonify({"items": [serialize_time_task(item) for item in items]})


@api_bp.get("/commissions/<int:commission_id>/time")
@jwt_required()
def commission_time_entries(commission_id: int) -> Any:
    user = current_api_user()
    commission = viewable_commission(commission_id, user)
    query = TimeEntry.query.filter_by(commission_id=commission.id)

    if not can_view_colleague_commissions(user):
        query = query.filter_by(user_id=user.id)

    entries = query.order_by(TimeEntry.created_at.desc()).all()
    return jsonify({"items": [serialize_time_entry(item, viewer=user) for item in entries]})


@api_bp.post("/commissions/<int:commission_id>/time")
@jwt_required()
def add_time_entry(commission_id: int) -> Any:
    user = current_api_user()
    commission = editable_commission(commission_id, user)
    data = request.get_json(silent=True) or {}
    values = time_entry_values_from_payload(data, user)
    entry = TimeEntry(
        commission=commission,
        user=user,
        task_template=values["task"],
        minutes=values["minutes"],
        work_minutes=values["work_minutes"],
        waiting_minutes=values["waiting_minutes"],
        material_minutes=values["material_minutes"],
        travel_minutes=values["travel_minutes"],
        documentation_minutes=values["documentation_minutes"],
        extra_minutes=values["extra_minutes"],
        deviation_reason=values["deviation_reason"],
        note=values["note"],
    )
    db.session.add(entry)
    with db.session.no_autoflush:
        apply_time_review(entry)
    audit("time.create", "commission", commission.id,
          {"minutes": entry.minutes, "task_template_id": entry.task_template_id})
    db.session.commit()
    return jsonify({"item": serialize_time_entry(entry, viewer=user)}), 201


@api_bp.patch("/commissions/<int:commission_id>/time/<int:entry_id>")
@jwt_required()
def update_time_entry(commission_id: int, entry_id: int) -> Any:
    user = current_api_user()
    commission, entry = editable_time_entry(commission_id, entry_id, user)
    data = request.get_json(silent=True) or {}
    values = time_entry_values_from_payload(data, user)
    entry.task_template = values["task"]
    entry.minutes = values["minutes"]
    entry.work_minutes = values["work_minutes"]
    entry.waiting_minutes = values["waiting_minutes"]
    entry.material_minutes = values["material_minutes"]
    entry.travel_minutes = values["travel_minutes"]
    entry.documentation_minutes = values["documentation_minutes"]
    entry.extra_minutes = values["extra_minutes"]
    entry.deviation_reason = values["deviation_reason"]
    entry.note = values["note"]
    entry.reviewed_by_user_id = None
    entry.reviewed_at = None
    apply_time_review(entry)
    audit("time.update", "time_entry", entry.id, {"commission_id": commission.id, "minutes": entry.minutes, "task_template_id": entry.task_template_id})
    db.session.commit()
    return jsonify({"item": serialize_time_entry(entry, viewer=user)})


@api_bp.delete("/commissions/<int:commission_id>/time/<int:entry_id>")
@jwt_required()
def delete_time_entry(commission_id: int, entry_id: int) -> Any:
    user = current_api_user()
    commission, entry = editable_time_entry(commission_id, entry_id, user)
    audit("time.delete", "time_entry", entry.id, {"commission_id": commission.id, "minutes": entry.minutes, "task_template_id": entry.task_template_id})
    db.session.delete(entry)
    db.session.commit()
    return jsonify({})


@api_bp.get("/commissions/<int:commission_id>/material-review")
@jwt_required()
def commission_material_review(commission_id: int) -> Any:
    user = current_api_user()
    commission = viewable_commission(commission_id, user)
    entries = TimeEntry.query.filter_by(commission_id=commission.id).all()
    task_ids = {entry.task_template_id for entry in entries if entry.task_template_id}
    if not task_ids:
        return jsonify({"items": []})
    expectations = TimeTaskExpectedMaterial.query.filter(TimeTaskExpectedMaterial.task_template_id.in_(task_ids)).all()
    movements = InventoryMovement.query.filter_by(
        commission_id=commission.id,
        movement_type=InventoryMovementType.WITHDRAWAL.value,
    ).all()
    result = []
    for expectation in expectations:
        installed_quantity = sum(
            movement.quantity
            for movement in movements
            if movement.task_template_id == expectation.task_template_id
            and movement.item_id == expectation.item_id
            and movement.usage_status == MaterialUsageStatus.INSTALLED.value
        )
        is_ok = installed_quantity + 0.0001 >= expectation.quantity
        result.append({
            "task_template": serialize_time_task(expectation.task_template),
            "expected_material": serialize_expected_material(expectation),
            "installed_quantity": installed_quantity,
            "missing_quantity": max(expectation.quantity - installed_quantity, 0),
            "review_status": TimeReviewStatus.OK.value if is_ok else TimeReviewStatus.NEEDS_REVIEW.value,
            "review_level": ReviewLevel.GREEN.value if is_ok else ReviewLevel.RED.value,
        })
    return jsonify({"items": result})


@api_bp.get("/review/time-entries")
@jwt_required()
def review_time_entries() -> Any:
    user = current_api_user()
    require_review_permission(user)
    query = TimeEntry.query.join(Commission, TimeEntry.commission_id == Commission.id).join(User, TimeEntry.user_id == User.id).outerjoin(TimeTaskTemplate, TimeEntry.task_template_id == TimeTaskTemplate.id)
    status = request.args.get("status", TimeReviewStatus.NEEDS_REVIEW.value).strip()
    review_level = request.args.get("review_level", request.args.get("risk", "")).strip()
    user_id = request.args.get("user_id", "").strip()
    commission_id = request.args.get("commission_id", "").strip()
    search_text = request.args.get("q", "").strip()
    if status and status != "all":
        query = query.filter(TimeEntry.review_status == status)
    if review_level and review_level != "all":
        query = query.filter(TimeEntry.review_level == review_level)
    if user_id.isdigit():
        query = query.filter(TimeEntry.user_id == int(user_id))
    if commission_id.isdigit():
        query = query.filter(TimeEntry.commission_id == int(commission_id))
    if search_text:
        for term in [item.strip() for item in search_text.split() if item.strip()]:
            like_term = f"%{term}%"
            query = query.filter(or_(
                Commission.code.ilike(like_term),
                Commission.erp_idnum.ilike(like_term),
                Commission.title.ilike(like_term),
                TimeTaskTemplate.code.ilike(like_term),
                TimeTaskTemplate.title.ilike(like_term),
                User.display_name.ilike(like_term),
            ))
    items = query.order_by(TimeEntry.created_at.desc()).limit(query_limit()).all()
    return jsonify({"items": [serialize_time_entry(item, viewer=user) for item in items]})


@api_bp.get("/review/materials")
@jwt_required()
def review_materials() -> Any:
    user = current_api_user()
    require_review_permission(user)
    query = InventoryMovement.query.join(InventoryItem, InventoryMovement.item_id == InventoryItem.id).outerjoin(Commission, InventoryMovement.commission_id == Commission.id).outerjoin(User, InventoryMovement.user_id == User.id).outerjoin(TimeTaskTemplate, InventoryMovement.task_template_id == TimeTaskTemplate.id)
    status = request.args.get("status", TimeReviewStatus.NEEDS_REVIEW.value).strip()
    review_level = request.args.get("review_level", request.args.get("risk", "")).strip()
    user_id = request.args.get("user_id", "").strip()
    commission_id = request.args.get("commission_id", "").strip()
    movement_type = request.args.get("movement_type", "").strip()
    source = request.args.get("source", "").strip()
    search_text = request.args.get("q", "").strip()
    if status and status != "all":
        query = query.filter(InventoryMovement.review_status == status)
    if review_level and review_level != "all":
        query = query.filter(InventoryMovement.review_level == review_level)
    if user_id.isdigit():
        query = query.filter(InventoryMovement.user_id == int(user_id))
    if commission_id.isdigit():
        query = query.filter(InventoryMovement.commission_id == int(commission_id))
    if movement_type:
        query = query.filter(InventoryMovement.movement_type == movement_type)
    if source:
        query = query.filter(InventoryMovement.source == source)
    if search_text:
        for term in [item.strip() for item in search_text.split() if item.strip()]:
            like_term = f"%{term}%"
            query = query.filter(or_(
                InventoryItem.item_number.ilike(like_term),
                InventoryItem.name.ilike(like_term),
                Commission.code.ilike(like_term),
                Commission.erp_idnum.ilike(like_term),
                Commission.title.ilike(like_term),
                TimeTaskTemplate.code.ilike(like_term),
                TimeTaskTemplate.title.ilike(like_term),
                User.display_name.ilike(like_term),
            ))
    items = query.order_by(InventoryMovement.occurred_at.desc()).limit(query_limit()).all()
    return jsonify({"items": [serialize_inventory_movement(item, can_edit=True) for item in items]})


def _issue_token(user: User, action: str) -> Any:
    user.last_login_at = utc_now()
    expires_delta = False if settings.get(SettingKeys.MOBILE_TOKEN_NEVER_EXPIRES) else None
    access_token = create_access_token(
        identity=str(user.id),
        expires_delta=expires_delta,
        additional_claims={
            "email": user.email,
            "is_admin": user.is_admin,
            "is_foreman": user.is_foreman,
        },
    )
    audit(action, "user", user.id)
    db.session.commit()
    return jsonify({"access_token": access_token, "user": serialize_user(user)})


def mobile_microsoft_login_available() -> bool:
    tenant_id = settings.get(SettingKeys.ENTRA_TENANT_ID).strip()
    client_id = settings.get(SettingKeys.MOBILE_MICROSOFT_CLIENT_ID).strip()
    redirect_uri = settings.get(SettingKeys.MOBILE_MICROSOFT_REDIRECT_URI).strip()

    return (
        settings.get(SettingKeys.MICROSOFT_LOGIN_ENABLED)
        and bool(tenant_id)
        and bool(client_id)
        and bool(redirect_uri)
    )


def current_api_user() -> User:
    identity = get_jwt_identity()

    user = (
        db.session.get(
            User,
            int(identity),
        )
        if identity
        else None
    )

    if user is None or not user.is_active:
        abort(401)

    g.api_user = user

    return user


def can_view_colleague_commissions(user: User) -> bool:
    return user.is_admin or user.is_foreman or settings.get(SettingKeys.MOBILE_ALLOW_COLLEAGUE_COMMISSIONS)


def can_edit_commission(commission: Commission, user: User) -> bool:
    return user.is_admin or user.is_foreman or commission.user_id == user.id


def can_edit_time_entry(entry: TimeEntry, user: User) -> bool:
    return user.is_admin or user.is_foreman or entry.user_id == user.id


def viewable_commission(commission_id: int, user: User) -> Commission:
    commission = db.session.get(Commission, commission_id)
    if commission is None or commission.local_status == CommissionLocalStatus.DELETED.value:
        abort(404)
    if commission.user_id != user.id and not can_view_colleague_commissions(user):
        abort(403)
    return commission


def editable_commission(commission_id: int, user: User) -> Commission:
    commission = viewable_commission(commission_id, user)
    if not can_edit_commission(commission, user):
        abort(403)
    return commission


def editable_time_entry(commission_id: int, entry_id: int, user: User) -> tuple[Commission, TimeEntry]:
    commission = editable_commission(commission_id, user)
    entry = TimeEntry.query.filter_by(id=entry_id, commission_id=commission.id).one_or_none()
    if entry is None:
        abort(404)
    if not can_edit_time_entry(entry, user):
        abort(403)
    return commission, entry


def cancelled_inventory_movement_ids(commission_id: int) -> set[int]:
    notes = (
        db.session.query(InventoryMovement.note)
        .filter(
            InventoryMovement.commission_id == commission_id,
            InventoryMovement.movement_type == InventoryMovementType.CANCELLATION.value,
            InventoryMovement.note.ilike(f"{CANCELLATION_NOTE_PREFIX}%"),
        )
        .all()
    )

    result = set()

    for row in notes:
        note = row[0] or ""

        if not note.startswith(CANCELLATION_NOTE_PREFIX):
            continue

        number_text = note.removeprefix(CANCELLATION_NOTE_PREFIX).split(" ", 1)[0].split("·", 1)[0].strip()

        try:
            result.add(int(number_text))
        except ValueError:
            continue

    return result


def find_inventory_item(code: str) -> InventoryItem | None:
    if not code:
        return None

    return db.session.query(InventoryItem).filter(
        InventoryItem.active.is_(True),
        or_(
            InventoryItem.qr_code_payload == code,
            InventoryItem.ean == code,
            InventoryItem.item_number == code,
        ),
    ).one_or_none()


def int_payload(data: dict[str, Any], key: str) -> int:
    try:
        return int(data.get(key, 0) or 0)
    except (TypeError, ValueError):
        abort(400)


def query_limit() -> int:
    try:
        limit = int(request.args.get("limit", 100))
    except ValueError:
        limit = 100
    return min(max(limit, 1), 500)


def time_entry_values_from_payload(data: dict[str, Any], user: User) -> dict[str, Any]:
    work_minutes = int_payload(data, "work_minutes")
    waiting_minutes = int_payload(data, "waiting_minutes")
    material_minutes = int_payload(data, "material_minutes")
    travel_minutes = int_payload(data, "travel_minutes")
    documentation_minutes = int_payload(data, "documentation_minutes")
    extra_minutes = int_payload(data, "extra_minutes")
    for value in [work_minutes, waiting_minutes, material_minutes, travel_minutes, documentation_minutes, extra_minutes]:
        if value < 0:
            abort(400)
    component_sum = work_minutes + waiting_minutes + material_minutes + travel_minutes + documentation_minutes + extra_minutes
    minutes = component_sum if component_sum > 0 else int_payload(data, "minutes")
    if minutes <= 0:
        abort(400)
    task = None
    if data.get("task_template_id"):
        task = get_user_visible_time_task(user, int(data["task_template_id"]))
        if task is None:
            abort(403)
    return {
        "task": task,
        "minutes": minutes,
        "work_minutes": work_minutes,
        "waiting_minutes": waiting_minutes,
        "material_minutes": material_minutes,
        "travel_minutes": travel_minutes,
        "documentation_minutes": documentation_minutes,
        "extra_minutes": extra_minutes,
        "deviation_reason": data.get("deviation_reason"),
        "note": data.get("note"),
    }


def require_review_permission(user: User) -> None:
    if not (user.is_admin or user.is_foreman):
        abort(403)


def apply_time_review(entry: TimeEntry) -> None:
    task = entry.task_template
    entry.review_level = ReviewLevel.GREEN.value
    if task is None or task.suggested_minutes <= 0:
        entry.review_status = TimeReviewStatus.OK.value
        return
    difference = abs(entry.minutes - task.suggested_minutes)
    percent = difference / task.suggested_minutes * 100
    entry.deviation_minutes = difference
    entry.deviation_percent = percent
    if difference > settings.get(SettingKeys.TIME_DEVIATION_MINUTES) or percent > settings.get(SettingKeys.TIME_DEVIATION_PERCENT):
        entry.review_status = TimeReviewStatus.NEEDS_REVIEW.value
        entry.review_level = ReviewLevel.YELLOW.value if entry.deviation_reason or entry.note else ReviewLevel.RED.value
    else:
        entry.review_status = TimeReviewStatus.OK.value
        entry.review_level = ReviewLevel.GREEN.value


def apply_material_review(movement: InventoryMovement) -> None:
    movement.review_status = TimeReviewStatus.OK.value
    movement.review_level = ReviewLevel.GREEN.value
    if movement.movement_type != InventoryMovementType.WITHDRAWAL.value:
        return
    has_reason = bool(movement.deviation_reason or movement.note)
    if movement.usage_status != MaterialUsageStatus.INSTALLED.value:
        movement.review_status = TimeReviewStatus.NEEDS_REVIEW.value
        movement.review_level = ReviewLevel.YELLOW.value if has_reason else ReviewLevel.RED.value
        return
    task = movement.task_template
    if task is None:
        movement.review_status = TimeReviewStatus.NEEDS_REVIEW.value
        movement.review_level = ReviewLevel.YELLOW.value if has_reason else ReviewLevel.RED.value
        return
    expectations = list(task.expected_materials)
    if not expectations:
        return
    matching = [item for item in expectations if item.item_id == movement.item_id]
    if not matching:
        movement.review_status = TimeReviewStatus.NEEDS_REVIEW.value
        movement.review_level = ReviewLevel.YELLOW.value if has_reason else ReviewLevel.RED.value
        return
    expected_quantity = sum(item.quantity for item in matching)
    if movement.quantity > expected_quantity + 0.0001:
        movement.review_status = TimeReviewStatus.NEEDS_REVIEW.value
        movement.review_level = ReviewLevel.YELLOW.value if has_reason else ReviewLevel.RED.value


def commission_history_entries(
    commission: Commission,
) -> list[AuditLog]:
    entries_by_id: dict[int, AuditLog] = {}

    direct_entries = AuditLog.query.filter_by(
        entity_type="commission",
        entity_id=str(commission.id),
    ).all()

    for entry in direct_entries:
        entries_by_id[entry.id] = entry

    related_actions = [
        "inventory.withdrawal",
        "inventory.cancel_withdrawal",
        "time.update",
        "time.delete",
    ]

    related_entries = AuditLog.query.filter(
        AuditLog.action.in_(
            related_actions
        )
    ).all()

    for entry in related_entries:
        metadata = entry.metadata_json or {}

        try:
            related_commission_id = int(
                metadata.get(
                    "commission_id",
                    0,
                )
            )
        except (TypeError, ValueError):
            continue

        if related_commission_id == commission.id:
            entries_by_id[entry.id] = entry

    time_entry_ids = [
        str(item.id)
        for item in TimeEntry.query.filter_by(
            commission_id=commission.id
        ).all()
    ]

    if time_entry_ids:
        time_entries = AuditLog.query.filter(
            AuditLog.entity_type
            == "time_entry",
            AuditLog.entity_id.in_(
                time_entry_ids
            ),
        ).all()

        for entry in time_entries:
            entries_by_id[entry.id] = entry

    movement_ids = [
        str(item.id)
        for item in InventoryMovement.query.filter_by(
            commission_id=commission.id
        ).all()
    ]

    if movement_ids:
        movement_entries = AuditLog.query.filter(
            AuditLog.entity_type
            == "inventory_movement",
            AuditLog.entity_id.in_(
                movement_ids
            ),
        ).all()

        for entry in movement_entries:
            entries_by_id[entry.id] = entry

    return sorted(
        entries_by_id.values(),
        key=lambda item: (
            item.created_at,
            item.id,
        ),
        reverse=True,
    )


def serialize_history_entry(
    entry: AuditLog,
) -> dict[str, Any]:
    return {
        "id": entry.id,
        "action": entry.action,
        "title": history_title(entry),
        "detail": history_detail(entry),
        "actor": (
            serialize_user(entry.actor)
            if entry.actor
            else None
        ),
        "actor_name": (
            entry.actor.display_name
            if entry.actor
            else "System"
        ),
        "created_at": iso_datetime(
            entry.created_at
        ),
    }


def history_title(
    entry: AuditLog,
) -> str:
    titles = {
        "commission.create.web": "Auftrag im Web erfasst",
        "commission.create.ios": "Auftrag in der App erfasst",
        "commission.start": "Auftrag angenommen",
        "commission.complete": "Auftragsabschluss übermittelt",
        "inventory.withdrawal": "Material gebucht",
        "inventory.cancel_withdrawal": "Materialbuchung storniert",
        "time.create": "Zeit erfasst",
        "time.update": "Zeiterfassung geändert",
        "time.delete": "Zeiterfassung gelöscht",
        "time.review": "Zeiterfassung geprüft",
        "material.review": "Materialbuchung geprüft",
        "erp.outbox.succeeded": "Mit Wowiport synchronisiert",
        "erp.outbox.failed": "Wowiport-Synchronisierung fehlgeschlagen",
        "webhook.wowiport": "Änderung aus Wowiport übernommen",
    }

    return titles.get(
        entry.action,
        entry.action,
    )


def history_detail(
    entry: AuditLog,
) -> str | None:
    metadata = entry.metadata_json or {}
    action = entry.action

    if action == "commission.complete":
        completed = bool(
            metadata.get("completed")
        )
        tenant_charge = bool(
            metadata.get("tenant_charge")
        )
        reason = str(
            metadata.get("reason") or ""
        ).strip()

        if not completed:
            return (
                f"Nicht abgeschlossen: {reason}"
                if reason
                else "Nicht abgeschlossen"
            )

        if tenant_charge:
            return (
                "Abgeschlossen · "
                "Mieterbelastung angefordert"
            )

        return "Erfolgreich abgeschlossen"

    if action.startswith("inventory."):
        quantity = metadata.get("quantity")

        if quantity is not None:
            return f"Menge: {quantity}"

    if action.startswith("time."):
        minutes = metadata.get("minutes")
        decision = metadata.get("decision")

        values = []

        if minutes is not None:
            values.append(
                f"{minutes} Minuten"
            )

        if decision == "approve":
            values.append("Freigegeben")
        elif decision == "reject":
            values.append("Abgelehnt")

        return (
            " · ".join(values)
            if values
            else None
        )

    if action.startswith("erp.outbox."):
        operation_type = str(
            metadata.get(
                "operation_type",
                "",
            )
        )
        error = str(
            metadata.get("error") or ""
        ).strip()

        operation_titles = {
            "create_commission": "Auftrag erstellt",
            "accept_commission": "Auftrag angenommen",
            "refuse_commission": "Auftrag storniert",
            "cancel_commission": "Auftrag storniert",
            "complete_commission": "Auftrag abgeschlossen",
            "await_invoice": "Warte auf Rechnung",
        }

        operation_title = operation_titles.get(
            operation_type,
            operation_type,
        )

        if error:
            return (
                f"{operation_title}: {error}"
            )

        return operation_title or None

    if action == "webhook.wowiport":
        webhook_action = str(
            metadata.get("action") or ""
        ).strip()

        return webhook_action or None

    return None


def serialize_user(user: User) -> dict[str, Any]:
    return {"id": user.id, "email": user.email, "display_name": user.display_name, "craftsman_erp_id": user.craftsman_erp_id, "is_admin": user.is_admin, "is_foreman": user.is_foreman}


def serialize_commission(commission: Commission, include_details: bool = False, viewer: User | None = None) -> dict[str, Any]:
    item = {
        "id": commission.id,
        "erp_id": commission.erp_id,
        "erp_idnum": commission.erp_idnum,
        "code": commission.code,
        "title": commission.title,
        "description": commission.description,
        "damage_location": commission.damage_location,
        "contact_person": commission.contact_person,
        "status_id": commission.status_id,
        "status_code": commission.status_code,
        "local_status": commission.local_status,
        "craftsman_erp_id": commission.craftsman_erp_id,
        "use_unit_id": commission.use_unit_id,
        "license_agreement_id": commission.license_agreement_id,
        "apartment_address": commission.apartment_address,
        "apartment_location": commission.apartment_location,
        "tenant_name": commission.tenant_name,
        "assigned_user": serialize_user(commission.user) if commission.user else None,
        "recording_date": iso_datetime(commission.recording_date),
        "release_date": iso_datetime(commission.release_date),
        "execution_from": iso_datetime(commission.execution_from),
        "execution_to": iso_datetime(commission.execution_to),
        "completed_at": iso_datetime(commission.completed_at),
        "latitude": commission.latitude,
        "longitude": commission.longitude,
        "has_coordinates": commission.latitude is not None and commission.longitude is not None,
        "public_id": commission.public_id,
        "source": commission.source,
        "erp_sync_state": commission.erp_sync_state,
        "erp_sync_message": commission.erp_sync_message,
        "commission_type_id": commission.commission_type_erp_id,
        "is_actionable": commission.is_actionable,
        "display_title": commission.display_title,
        "completion_successful": commission.completion_successful,
        "tenant_charge_requested": commission.tenant_charge_requested,
        "completion_reason": commission.completion_reason,
        "positions": [
            {
                "id": position.id,
                "erp_id": position.erp_id,
                "position_index": position.position_index,
                "position_number": position.position_number,
                "code": position.code,
                "commission_text": position.commission_text,
                "internal_description": position.internal_description,
                "description": position.description,
                "display_text": position.display_text,
                "units": position.units,
                "service_catalog_id": position.service_catalog_erp_id,
                "craft_activity_id": position.craft_activity_erp_id,
            }
            for position in commission.positions
        ],
    }

    if viewer is not None:
        item["is_own"] = commission.user_id == viewer.id
        item["can_edit"] = can_edit_commission(commission, viewer)

    if include_details:
        item["raw_payload"] = commission.raw_payload

    return item


def serialize_inventory_item(item: InventoryItem) -> dict[str, Any]:
    return {"id": item.id, "item_number": item.item_number, "name": item.name, "unit": item.unit, "ean": item.ean, "qr_code_payload": item.qr_code_payload, "stock_quantity": item.stock_quantity, "reorder_level": item.reorder_level}


def serialize_inventory_movement(
    item: InventoryMovement,
    cancelled_ids: set[int] | None = None,
    can_edit: bool = False,
) -> dict[str, Any]:
    cancelled_ids = cancelled_ids or set()
    is_cancelled = item.id in cancelled_ids
    can_cancel = (
        can_edit
        and item.movement_type == InventoryMovementType.WITHDRAWAL.value
        and not is_cancelled
    )

    return {
        "id": item.id,
        "item": serialize_inventory_item(item.item),
        "user": serialize_user(item.user) if item.user else None,
        "commission_id": item.commission_id,
        "task_template": serialize_time_task(item.task_template) if item.task_template else None,
        "movement_type": item.movement_type,
        "quantity": item.quantity,
        "source": item.source,
        "usage_status": item.usage_status,
        "deviation_reason": item.deviation_reason,
        "note": item.note,
        "review_status": item.review_status,
        "review_level": item.review_level,
        "occurred_at": iso_datetime(item.occurred_at),
        "is_cancelled": is_cancelled,
        "can_cancel": can_cancel,
    }


def serialize_time_task(
    item: TimeTaskTemplate | None,
    include_suggested: bool = True,
) -> dict[str, Any] | None:
    if item is None:
        return None

    return {
        "id": item.id,
        "code": item.code,
        "title": item.title,
        "suggested_minutes": item.suggested_minutes,
        "service_catalog_id": (
            item.erp_service_catalogue_id
        ),
        "erp_idnum": item.erp_idnum,
        "craft_activity": (
            serialize_craft_activity(
                item.craft_activity
            )
            if item.craft_activity
            else None
        ),
        "expected_materials": [
            serialize_expected_material(material)
            for material in item.expected_materials
        ],
    }


def serialize_expected_material(item: TimeTaskExpectedMaterial) -> dict[str, Any]:
    return {
        "id": item.id,
        "task_template_id": item.task_template_id,
        "item": serialize_inventory_item(item.item),
        "quantity": item.quantity,
        "required": item.required,
        "note": item.note,
    }


def serialize_craft_activity(item: CraftActivity) -> dict[str, Any]:
    return {
        "id": item.id,
        "erp_id": item.erp_id,
        "code": item.code,
        "name": item.name,
    }


def serialize_time_entry(item: TimeEntry, viewer: User | None = None) -> dict[str, Any]:
    can_edit = False
    if viewer is not None:
        can_edit = can_edit_commission(item.commission, viewer) and can_edit_time_entry(item, viewer)
    return {
        "id": item.id,
        "commission_id": item.commission_id,
        "user": serialize_user(item.user),
        "task_template": serialize_time_task(item.task_template) if item.task_template else None,
        "minutes": item.minutes,
        "work_minutes": item.work_minutes,
        "waiting_minutes": item.waiting_minutes,
        "material_minutes": item.material_minutes,
        "travel_minutes": item.travel_minutes,
        "documentation_minutes": item.documentation_minutes,
        "extra_minutes": item.extra_minutes,
        "deviation_reason": item.deviation_reason,
        "note": item.note,
        "deviation_minutes": item.deviation_minutes,
        "deviation_percent": item.deviation_percent,
        "review_status": item.review_status,
        "review_level": item.review_level,
        "created_at": iso_datetime(item.created_at),
        "can_edit": can_edit,
    }
