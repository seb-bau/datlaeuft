from __future__ import annotations

from typing import Any

from flask import Blueprint, abort, current_app, jsonify, request
from flask_jwt_extended import create_access_token, get_jwt_identity, jwt_required
from sqlalchemy import or_

from .audit import audit
from .auth import resolve_microsoft_user, validate_microsoft_id_token
from .extensions import db
from .models import Commission, CommissionLocalStatus, InventoryItem, InventoryMovement, InventoryMovementType, TimeEntry, TimeReviewStatus, TimeTaskTemplate, User, utc_now, CraftActivity
from .settings import SettingKeys, settings
from .utils import iso_datetime
from .wowiport import WowiportService
from .catalog import get_user_visible_time_task, service_position_query_for_user

api_bp = Blueprint("api", __name__, url_prefix="/api")

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
        query = query.filter(Commission.local_status.in_([
            CommissionLocalStatus.OPEN.value,
            CommissionLocalStatus.IN_PROGRESS.value,
        ]))
    elif status in {item.value for item in CommissionLocalStatus}:
        query = query.filter_by(local_status=status)

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


@api_bp.get("/commissions/<int:commission_id>")
@jwt_required()
def commission_detail(commission_id: int) -> Any:
    user = current_api_user()
    commission = viewable_commission(commission_id, user)
    return jsonify({"item": serialize_commission(commission, include_details=True, viewer=user)})


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
    commission.local_status = CommissionLocalStatus.IN_PROGRESS.value
    audit("commission.start", "commission", commission.id)
    db.session.commit()
    return jsonify({"item": serialize_commission(commission)})


@api_bp.post("/commissions/<int:commission_id>/complete")
@jwt_required()
def complete_commission(commission_id: int) -> Any:
    user = current_api_user()
    commission = editable_commission(commission_id, user)
    try:
        WowiportService().mark_commission_done(commission.erp_id)
    except Exception as exc:
        current_app.logger.exception("commission_done_failed erp_id=%s", commission.erp_id)
        return jsonify({"message": "ERP-Status konnte nicht gesetzt werden.", "detail": str(exc)}), 502
    commission.local_status = CommissionLocalStatus.DONE.value
    commission.completed_at = utc_now()
    audit("commission.complete", "commission", commission.id, {"erp_id": commission.erp_id})
    db.session.commit()
    return jsonify({"item": serialize_commission(commission)})


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
    movements = InventoryMovement.query.filter_by(commission_id=commission.id).order_by(InventoryMovement.occurred_at.desc()).all()
    return jsonify({"items": [serialize_inventory_movement(item) for item in movements]})


@api_bp.post("/commissions/<int:commission_id>/materials")
@jwt_required()
def add_commission_material(commission_id: int) -> Any:
    user = current_api_user()
    commission = editable_commission(commission_id, user)
    data = request.get_json(silent=True) or {}
    item = find_inventory_item(str(data.get("code", "")).strip())
    if item is None:
        abort(404)
    quantity = float(data.get("quantity", 1))
    if quantity <= 0:
        abort(400)
    movement = InventoryMovement(item=item, user=user, commission=commission, movement_type=InventoryMovementType.WITHDRAWAL.value, quantity=quantity, note=data.get("note"))
    item.stock_quantity -= quantity
    db.session.add(movement)
    audit("inventory.withdrawal", "inventory_item", item.id, {"commission_id": commission.id, "quantity": quantity})
    db.session.commit()
    return jsonify({"item": serialize_inventory_movement(movement), "inventory_item": serialize_inventory_item(item)}), 201


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
    return jsonify({"items": [serialize_time_entry(item) for item in entries]})


@api_bp.post("/commissions/<int:commission_id>/time")
@jwt_required()
def add_time_entry(commission_id: int) -> Any:
    user = current_api_user()
    commission = editable_commission(commission_id, user)
    data = request.get_json(silent=True) or {}
    minutes = int(data.get("minutes", 0))
    if minutes <= 0:
        abort(400)
    task = None
    if data.get("task_template_id"):
        task = get_user_visible_time_task(user, int(data["task_template_id"]))
        if task is None:
            abort(403)
    entry = TimeEntry(commission=commission, user=user, task_template=task, minutes=minutes, note=data.get("note"))
    apply_time_review(entry)
    db.session.add(entry)
    audit("time.create", "commission", commission.id, {"minutes": minutes, "task_template_id": task.id if task else None})
    db.session.commit()
    return jsonify({"item": serialize_time_entry(entry)}), 201


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
    user = db.session.get(User, int(identity)) if identity else None
    if user is None or not user.is_active:
        abort(401)
    return user


def can_view_colleague_commissions(user: User) -> bool:
    return user.is_admin or user.is_foreman or settings.get(SettingKeys.MOBILE_ALLOW_COLLEAGUE_COMMISSIONS)


def can_edit_commission(commission: Commission, user: User) -> bool:
    return user.is_admin or user.is_foreman or commission.user_id == user.id


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


def apply_time_review(entry: TimeEntry) -> None:
    task = entry.task_template
    if task is None or task.suggested_minutes <= 0:
        entry.review_status = TimeReviewStatus.OK.value
        return
    difference = abs(entry.minutes - task.suggested_minutes)
    percent = difference / task.suggested_minutes * 100
    entry.deviation_minutes = difference
    entry.deviation_percent = percent
    if difference > settings.get(SettingKeys.TIME_DEVIATION_MINUTES) or percent > settings.get(SettingKeys.TIME_DEVIATION_PERCENT):
        entry.review_status = TimeReviewStatus.NEEDS_REVIEW.value
    else:
        entry.review_status = TimeReviewStatus.OK.value


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
    }

    if viewer is not None:
        item["is_own"] = commission.user_id == viewer.id
        item["can_edit"] = can_edit_commission(commission, viewer)

    if include_details:
        item["raw_payload"] = commission.raw_payload

    return item


def serialize_inventory_item(item: InventoryItem) -> dict[str, Any]:
    return {"id": item.id, "item_number": item.item_number, "name": item.name, "unit": item.unit, "ean": item.ean, "qr_code_payload": item.qr_code_payload, "stock_quantity": item.stock_quantity, "reorder_level": item.reorder_level}


def serialize_inventory_movement(item: InventoryMovement) -> dict[str, Any]:
    return {"id": item.id, "item": serialize_inventory_item(item.item), "user": serialize_user(item.user) if item.user else None, "commission_id": item.commission_id, "movement_type": item.movement_type, "quantity": item.quantity, "note": item.note, "occurred_at": iso_datetime(item.occurred_at)}


def serialize_time_task(item: TimeTaskTemplate, include_suggested: bool = False) -> dict[str, Any]:
    data = {
        "id": item.id,
        "code": item.code,
        "title": item.title,
        "craft_activity": serialize_craft_activity(item.craft_activity) if item.craft_activity else None,
    }

    if include_suggested:
        data["suggested_minutes"] = item.suggested_minutes

    return data


def serialize_craft_activity(item: CraftActivity) -> dict[str, Any]:
    return {
        "id": item.id,
        "erp_id": item.erp_id,
        "code": item.code,
        "name": item.name,
    }


def serialize_time_entry(item: TimeEntry) -> dict[str, Any]:
    return {"id": item.id, "commission_id": item.commission_id, "user": serialize_user(item.user), "task_template": serialize_time_task(item.task_template) if item.task_template else None, "minutes": item.minutes, "note": item.note, "deviation_minutes": item.deviation_minutes, "deviation_percent": item.deviation_percent, "review_status": item.review_status, "created_at": iso_datetime(item.created_at)}