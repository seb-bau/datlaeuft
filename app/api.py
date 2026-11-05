from __future__ import annotations

from typing import Any

from flask import (
    Blueprint,
    abort,
    current_app,
    g,
    jsonify,
    request,
    send_file,
)
from flask_jwt_extended import create_access_token, get_jwt_identity, jwt_required
from sqlalchemy import and_, or_
from .media_sync import MEDIA_SOURCE
from uuid import uuid4

from .audit import audit
from .auth import resolve_microsoft_user, validate_microsoft_id_token
from .extensions import db
from .models import (
    AuditLog,
    Commission,
    CommissionLocalStatus,
    CommissionSource,
    CraftActivity,
    InventoryItem,
    InventoryItemBarcode,
    InventoryLocation,
    InventoryMovement,
    InventoryMovementType,
    InventoryStock,
    MaterialSource,
    MaterialUsageStatus,
    ReviewLevel,
    TimeEntry,
    TimeReviewStatus,
    TimeTaskExpectedMaterial,
    TimeTaskTemplate,
    User,
    WowiCommissionType,
    WowiCompanyCode,
    utc_now,
    CommissionPhoto,
    AppointmentRequestStatus,
    AppointmentWindow,
    CommissionAppointmentRequest,
    CommissionAttachment,
    InventoryItemGroup,
    InventoryItemType,
    WorkTimeProfile,
)
from .appointment_service import (
    create_appointment_request,
    parse_manual_appointment,
    serialize_accepted_appointment_from,
    send_appointment_request_email,
)
from .appointment_service import (
    create_appointment_request,
    send_appointment_request_email,
)
from .photo_service import (
    commission_photo_path,
    store_commission_photo,
)
from .work_time_service import (
    work_time_day_row,
)
from .inventory_service import (
    book_inventory_movement,
    get_default_inventory_location,
    get_inventory_location,
    movement_matches_expected_material,
    prepare_inventory_movement,
)
from .settings import SettingKeys, settings
from .utils import iso_datetime
from .wowiport import (
    WowiportService,
)
from .catalog import get_user_visible_time_task, service_position_query_for_user
from .erp_sync import enqueue_erp_operation
from .models import CommissionSyncState, ErpOperationType

from .commission_service import (
    create_pending_commission,
    integer_set
)
from .object_directory import (
    commission_creation_tasks,
    search_commission_objects,
)
from .attachment_service import (
    commission_attachment_path,
)
from .push_preferences import (
    COMMISSION_CHANGE_EVENTS,
    CONFIGURABLE_PUSH_EVENTS,
    PushEventType,
    serialize_push_preferences,
)
from .push_service import (
    register_push_device,
    unregister_push_device,
)
from datetime import date

from .performance_review_service import (
    commission_requires_material_confirmation,
)

from .process_info_service import (
    send_commission_status_email,
)

from .commission_workflow_service import (
    CommissionWorkflowError,
    accept_commission as accept_commission_workflow,
    complete_commission as complete_commission_workflow,
    reject_commission as reject_commission_workflow,
)
import json
from datetime import datetime, time
from zoneinfo import ZoneInfo
from sqlalchemy.exc import IntegrityError
from .commission_service import clean_text, normalize_public_id, required_int
from .models import TicketCraftFallback


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
        "show_service_target_times": settings.get(SettingKeys.MOBILE_SHOW_SERVICE_TARGET_TIMES),
        "show_work_time_target_times": settings.get(SettingKeys.MOBILE_SHOW_WORK_TIME_TARGET_TIMES),
        "time_components_enabled": settings.get(SettingKeys.MOBILE_TIME_COMPONENTS_ENABLED),
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


@api_bp.get("/push/preferences")
@jwt_required()
def push_preferences() -> Any:
    user = current_api_user()
    return jsonify({"preferences": serialize_push_preferences(user)})


@api_bp.patch("/push/preferences")
@jwt_required()
def update_push_preferences() -> Any:
    user = current_api_user()
    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify({"message": "Ungültiger JSON-Body."}), 400

    legacy_fields = {
        "new_commission_enabled": (
            "push_new_commission_enabled",
            (PushEventType.NEW_COMMISSION,),
        ),
        "changed_commission_enabled": (
            "push_changed_commission_enabled",
            COMMISSION_CHANGE_EVENTS,
        ),
    }
    events = data.get("events", {})
    allowed_events = {
        event_type.value for event_type in CONFIGURABLE_PUSH_EVENTS
    }

    if not isinstance(events, dict):
        return jsonify({
            "message": "Mitteilungstypen müssen als Objekt übergeben werden."
        }), 400

    for key, value in events.items():
        if key not in allowed_events or type(value) is not bool:
            return jsonify({
                "message": "Ungültiger Mitteilungstyp oder Wahrheitswert."
            }), 400

    for key in legacy_fields:
        if key in data and type(data[key]) is not bool:
            return jsonify({
                "message": f"{key} muss ein Wahrheitswert sein."
            }), 400

    if not events and not any(key in data for key in legacy_fields):
        return jsonify({
            "message": "Es wurde keine Mitteilungseinstellung übergeben."
        }), 400

    user = (
        User.query.filter_by(id=user.id)
        .with_for_update()
        .populate_existing()
        .one()
    )
    preferences = dict(user.push_event_preferences or {})

    for key, (attribute, event_types) in legacy_fields.items():
        if key in data:
            setattr(user, attribute, data[key])
            for event_type in event_types:
                preferences.pop(event_type.value, None)

    preferences.update(events)
    user.push_event_preferences = preferences
    db.session.commit()
    return jsonify({"preferences": serialize_push_preferences(user)})


@api_bp.put("/push/device")
@jwt_required()
def register_device_for_push() -> Any:
    user = current_api_user()
    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify({"message": "Ungültiger JSON-Body."}), 400

    try:
        device = register_push_device(
            user=user,
            installation_id=str(data.get("installation_id", "")),
            device_token=str(data.get("device_token", "")),
            platform=str(data.get("platform", "ios")),
            app_version=data.get("app_version"),
            device_name=data.get("device_name"),
        )
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"message": str(exc)}), 400

    db.session.commit()
    return jsonify(
        {
            "device": {
                "id": device.id,
                "installation_id": device.installation_id,
                "active": device.active,
            }
        }
    )


@api_bp.delete("/push/device")
@jwt_required()
def unregister_device_for_push() -> Any:
    user = current_api_user()
    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify({"message": "Ungültiger JSON-Body."}), 400

    try:
        device = unregister_push_device(
            user=user,
            installation_id=str(data.get("installation_id", "")),
        )
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"message": str(exc)}), 400

    db.session.commit()
    return jsonify({"unregistered": device is not None})


@api_bp.get("/commissions")
@jwt_required()
def commissions() -> Any:
    user = current_api_user()
    scope = request.args.get("scope", "mine").strip()
    status = request.args.get("status", "").strip()
    user_id_filter = request.args.get("user_id", "").strip()

    query = Commission.query.filter(
        Commission.local_status
        != CommissionLocalStatus.DELETED.value
    )
    allowed_user_ids = (
        viewable_commission_user_ids(user)
    )

    if scope != "all":
        query = query.filter(
            Commission.user_id == user.id
        )
    elif user_id_filter:
        if not user_id_filter.isdigit():
            return jsonify({
                "message": (
                    "Der ausgewählte Handwerker "
                    "ist ungültig."
                ),
            }), 400

        selected_user_id = int(user_id_filter)

        if selected_user_id not in allowed_user_ids:
            return jsonify({
                "message": (
                    "Für diesen Handwerker besteht "
                    "keine Berechtigung."
                ),
            }), 403

        query = query.filter(
            Commission.user_id
            == selected_user_id
        )
    else:
        query = query.filter(
            Commission.user_id.in_(
                allowed_user_ids
            )
        )

    if status == "active":
        query = query.filter(
            Commission.local_status.in_(
                [
                    CommissionLocalStatus
                    .WAITING_FOR_ACCEPTANCE
                    .value,
                    CommissionLocalStatus
                    .OPEN
                    .value,
                    CommissionLocalStatus
                    .IN_PROGRESS
                    .value,
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

    query = query.order_by(
        Commission.execution_from.is_(None).asc(),
        Commission.execution_from.asc(),
        Commission.id.asc(),
    )
    return jsonify({"items": [serialize_commission(item, viewer=user) for item in query.all()]})


@api_bp.get("/commissions/users")
@jwt_required()
def commission_users() -> Any:
    user = current_api_user()
    allowed_user_ids = (
        viewable_commission_user_ids(user)
    )
    colleague_user_ids = (
        allowed_user_ids - {user.id}
    )
    items = [user]

    if colleague_user_ids:
        items.extend(
            User.query
            .filter(
                User.id.in_(
                    colleague_user_ids
                ),
                User.is_active_flag.is_(True),
                User.craftsman_erp_id.isnot(None),
            )
            .order_by(
                User.display_name.asc()
            )
            .all()
        )

    return jsonify({
        "items": [
            serialize_user(item)
            for item in items
        ],
    })


@api_bp.get("/commissions/create-options")
@jwt_required()
def commission_create_options() -> Any:
    current_api_user()

    allowed_type_ids = integer_set(
        settings.get(
            SettingKeys.WOWI_ALLOWED_COMMISSION_TYPE_IDS
        )
    )

    if allowed_type_ids:
        commission_types = (
            WowiCommissionType.query.filter(
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

    company_codes = (
        WowiCompanyCode.query.filter(
            WowiCompanyCode.active.is_(True)
        )
        .order_by(
            WowiCompanyCode.code.asc()
        )
        .all()
    )

    can_create = (
            bool(commission_types)
            and commission_creation_tasks()
            .first() is not None
    )

    return jsonify(
        {
            "can_create": can_create,
            "notify_tenants_default": settings.get(
                SettingKeys.COMMISSION_NOTIFY_TENANTS_DEFAULT
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


def _simple_commission_task(craft_activity, mapping):
    task = mapping.task_template if mapping is not None else None
    if (
        task is None
        or not task.active
        or task.craft_activity_id != craft_activity.id
        or task.erp_service_catalogue_id is None
    ):
        return None
    return task


def _simple_commission_payload(data):
    title = clean_text(data.get("title"))
    commission_text = clean_text(data.get("commission_text"))
    title = title or commission_text
    commission_text = commission_text or title

    if not title or not commission_text:
        raise ValueError("Bitte Kurztitel oder Auftragstext eingeben.")

    if len(title) > 255:
        raise ValueError("Der Kurztitel darf höchstens 255 Zeichen enthalten.")

    contact_person = clean_text(data.get("contact_person"))
    if contact_person and len(contact_person) > 255:
        raise ValueError("Der Ansprechpartner darf höchstens 255 Zeichen enthalten.")

    craft_activity_id = required_int(data, "craft_activity_id", "Gewerk")
    craft_activity = CraftActivity.query.filter(
        CraftActivity.id == craft_activity_id,
        CraftActivity.active.is_(True),
        CraftActivity.app_enabled.is_(True),
        CraftActivity.erp_id.isnot(None),
    ).one_or_none()

    if craft_activity is None:
        raise ValueError("Das ausgewählte Gewerk ist nicht verfügbar.")

    mapping = TicketCraftFallback.query.filter_by(
        craft_activity_id=craft_activity.id,
    ).one_or_none()

    task = _simple_commission_task(craft_activity, mapping)
    if task is None:
        raise ValueError(
            "Für dieses Gewerk fehlt eine gültige Fallback-Leistungsposition "
            "unter KI-Auftragsvorgaben."
        )

    craftsman_user_id = required_int(data, "craftsman_user_id", "Handwerker")
    craftsman = User.query.filter(
        User.id == craftsman_user_id,
        User.is_active_flag.is_(True),
        User.craftsman_erp_id.isnot(None),
    ).one_or_none()

    if craftsman is None or not craftsman.is_active:
        raise ValueError("Der ausgewählte interne Handwerker ist nicht verfügbar.")

    commission_type_id = required_int(data, "commission_type_id", "Auftragsart")
    allowed_type_ids = integer_set(
        settings.get(SettingKeys.WOWI_ALLOWED_COMMISSION_TYPE_IDS)
    )

    commission_type = WowiCommissionType.query.filter(
        WowiCommissionType.erp_id == commission_type_id,
        WowiCommissionType.active.is_(True),
    ).one_or_none()

    if commission_type_id not in allowed_type_ids or commission_type is None:
        raise ValueError("Diese Auftragsart ist nicht freigegeben.")

    mapped_type_ids = integer_set(task.commission_type_erp_ids or [])
    if mapped_type_ids and commission_type_id not in mapped_type_ids:
        raise ValueError(
            "Die Auftragsart ist für die Fallback-Leistungsposition "
            "dieses Gewerks nicht zulässig."
        )

    execution_from = None
    execution_date = clean_text(data.get("execution_date"))

    if execution_date:
        try:
            parsed_date = date.fromisoformat(execution_date)
            if parsed_date.isoformat() != execution_date:
                raise ValueError
        except ValueError as exc:
            raise ValueError("Das Ausführungsdatum ist ungültig.") from exc

        execution_from = datetime.combine(
            parsed_date,
            time.min,
            tzinfo=ZoneInfo("Europe/Berlin"),
        ).isoformat()

    return {
        "client_request_id": data["client_request_id"],
        "object_type": data.get("object_type"),
        "object_id": data.get("object_id"),
        "commission_type_id": commission_type_id,
        "craft_activity_id": craft_activity.id,
        "craftsman_user_id": craftsman.id,
        "assignment_type": "internal",
        "title": title,
        "contact_person": contact_person,
        "notify_tenants": data.get("notify_tenants"),
        "execution_from": execution_from,
        "execution_to": None,
        "positions": [{
            "service_catalog_id": task.erp_service_catalogue_id,
            "commission_text": commission_text,
            "units": 1.0,
        }],
    }


def _existing_simple_commission(public_id, user):
    commission = Commission.query.filter_by(
        public_id=public_id,
    ).one_or_none()

    if commission is not None:
        if (
            commission.created_by_user_id != user.id
            or commission.source != CommissionSource.IOS.value
        ):
            abort(403)

        if commission.local_status == CommissionLocalStatus.DELETED.value:
            abort(409, description="Dieser Auftrag wurde bereits gelöscht.")

    return commission


def _simple_commission_response(commission, user, status):
    return jsonify({
        "item": serialize_commission(
            commission,
            include_details=True,
            viewer=user,
        ),
    }), status


def _remove_simple_commission_photo_files(stored_photos):
    for stored_photo in stored_photos:
        for storage_name in (
            stored_photo.storage_name,
            stored_photo.thumbnail_storage_name,
        ):
            try:
                commission_photo_path(storage_name).unlink(missing_ok=True)
            except OSError:
                current_app.logger.exception(
                    "Auftragsfoto konnte nicht bereinigt werden."
                )


@api_bp.get("/commissions/simple-create-options")
@jwt_required()
def simple_commission_create_options() -> Any:
    user = current_api_user()

    allowed_type_ids = integer_set(
        settings.get(SettingKeys.WOWI_ALLOWED_COMMISSION_TYPE_IDS)
    )

    commission_types = WowiCommissionType.query.filter(
        WowiCommissionType.active.is_(True),
        WowiCommissionType.erp_id.in_(allowed_type_ids),
    ).order_by(WowiCommissionType.code.asc()).all()

    type_ids = {item.erp_id for item in commission_types}

    craftsmen = User.query.filter(
        User.is_active_flag.is_(True),
        User.craftsman_erp_id.isnot(None),
    ).order_by(User.display_name.asc()).all()

    craftsmen = [item for item in craftsmen if item.is_active]

    mappings = {
        item.craft_activity_id: item
        for item in TicketCraftFallback.query.all()
    }

    craft_activities = CraftActivity.query.filter(
        CraftActivity.active.is_(True),
        CraftActivity.app_enabled.is_(True),
        CraftActivity.erp_id.isnot(None),
    ).order_by(CraftActivity.name.asc()).all()

    craft_choices = []

    for craft_activity in craft_activities:
        task = _simple_commission_task(
            craft_activity,
            mappings.get(craft_activity.id),
        )

        mapped_ids = (
            integer_set(task.commission_type_erp_ids or [])
            if task
            else set()
        )

        supported_ids = (
            (type_ids & mapped_ids if mapped_ids else type_ids)
            if task
            else set()
        )

        craft_choices.append({
            "id": craft_activity.id,
            "name": craft_activity.name or craft_activity.code or "Gewerk",
            "fallback_available": task is not None,
            "commission_type_ids": sorted(supported_ids),
        })

    return jsonify({
        "can_create": bool(
            craftsmen
            and any(item["commission_type_ids"] for item in craft_choices)
        ),
        "notify_tenants_default": settings.get(
            SettingKeys.COMMISSION_NOTIFY_TENANTS_DEFAULT
        ),
        "default_craftsman_user_id": (
            user.id
            if any(item.id == user.id for item in craftsmen)
            else None
        ),
        "commission_types": [
            {"id": item.erp_id, "code": item.code}
            for item in commission_types
        ],
        "craft_activities": craft_choices,
        "craftsmen": [
            {"id": item.id, "display_name": item.display_name}
            for item in craftsmen
        ],
    })


@api_bp.post("/commissions/simple")
@jwt_required()
def create_simple_commission() -> Any:
    user = current_api_user()

    try:
        data = json.loads(request.form.get("data", ""))

        if not isinstance(data, dict):
            raise ValueError("Die übermittelten Auftragsdaten sind ungültig.")

        if not clean_text(data.get("client_request_id")):
            raise ValueError("Die Client-Auftragskennung fehlt.")

        public_id = normalize_public_id(data["client_request_id"])
        data["client_request_id"] = public_id

    except (ValueError, TypeError) as exc:
        message = (
            "Die Auftragsdaten sind ungültig."
            if isinstance(exc, json.JSONDecodeError)
            else str(exc)
        )
        return jsonify({"message": message}), 400

    existing = _existing_simple_commission(public_id, user)
    if existing is not None:
        return _simple_commission_response(existing, user, 200)

    stored_photos = []

    try:
        payload = _simple_commission_payload(data)

        commission = create_pending_commission(
            data=payload,
            created_by_user=user,
            source=CommissionSource.IOS,
            allow_assignment_override=True,
        )

        photos = []
        content_hashes = set()

        for upload in request.files.getlist("photos"):
            stored_photo = store_commission_photo(commission, upload)
            stored_photos.append(stored_photo)

            if stored_photo.content_sha256 in content_hashes:
                _remove_simple_commission_photo_files([stored_photo])
                continue

            content_hashes.add(stored_photo.content_sha256)

            photo = CommissionPhoto(
                commission=commission,
                uploaded_by=user,
                source="app",
                context="order",
                external_document_id=f"datlaeuft-photo-{uuid4().hex}",
                external_entity_name="Commission",
                external_entity_id=commission.erp_id,
                original_filename=stored_photo.original_filename,
                storage_name=stored_photo.storage_name,
                thumbnail_storage_name=stored_photo.thumbnail_storage_name,
                mime_type=stored_photo.mime_type,
                size_bytes=stored_photo.size_bytes,
                content_sha256=stored_photo.content_sha256,
                active=True,
                erp_sync_state=CommissionSyncState.PENDING.value,
            )

            db.session.add(photo)
            photos.append(photo)

        db.session.flush()

        audit("commission.create.ios", "commission", commission.id, {
            "public_id": commission.public_id,
            "assigned_user_id": commission.user_id,
            "position_count": len(commission.positions),
            "photo_count": len(photos),
        })

        for photo in photos:
            audit("commission.photo.upload", "commission", commission.id, {
                "photo_id": photo.id,
                "filename": photo.original_filename,
                "caption": photo.caption,
            })

        db.session.commit()

    except IntegrityError:
        db.session.rollback()
        _remove_simple_commission_photo_files(stored_photos)

        existing = _existing_simple_commission(public_id, user)
        if existing is not None:
            return _simple_commission_response(existing, user, 200)

        raise

    except ValueError as exc:
        db.session.rollback()
        _remove_simple_commission_photo_files(stored_photos)
        return jsonify({"message": str(exc)}), 400

    except Exception:
        db.session.rollback()
        _remove_simple_commission_photo_files(stored_photos)
        raise

    return _simple_commission_response(commission, user, 202)


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
                "assigned_user_id": commission.user_id,
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


@api_bp.get("/commissions/create-tasks")
@jwt_required()
def commission_create_tasks() -> Any:
    current_api_user()

    search_text = request.args.get(
        "q",
        "",
    ).strip()

    try:
        limit = int(
            request.args.get(
                "limit",
                100,
            )
        )
    except ValueError:
        limit = 100

    limit = min(
        max(limit, 1),
        250,
    )

    query = (
        commission_creation_tasks()
        .outerjoin(
            CraftActivity,
            TimeTaskTemplate.craft_activity_id
            == CraftActivity.id,
        )
    )

    terms = [
        item
        for item in search_text.split()
        if item
    ]

    for term in terms:
        like_term = f"%{term}%"
        query = query.filter(
            or_(
                TimeTaskTemplate.code.ilike(
                    like_term
                ),
                TimeTaskTemplate.title.ilike(
                    like_term
                ),
                TimeTaskTemplate.erp_idnum.ilike(
                    like_term
                ),
                CraftActivity.code.ilike(
                    like_term
                ),
                CraftActivity.name.ilike(
                    like_term
                ),
            )
        )

    items = (
        query
        .order_by(
            CraftActivity.name.is_(None).asc(),
            CraftActivity.name.asc(),
            TimeTaskTemplate.title.asc(),
        )
        .limit(limit)
        .all()
    )

    return jsonify(
        {
            "items": [
                serialize_time_task(item)
                for item in items
            ]
        }
    )


@api_bp.get("/commission-objects/search")
@jwt_required()
def commission_objects_search() -> Any:
    current_api_user()

    search_text = request.args.get(
        "q",
        "",
    ).strip()

    try:
        limit = int(
            request.args.get(
                "limit",
                30,
            )
        )
    except ValueError:
        limit = 30

    limit = min(
        max(limit, 1),
        50,
    )

    items = search_commission_objects(
        search_text,
        limit=limit,
    )

    return jsonify(
        {
            "items": items,
        }
    )


@api_bp.get("/commissions/<int:commission_id>/contacts")
@jwt_required()
def commission_contacts(commission_id: int) -> Any:
    user = current_api_user()
    commission = viewable_commission(commission_id, user)

    if commission.erp_id is None:
        return jsonify({"contact_items": []}), 200

    try:
        contact_items = WowiportService().get_commission_contact_items(
            commission.erp_id
        )
    except Exception:
        current_app.logger.exception(
            "commission_contacts_failed commission_id=%s erp_id=%s",
            commission.id,
            commission.erp_id,
        )
        contact_items = []

    return jsonify({"contact_items": contact_items}), 200


@api_bp.post("/commissions/<int:commission_id>/start")
@jwt_required()
def start_commission(commission_id: int) -> Any:
    user = current_api_user()
    commission = editable_commission(
        commission_id,
        user,
    )
    db.session.refresh(
        commission,
        with_for_update=True,
    )

    try:
        accept_commission_workflow(
            commission,
            user=user,
        )
    except CommissionWorkflowError as exc:
        db.session.rollback()
        return commission_workflow_error_response(
            exc
        )

    db.session.commit()

    return jsonify({
        "item": serialize_commission(commission),
    }), 202


def commission_workflow_error_response(
    error: CommissionWorkflowError,
) -> tuple[Any, int]:
    payload = {
        "message": error.message,
    }

    if error.code:
        payload["code"] = error.code

    return jsonify(payload), error.status_code


@api_bp.post(
    "/commissions/<int:commission_id>/reject"
)
@jwt_required()
def reject_commission(
    commission_id: int,
) -> Any:
    user = current_api_user()
    commission = editable_commission(
        commission_id,
        user,
    )
    db.session.refresh(
        commission,
        with_for_update=True,
    )

    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        data = {}

    try:
        reason = reject_commission_workflow(
            commission,
            str(data.get("reason") or ""),
            user=user,
        )
    except CommissionWorkflowError as exc:
        db.session.rollback()
        return commission_workflow_error_response(
            exc
        )

    db.session.commit()

    send_commission_status_notification(
        commission,
        event_name="Auftrag abgelehnt",
        reason=reason,
        submitted_by_name=user.display_name,
    )

    return jsonify({
        "item": serialize_commission(
            commission,
            viewer=user,
        )
    }), 202


def send_commission_status_notification(
    commission: Commission,
    *,
    event_name: str,
    reason: str,
    submitted_by_name: str,
) -> None:
    try:
        send_commission_status_email(
            commission,
            event_name=event_name,
            reason=reason,
            submitted_by_name=submitted_by_name,
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "commission_status_email_failed "
            "commission_id=%s event=%s",
            commission.id,
            event_name,
        )


@api_bp.get(
    "/commissions/<int:commission_id>/completion-check"
)
@jwt_required()
def commission_completion_check(
        commission_id: int,
) -> Any:
    user = current_api_user()
    commission = editable_commission(
        commission_id,
        user,
    )
    requires_confirmation = (
        commission_requires_material_confirmation(
            commission
        )
    )

    return jsonify({
        "requires_confirmation": (
            requires_confirmation
        ),
        "message": (
            "Für diesen Auftrag ist eine "
            "Materialbuchung vorgesehen. "
            "Auftrag trotzdem abschließen?"
            if requires_confirmation
            else None
        ),
    })


@api_bp.post(
    "/commissions/<int:commission_id>/complete"
)
@jwt_required()
def complete_commission(
    commission_id: int,
) -> Any:
    user = current_api_user()
    commission = editable_commission(
        commission_id,
        user,
    )
    db.session.refresh(
        commission,
        with_for_update=True,
    )

    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        data = {}

    if (
        "completed" not in data
        or not isinstance(
            data.get("completed"),
            bool,
        )
    ):
        return jsonify({
            "message": (
                "Bitte angeben, ob der Auftrag "
                "abgeschlossen werden konnte."
            )
        }), 400

    completed = data["completed"]
    tenant_charge = bool(
        data.get("tenant_charge", False)
    )

    try:
        reason = complete_commission_workflow(
            commission,
            completed=completed,
            reason=str(
                data.get("reason") or ""
            ),
            tenant_charge=tenant_charge,
            confirm_missing_material=bool(
                data.get(
                    "confirm_missing_material",
                    False,
                )
            ),
            user=user,
        )
    except CommissionWorkflowError as exc:
        db.session.rollback()
        return commission_workflow_error_response(
            exc
        )

    db.session.commit()

    if not completed:
        send_commission_status_notification(
            commission,
            event_name=(
                "Auftrag nicht erfolgreich abgeschlossen"
            ),
            reason=reason,
            submitted_by_name=user.display_name,
        )

    return jsonify({
        "item": serialize_commission(
            commission,
            viewer=user,
        )
    }), 202


@api_bp.get("/appointment-windows")
@jwt_required()
def appointment_windows() -> Any:
    current_api_user()

    items = (
        AppointmentWindow.query
        .filter(
            AppointmentWindow.active.is_(True)
        )
        .order_by(
            AppointmentWindow.sort_order.asc(),
            AppointmentWindow.start_time.asc(),
            AppointmentWindow.name.asc(),
        )
        .all()
    )

    return jsonify({
        "items": [
            serialize_appointment_window(item)
            for item in items
        ],
        "custom_times_enabled": bool(
            settings.get(
                SettingKeys.APPOINTMENT_CUSTOM_TIMES_ENABLED
            )
        ),
        "custom_time_duration_minutes": max(
            settings.get(
                SettingKeys.APPOINTMENT_CUSTOM_TIME_DURATION_MINUTES
            ),
            1,
        ),
    })


@api_bp.get(
    "/commissions/<int:commission_id>/appointments"
)
@jwt_required()
def commission_appointments(
        commission_id: int,
) -> Any:
    user = current_api_user()
    commission = viewable_commission(
        commission_id,
        user,
    )

    items = (
        CommissionAppointmentRequest.query
        .filter_by(
            commission_id=commission.id
        )
        .order_by(
            CommissionAppointmentRequest.created_at.desc()
        )
        .all()
    )

    return jsonify({
        "items": [
            serialize_appointment_request(item)
            for item in items
        ]
    })


@api_bp.post(
    "/commissions/<int:commission_id>/appointments"
)
@jwt_required()
def commission_appointment_create(
        commission_id: int,
) -> Any:
    user = current_api_user()
    commission = editable_commission(
        commission_id,
        user,
    )
    db.session.refresh(commission, with_for_update=True)
    if not commission.is_actionable:
        return jsonify({
            "message": (
                "Terminvorschläge können erst nach "
                "der Wowiport-Bestätigung versendet werden."
            )
        }), 409

    if commission.accepted_appointment_from is not None:
        return jsonify({
            "message": (
                "Der Auftrag besitzt bereits einen "
                "bestätigten Termin."
            )
        }), 409

    payload = request.get_json(
        silent=True
    ) or {}

    try:
        appointment_request, public_token = (
            create_appointment_request(
                commission,
                user,
                payload,
            )
        )

        db.session.add(
            appointment_request
        )
        db.session.flush()

        send_appointment_request_email(
            appointment_request,
            public_token,
        )

        audit(
            "appointment.sent",
            "commission",
            commission.id,
            {
                "appointment_request_id": appointment_request.id,
                "recipient_email": appointment_request.recipient_email,
                "proposal_count": len(
                    appointment_request.proposals
                ),
            },
        )

        db.session.commit()

    except ValueError as exc:
        db.session.rollback()

        return jsonify({
            "message": str(exc)
        }), 400

    except RuntimeError as exc:
        db.session.rollback()

        current_app.logger.exception(
            "appointment_email_failed commission_id=%s",
            commission.id,
        )

        return jsonify({
            "message": str(exc)
        }), 502

    return jsonify({
        "item": serialize_appointment_request(
            appointment_request
        )
    }), 201


@api_bp.post("/commissions/<int:commission_id>/appointments/accepted")
@jwt_required()
def commission_appointment_enter(commission_id: int) -> Any:
    user = current_api_user()
    commission = editable_commission(commission_id, user)
    db.session.refresh(commission, with_for_update=True)
    commission = editable_commission(commission_id, user)

    if not commission.is_actionable:
        return jsonify({
            "message": "Termine können erst nach der Wowiport-Bestätigung eingetragen werden."
        }), 409

    if commission.accepted_appointment_from is not None:
        return jsonify({
            "message": "Der Auftrag besitzt bereits einen bestätigten Termin."
        }), 409

    try:
        appointment_from, has_time, comment = parse_manual_appointment(
            request.get_json(silent=True)
        )
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"message": str(exc)}), 400

    commission.accepted_appointment_from = appointment_from
    commission.accepted_appointment_to = None
    commission.accepted_appointment_comment = comment
    commission.accepted_appointment_has_time = has_time
    commission.accepted_appointment_is_manual = True

    CommissionAppointmentRequest.query.filter(
        CommissionAppointmentRequest.commission_id == commission.id,
        CommissionAppointmentRequest.status == AppointmentRequestStatus.PENDING.value,
    ).update(
        {CommissionAppointmentRequest.status: AppointmentRequestStatus.SUPERSEDED.value},
        synchronize_session=False,
    )

    audit(
        "appointment.accepted",
        "commission",
        commission.id,
        {
            "source": "manual",
            "appointment_from": serialize_accepted_appointment_from(commission),
            "appointment_to": None,
            "has_time": has_time,
            "tenant_comment": comment,
        },
    )
    db.session.commit()

    return jsonify({
        "item": serialize_commission(commission, viewer=user)
    }), 201


@api_bp.delete(
    "/commissions/<int:commission_id>/appointments/accepted"
)
@jwt_required()
def commission_appointment_cancel(
        commission_id: int,
) -> Any:
    user = current_api_user()
    commission = editable_commission(
        commission_id,
        user,
    )
    db.session.refresh(commission, with_for_update=True)
    accepted_request = (
        CommissionAppointmentRequest.query
        .filter(
            CommissionAppointmentRequest.commission_id
            == commission.id,
            CommissionAppointmentRequest.status
            == AppointmentRequestStatus.ACCEPTED.value,
        )
        .order_by(
            CommissionAppointmentRequest.responded_at.desc(),
            CommissionAppointmentRequest.id.desc(),
        )
        .first()
    )

    appointment_from = (
        commission.accepted_appointment_from
    )
    appointment_to = (
        commission.accepted_appointment_to
    )

    if (
            appointment_from is None
            and accepted_request is None
    ):
        return jsonify({
            "item": serialize_commission(
                commission,
                viewer=user,
            )
        })

    if accepted_request is not None:
        accepted_request.status = (
            AppointmentRequestStatus.CANCELLED.value
        )

    commission.accepted_appointment_from = None
    commission.accepted_appointment_to = None
    commission.accepted_appointment_comment = None
    commission.accepted_appointment_has_time = True
    commission.accepted_appointment_is_manual = False

    audit(
        "appointment.cancelled",
        "commission",
        commission.id,
        {
            "appointment_request_id": (
                accepted_request.id
                if accepted_request
                else None
            ),
            "appointment_from": iso_datetime(
                appointment_from
            ),
            "appointment_to": iso_datetime(
                appointment_to
            ),
        },
    )

    db.session.commit()

    return jsonify({
        "item": serialize_commission(
            commission,
            viewer=user,
        )
    })


@api_bp.get(
    "/commissions/<int:commission_id>/attachments"
)
@jwt_required()
def commission_attachments(
        commission_id: int,
) -> Any:
    user = current_api_user()
    commission = viewable_commission(
        commission_id,
        user,
    )

    attachments = (
        CommissionAttachment.query
        .filter(
            CommissionAttachment.commission_id
            == commission.id,
            CommissionAttachment.active.is_(True),
        )
        .order_by(
            CommissionAttachment.created_at.desc(),
            CommissionAttachment.id.desc(),
        )
        .all()
    )

    return jsonify({
        "items": [
            serialize_commission_attachment(
                attachment
            )
            for attachment in attachments
        ]
    })


@api_bp.get(
    "/commissions/<int:commission_id>/attachments/"
    "<int:attachment_id>/content"
)
@jwt_required()
def commission_attachment_content(
        commission_id: int,
        attachment_id: int,
) -> Any:
    user = current_api_user()
    commission = viewable_commission(
        commission_id,
        user,
    )

    attachment = (
        CommissionAttachment.query
        .filter(
            CommissionAttachment.id
            == attachment_id,
            CommissionAttachment.commission_id
            == commission.id,
            CommissionAttachment.active.is_(True),
        )
        .one_or_none()
    )

    if attachment is None:
        abort(404)

    path = commission_attachment_path(
        attachment.storage_name
    )

    if not path.is_file():
        abort(404)

    display_inline = (
            attachment.mime_type.startswith("image/")
            or attachment.mime_type
            == "application/pdf"
    )

    response = send_file(
        path,
        mimetype=attachment.mime_type,
        as_attachment=not display_inline,
        download_name=attachment.original_filename,
        conditional=True,
        max_age=3600,
    )
    response.headers[
        "X-Content-Type-Options"
    ] = "nosniff"

    return response


@api_bp.get(
    "/commissions/<int:commission_id>/attachments/"
    "<int:attachment_id>/thumbnail"
)
@jwt_required()
def commission_attachment_thumbnail(
        commission_id: int,
        attachment_id: int,
) -> Any:
    user = current_api_user()
    commission = viewable_commission(
        commission_id,
        user,
    )

    attachment = (
        CommissionAttachment.query
        .filter(
            CommissionAttachment.id
            == attachment_id,
            CommissionAttachment.commission_id
            == commission.id,
            CommissionAttachment.active.is_(True),
        )
        .one_or_none()
    )

    if attachment is None:
        abort(404)

    thumbnail_storage_name = getattr(
        attachment,
        "thumbnail_storage_name",
        None,
    )

    if not thumbnail_storage_name:
        abort(404)

    path = commission_attachment_path(
        thumbnail_storage_name
    )

    if not path.is_file():
        abort(404)

    response = send_file(
        path,
        mimetype="image/jpeg",
        as_attachment=False,
        conditional=True,
        max_age=3600,
    )
    response.headers[
        "X-Content-Type-Options"
    ] = "nosniff"

    return response


@api_bp.get(
    "/commissions/<int:commission_id>/photos"
)
@jwt_required()
def commission_photos(
        commission_id: int,
) -> Any:
    user = current_api_user()
    commission = viewable_commission(
        commission_id,
        user,
    )

    context_filter = request.args.get(
        "context",
        "",
    ).strip().lower()

    entity_filter = request.args.get(
        "entity",
        "",
    ).strip().lower()

    if context_filter not in {
        "",
        "order",
        "object",
    }:
        return jsonify({
            "message": "Ungültiger Fotokontext."
        }), 400

    if entity_filter not in {
        "",
        "building",
        "use_unit",
    }:
        return jsonify({
            "message": (
                "Ungültiger "
                "Bestandsobjektfilter."
            )
        }), 400

    photos: list[CommissionPhoto] = []

    if (
            context_filter in {"", "order"}
            and not entity_filter
    ):
        photos.extend(
            CommissionPhoto.query
            .filter(
                CommissionPhoto.commission_id
                == commission.id,
                CommissionPhoto.context
                == "order",
                CommissionPhoto.active.is_(
                    True
                ),
            )
            .all()
        )

    if context_filter in {"", "object"}:
        entity_conditions = []

        if (
                entity_filter
                in {"", "building"}
                and commission.building_erp_id
                is not None
        ):
            entity_conditions.append(
                and_(
                    CommissionPhoto
                    .external_entity_name
                    == "Building",
                    CommissionPhoto
                    .external_entity_id
                    == commission
                    .building_erp_id,
                )
            )

        if (
                entity_filter
                in {"", "use_unit"}
                and commission.use_unit_id
                is not None
        ):
            entity_conditions.append(
                and_(
                    CommissionPhoto
                    .external_entity_name
                    == "UseUnit",
                    CommissionPhoto
                    .external_entity_id
                    == commission.use_unit_id,
                )
            )

        if entity_conditions:
            photos.extend(
                CommissionPhoto.query
                .filter(
                    CommissionPhoto
                    .commission_id
                    .is_(None),
                    CommissionPhoto.source
                    == MEDIA_SOURCE,
                    CommissionPhoto.active.is_(
                        True
                    ),
                    or_(
                        *entity_conditions
                    ),
                )
                .all()
            )

    photos.sort(
        key=lambda photo: photo.id,
        reverse=True,
    )

    return jsonify({
        "items": [
            serialize_commission_photo(
                photo,
                commission_id=commission.id,
            )
            for photo in photos
        ]
    })


def viewable_commission_photo(
        commission: Commission,
        photo_id: int,
) -> CommissionPhoto:
    photo = CommissionPhoto.query.filter(
        CommissionPhoto.id == photo_id,
        CommissionPhoto.active.is_(True),
    ).one_or_none()

    if photo is None:
        abort(404)

    if photo.commission_id == commission.id:
        return photo

    if (
            photo.commission_id is None
            and photo.source == MEDIA_SOURCE
            and (
            (
                    photo.external_entity_name
                    == "Building"
                    and photo.external_entity_id
                    == commission.building_erp_id
            )
            or (
                    photo.external_entity_name
                    == "UseUnit"
                    and photo.external_entity_id
                    == commission.use_unit_id
            )
        )
    ):
        return photo

    abort(404)


@api_bp.post(
    "/commissions/<int:commission_id>/photos"
)
@jwt_required()
def commission_photo_upload(
        commission_id: int,
) -> Any:
    user = current_api_user()
    commission = editable_commission(
        commission_id,
        user,
    )

    upload = request.files.get("photo")

    if upload is None:
        return jsonify({
            "message": "Es wurde kein Foto übermittelt."
        }), 400

    caption = (
            request.form.get(
                "caption",
                "",
            ).strip()
            or None
    )

    if caption is not None:
        caption = caption[:500]

    try:
        stored_photo = store_commission_photo(
            commission,
            upload,
        )
    except ValueError as exc:
        return jsonify({
            "message": str(exc)
        }), 400

    existing_photo = (
        CommissionPhoto.query
        .filter(
            CommissionPhoto.commission_id
            == commission.id,
            CommissionPhoto.content_sha256
            == stored_photo.content_sha256,
            CommissionPhoto.active.is_(True),
            CommissionPhoto.context == "order",
        )
        .one_or_none()
    )

    if existing_photo is not None:
        return jsonify({
            "item": serialize_commission_photo(
                existing_photo
            ),
            "duplicate": True,
        }), 200

    external_document_id = (
        "datlaeuft-photo-"
        f"{uuid4().hex}"
    )

    photo = CommissionPhoto(
        commission=commission,
        uploaded_by=user,
        source="app",
        context="order",
        external_document_id=(
            external_document_id
        ),
        external_entity_name="Commission",
        external_entity_id=commission.erp_id,
        original_filename=(
            stored_photo.original_filename
        ),
        storage_name=(
            stored_photo.storage_name
        ),
        thumbnail_storage_name=(
            stored_photo.thumbnail_storage_name
        ),
        mime_type=stored_photo.mime_type,
        size_bytes=stored_photo.size_bytes,
        content_sha256=(
            stored_photo.content_sha256
        ),
        caption=caption,
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
        },
    )

    db.session.commit()

    return jsonify({
        "item": serialize_commission_photo(photo),
        "duplicate": False,
    }), 201


@api_bp.patch(
    "/commissions/<int:commission_id>/photos/<int:photo_id>"
)
@jwt_required()
def commission_photo_update(
        commission_id: int,
        photo_id: int,
) -> Any:
    user = current_api_user()
    commission = editable_commission(
        commission_id,
        user,
    )

    photo = (
        CommissionPhoto.query
        .filter(
            CommissionPhoto.id == photo_id,
            CommissionPhoto.commission_id
            == commission.id,
            CommissionPhoto.active.is_(True),
        )
        .one_or_none()
    )

    if photo is None:
        abort(404)

    data = request.get_json(
        silent=True
    ) or {}

    caption = str(
        data.get("caption") or ""
    ).strip()

    photo.caption = (
        caption[:500]
        if caption
        else None
    )

    audit(
        "commission.photo.update",
        "commission",
        commission.id,
        {
            "photo_id": photo.id,
            "caption": photo.caption,
        },
    )

    db.session.commit()

    return jsonify({
        "item": serialize_commission_photo(photo)
    })


@api_bp.delete(
    "/commissions/<int:commission_id>/photos/<int:photo_id>"
)
@jwt_required()
def commission_photo_delete(
        commission_id: int,
        photo_id: int,
) -> Any:
    user = current_api_user()
    commission = editable_commission(
        commission_id,
        user,
    )

    photo = (
        CommissionPhoto.query
        .filter(
            CommissionPhoto.id == photo_id,
            CommissionPhoto.commission_id
            == commission.id,
            CommissionPhoto.active.is_(True),
        )
        .one_or_none()
    )

    if photo is None:
        abort(404)

    if (
            photo.erp_sync_state
            == CommissionSyncState.SYNCED.value
            and photo.external_id
    ):
        return jsonify({
            "message": (
                "Das Foto wurde bereits an Wowiport "
                "übertragen und kann nur dort entfernt werden."
            )
        }), 409

    photo.active = False
    photo.deleted_at = utc_now()

    audit(
        "commission.photo.delete",
        "commission",
        commission.id,
        {
            "photo_id": photo.id,
            "filename": photo.original_filename,
        },
    )

    db.session.commit()

    return jsonify({
        "message": "Foto entfernt."
    })


@api_bp.get(
    "/commissions/<int:commission_id>/photos/<int:photo_id>/content"
)
@jwt_required()
def commission_photo_content(
        commission_id: int,
        photo_id: int,
) -> Any:
    user = current_api_user()
    commission = viewable_commission(
        commission_id,
        user,
    )

    photo = viewable_commission_photo(
        commission=commission,
        photo_id=photo_id,
    )

    if photo is None:
        abort(404)

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


@api_bp.get(
    "/commissions/<int:commission_id>/photos/<int:photo_id>/thumbnail"
)
@jwt_required()
def commission_photo_thumbnail(
        commission_id: int,
        photo_id: int,
) -> Any:
    user = current_api_user()
    commission = viewable_commission(
        commission_id,
        user,
    )

    photo = viewable_commission_photo(
        commission=commission,
        photo_id=photo_id,
    )

    if photo is None:
        abort(404)

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


@api_bp.get("/inventory/locations")
@jwt_required()
def inventory_locations() -> Any:
    current_api_user()

    locations = (
        InventoryLocation.query
        .filter(InventoryLocation.active.is_(True))
        .order_by(
            InventoryLocation.is_default.desc(),
            InventoryLocation.name.asc(),
        )
        .all()
    )

    return jsonify({
        "items": [
            serialize_inventory_location(location)
            for location in locations
        ]
    })


@api_bp.get("/inventory/lookup")
@jwt_required()
def inventory_lookup() -> Any:
    current_api_user()

    code = request.args.get("code", "").strip()
    item = find_inventory_item(code)

    if item is None:
        abort(404)

    return jsonify({
        "item": serialize_inventory_item(item)
    })


@api_bp.get("/inventory/search")
@jwt_required()
def inventory_search() -> Any:
    current_api_user()

    search_text = request.args.get(
        "q",
        "",
    ).strip()
    raw_item_type_id = request.args.get(
        "item_type_id",
        "",
    ).strip()

    item_type_id = None

    if raw_item_type_id:
        try:
            item_type_id = int(
                raw_item_type_id
            )
        except ValueError:
            return jsonify({
                "message": (
                    "Die Artikelart ist ungültig."
                )
            }), 400

    if (
            len(search_text) < 2
            and item_type_id is None
    ):
        return jsonify({
            "items": []
        })

    try:
        limit = int(
            request.args.get(
                "limit",
                12,
            )
        )
    except ValueError:
        limit = 12

    limit = min(
        max(limit, 1),
        50,
    )

    query = (
        InventoryItem.query
        .outerjoin(
            InventoryItemType,
            InventoryItem.item_type_id
            == InventoryItemType.id,
        )
        .outerjoin(
            InventoryItemGroup,
            InventoryItemType.group_id
            == InventoryItemGroup.id,
        )
        .filter(
            InventoryItem.active.is_(True)
        )
    )

    if item_type_id is not None:
        query = query.filter(
            InventoryItem.item_type_id
            == item_type_id
        )

    for term in [
        item.strip()
        for item in search_text.split()
        if item.strip()
    ]:
        like_term = f"%{term}%"

        barcode_item_ids = (
            db.session.query(
                InventoryItemBarcode.item_id
            )
            .filter(
                InventoryItemBarcode.active.is_(True),
                InventoryItemBarcode.code.ilike(
                    like_term
                ),
            )
        )

        query = query.filter(
            or_(
                InventoryItem.name.ilike(
                    like_term
                ),
                InventoryItem.item_number.ilike(
                    like_term
                ),
                InventoryItem.ean.ilike(
                    like_term
                ),
                InventoryItem.qr_code_payload.ilike(
                    like_term
                ),
                InventoryItemType.name.ilike(
                    like_term
                ),
                InventoryItemType.code.ilike(
                    like_term
                ),
                InventoryItemGroup.name.ilike(
                    like_term
                ),
                InventoryItemGroup.code.ilike(
                    like_term
                ),
                InventoryItem.id.in_(
                    barcode_item_ids
                ),
            )
        )

    items = (
        query
        .order_by(
            InventoryItem.name.asc(),
            InventoryItem.item_number.asc(),
        )
        .limit(limit)
        .all()
    )

    return jsonify({
        "items": [
            serialize_inventory_item(item)
            for item in items
        ]
    })


@api_bp.get("/inventory/movements")
@jwt_required()
def inventory_movements() -> Any:
    user = current_api_user()

    movements = (
        InventoryMovement.query
        .filter(
            InventoryMovement.user_id == user.id,
            InventoryMovement.commission_id.is_(None),
        )
        .order_by(
            InventoryMovement.occurred_at.desc()
        )
        .limit(query_limit())
        .all()
    )

    return jsonify({
        "items": [
            serialize_inventory_movement(item)
            for item in movements
        ]
    })


@api_bp.post("/inventory/transfer")
@jwt_required()
def inventory_transfer() -> Any:
    user = current_api_user()
    data = request.get_json(silent=True) or {}

    item = find_inventory_item(
        str(data.get("code") or "").strip()
    )

    if item is None:
        return jsonify({
            "message": "Der Artikel wurde nicht gefunden."
        }), 404

    try:
        quantity = float(data.get("quantity", 0))
        source_location = get_inventory_location(
            int(data.get("source_location_id"))
        )
        target_location = get_inventory_location(
            int(data.get("target_location_id"))
        )

        movement = InventoryMovement(
            item=item,
            user=user,
            movement_type=InventoryMovementType.TRANSFER.value,
            quantity=quantity,
            source=(
                MaterialSource.VEHICLE.value
                if source_location.location_type == "vehicle"
                else MaterialSource.WAREHOUSE.value
            ),
            usage_status=(
                MaterialUsageStatus.IN_VEHICLE.value
                if target_location.location_type == "vehicle"
                else MaterialUsageStatus.NOT_INSTALLED.value
            ),
            note=(
                    str(data.get("note") or "").strip()
                    or None
            ),
            source_location=source_location,
            target_location=target_location,
            review_status=TimeReviewStatus.OK.value,
            review_level=ReviewLevel.GREEN.value,
        )

        book_inventory_movement(movement)

    except (TypeError, ValueError) as exc:
        db.session.rollback()
        return jsonify({
            "message": str(exc)
        }), 400

    audit(
        "inventory.transfer",
        "inventory_item",
        item.id,
        {
            "quantity": quantity,
            "source_location_id": source_location.id,
            "target_location_id": target_location.id,
        },
    )

    db.session.commit()

    return jsonify({
        "item": serialize_inventory_movement(movement),
        "inventory_item": serialize_inventory_item(item),
    }), 201


@api_bp.post("/inventory/receipt")
@jwt_required()
def inventory_receipt() -> Any:
    user = current_api_user()
    data = request.get_json(silent=True) or {}

    item = find_inventory_item(
        str(data.get("code") or "").strip()
    )

    if item is None:
        return jsonify({
            "message": "Der Artikel wurde nicht gefunden."
        }), 404

    try:
        quantity = float(data.get("quantity", 0))
        target_location = get_inventory_location(
            int(data.get("target_location_id"))
        )

        movement = InventoryMovement(
            item=item,
            user=user,
            movement_type=InventoryMovementType.STORED.value,
            quantity=quantity,
            source=MaterialSource.DIRECT_PURCHASE.value,
            usage_status=(
                MaterialUsageStatus.IN_VEHICLE.value
                if target_location.location_type == "vehicle"
                else MaterialUsageStatus.NOT_INSTALLED.value
            ),
            note=(
                    str(data.get("note") or "").strip()
                    or None
            ),
            target_location=target_location,
            review_status=TimeReviewStatus.OK.value,
            review_level=ReviewLevel.GREEN.value,
        )

        book_inventory_movement(movement)

    except (TypeError, ValueError) as exc:
        db.session.rollback()
        return jsonify({
            "message": str(exc)
        }), 400

    audit(
        "inventory.direct_purchase",
        "inventory_item",
        item.id,
        {
            "quantity": quantity,
            "target_location_id": target_location.id,
        },
    )

    db.session.commit()

    return jsonify({
        "item": serialize_inventory_movement(movement),
        "inventory_item": serialize_inventory_item(item),
    }), 201


@api_bp.get("/commissions/<int:commission_id>/materials")
@jwt_required()
def commission_materials(commission_id: int) -> Any:
    user = current_api_user()
    commission = viewable_commission(
        commission_id,
        user,
    )
    cancelled_ids = cancelled_inventory_movement_ids(
        commission.id
    )

    movements = (
        InventoryMovement.query
        .filter_by(commission_id=commission.id)
        .order_by(
            InventoryMovement.occurred_at.desc()
        )
        .all()
    )

    return jsonify({
        "items": [
            serialize_inventory_movement(
                item,
                cancelled_ids=cancelled_ids,
                can_edit=can_edit_commission(
                    commission,
                    user,
                ),
            )
            for item in movements
        ]
    })


@api_bp.post("/commissions/<int:commission_id>/materials")
@jwt_required()
def add_commission_material(
        commission_id: int,
) -> Any:
    user = current_api_user()
    commission = editable_commission(
        commission_id,
        user,
    )
    data = request.get_json(silent=True) or {}

    item = find_inventory_item(
        str(data.get("code", "")).strip()
    )

    if item is None:
        abort(404)

    try:
        quantity = float(data.get("quantity", 1))
    except (TypeError, ValueError):
        abort(400)

    if quantity <= 0:
        abort(400)

    task_template_id = data.get(
        "task_template_id"
    )

    if task_template_id in (None, ""):
        abort(
            400,
            description=(
                "Bitte eine Leistungsposition "
                "auswählen."
            ),
        )

    try:
        task_template_id = int(
            task_template_id
        )
    except (TypeError, ValueError):
        abort(
            400,
            description=(
                "Die Leistungsposition ist ungültig."
            ),
        )

    task = get_user_visible_time_task(
        user,
        task_template_id,
    )

    if task is None:
        abort(403)

    source = str(
        data.get("source")
        or MaterialSource.WAREHOUSE.value
    ).strip()

    if source not in {
        item.value
        for item in MaterialSource
    }:
        abort(400)

    usage_status = str(
        data.get("usage_status")
        or MaterialUsageStatus.INSTALLED.value
    ).strip()

    if usage_status not in {
        item.value
        for item in MaterialUsageStatus
    }:
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
        serial_number=(
                str(
                    data.get(
                        "serial_number"
                    )
                    or ""
                ).strip()
                or None
        ),
    )

    try:
        if source == MaterialSource.WAREHOUSE.value:
            movement.source_location = (
                get_inventory_location(
                    int(data["location_id"])
                )
                if data.get("location_id")
                else get_default_inventory_location()
            )
            book_inventory_movement(movement)

        elif source == MaterialSource.VEHICLE.value:
            if not data.get("location_id"):
                raise ValueError(
                    "Bei der Quelle „Fahrzeug“ muss ein Fahrzeug-Lagerort ausgewählt werden."
                )

            movement.source_location = (
                get_inventory_location(
                    int(data["location_id"])
                )
            )
            book_inventory_movement(movement)
        else:
            prepare_inventory_movement(
                movement
            )
            db.session.add(
                movement
            )

    except (TypeError, ValueError) as exc:
        db.session.rollback()

        return jsonify({
            "message": str(exc)
        }), 400

    with db.session.no_autoflush:
        apply_material_review(movement)

    audit(
        "inventory.withdrawal",
        "inventory_item",
        item.id,
        {
            "commission_id": commission.id,
            "quantity": quantity,
            "task_template_id": (
                task.id
                if task
                else None
            ),
            "source": source,
            "source_location_id": (
                movement.source_location_id
            ),
        },
    )

    db.session.commit()

    return jsonify({
        "item": serialize_inventory_movement(movement),
        "inventory_item": serialize_inventory_item(item),
    }), 201


@api_bp.post("/commissions/<int:commission_id>/materials/<int:movement_id>/cancel")
@jwt_required()
def cancel_commission_material(
        commission_id: int,
        movement_id: int,
) -> Any:
    user = current_api_user()
    commission = editable_commission(
        commission_id,
        user,
    )

    movement = (
        InventoryMovement.query
        .filter_by(
            id=movement_id,
            commission_id=commission.id,
        )
        .one_or_none()
    )

    if movement is None:
        abort(404)

    if movement.movement_type != InventoryMovementType.WITHDRAWAL.value:
        return jsonify({
            "message": "Nur Entnahmen können storniert werden."
        }), 400

    if movement.id in cancelled_inventory_movement_ids(
            commission.id
    ):
        return jsonify({
            "message": "Diese Materialbuchung wurde bereits storniert."
        }), 409

    item = movement.item
    note_parts = [
        f"{CANCELLATION_NOTE_PREFIX}{movement.id}"
    ]

    if movement.note:
        note_parts.append(
            f"Originalnotiz: {movement.note}"
        )

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
        serial_number=movement.serial_number,
        item_type_id_snapshot=(
            movement.item_type_id_snapshot
        ),
        item_group_id_snapshot=(
            movement.item_group_id_snapshot
        ),
    )

    try:
        if (
                movement.source_location is not None
                or movement.source == MaterialSource.WAREHOUSE.value
        ):
            cancellation.target_location = (
                    movement.source_location
                    or get_default_inventory_location()
            )
            book_inventory_movement(cancellation)
        else:
            prepare_inventory_movement(
                cancellation
            )
            db.session.add(
                cancellation
            )

    except ValueError as exc:
        db.session.rollback()
        return jsonify({
            "message": str(exc)
        }), 400

    audit(
        "inventory.cancel_withdrawal",
        "inventory_item",
        item.id,
        {
            "commission_id": commission.id,
            "movement_id": movement.id,
            "quantity": movement.quantity,
            "target_location_id": (
                cancellation.target_location_id
            ),
        },
    )

    db.session.commit()

    cancelled_ids = cancelled_inventory_movement_ids(
        commission.id
    )

    return jsonify({
        "item": serialize_inventory_movement(
            cancellation,
            cancelled_ids=cancelled_ids,
            can_edit=can_edit_commission(
                commission,
                user,
            ),
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
        CraftActivity.name.is_(None).asc(),
        CraftActivity.name.asc(),
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
        CraftActivity.name.is_(None).asc(),
        CraftActivity.name.asc(),
        TimeTaskTemplate.title.asc(),
        TimeTaskTemplate.code.asc(),
    ).limit(limit).all()

    return jsonify({"items": [serialize_time_task(item) for item in items]})


@api_bp.get("/my-time")
@jwt_required()
def my_time() -> Any:
    user = current_api_user()
    selected_date = api_date_value(
        request.args.get(
            "date",
            date.today().isoformat(),
        ),
        "Das ausgewählte Datum ist ungültig.",
    )
    day_row = work_time_day_row(
        user,
        selected_date,
    )
    entries = (
        TimeEntry.query
        .filter_by(
            user_id=user.id,
            service_date=selected_date,
        )
        .order_by(
            TimeEntry.created_at.desc(),
            TimeEntry.id.desc(),
        )
        .all()
    )
    is_future = selected_date > date.today()

    return jsonify({
        "date": selected_date.isoformat(),
        "expected_minutes": day_row[
            "expected_minutes"
        ],
        "recorded_minutes": day_row[
            "recorded_minutes"
        ],
        "difference_minutes": (
            None
            if is_future
            else day_row["difference_minutes"]
        ),
        "status": (
            "not_evaluated"
            if is_future
            else day_row["status"]
        ),
        "profile": (
            serialize_work_time_profile(
                day_row["profile"]
            )
            if day_row["profile"] is not None
            else None
        ),
        "target_override": (
            {
                "target_minutes": (
                    day_row["override"].target_minutes
                ),
                "reason": day_row["override"].reason,
            }
            if day_row["override"] is not None
            else None
        ),
        "items": [
            serialize_time_entry(
                item,
                viewer=user,
            )
            for item in entries
        ],
    })


@api_bp.get("/free-time-tasks")
@jwt_required()
def free_time_tasks() -> Any:
    current_api_user()
    items = (
        TimeTaskTemplate.query
        .filter(
            TimeTaskTemplate.active.is_(True),
            TimeTaskTemplate
            .free_time_enabled
            .is_(True),
        )
        .order_by(
            TimeTaskTemplate.title.asc(),
            TimeTaskTemplate.code.asc(),
        )
        .all()
    )

    return jsonify({
        "items": [
            serialize_time_task(item)
            for item in items
        ]
    })


@api_bp.post("/my-time/free")
@jwt_required()
def add_free_time_entry() -> Any:
    user = current_api_user()
    values = free_time_values_from_payload(
        request.get_json(silent=True) or {}
    )
    entry = TimeEntry(
        commission=None,
        user=user,
        task_template=values["task"],
        service_date=values["service_date"],
        minutes=values["minutes"],
        work_minutes=values["minutes"],
        waiting_minutes=0,
        material_minutes=0,
        travel_minutes=0,
        documentation_minutes=0,
        extra_minutes=0,
        note=values["note"],
        deviation_minutes=0,
        deviation_percent=0,
        review_status=TimeReviewStatus.OK.value,
        review_level=ReviewLevel.GREEN.value,
    )
    db.session.add(entry)
    db.session.flush()

    audit(
        "time.free.create",
        "time_entry",
        entry.id,
        {
            "task_template_id": (
                entry.task_template_id
            ),
            "service_date": (
                entry.service_date.isoformat()
            ),
            "minutes": entry.minutes,
        },
    )
    db.session.commit()

    return jsonify({
        "item": serialize_time_entry(
            entry,
            viewer=user,
        )
    }), 201


@api_bp.get("/commissions/<int:commission_id>/time")
@jwt_required()
def commission_time_entries(commission_id: int) -> Any:
    user = current_api_user()
    commission = viewable_commission(commission_id, user)
    query = TimeEntry.query.filter_by(commission_id=commission.id)

    query = query.filter(
        TimeEntry.user_id.in_(
            viewable_commission_user_ids(user)
        )
    )

    entries = query.order_by(
        TimeEntry.service_date.desc(),
        TimeEntry.created_at.desc(),
        TimeEntry.id.desc(),
    ).all()
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
        service_date=(
                values["service_date"]
                or date.today()
        ),
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
    audit(
        "time.create",
        "commission",
        commission.id,
        {
            "minutes": entry.minutes,
            "task_template_id": (
                entry.task_template_id
            ),
            "service_date": (
                entry.service_date.isoformat()
            ),
        },
    )
    db.session.commit()
    return jsonify({"item": serialize_time_entry(entry, viewer=user)}), 201


@api_bp.patch("/commissions/<int:commission_id>/time/<int:entry_id>")
@jwt_required()
def update_time_entry(commission_id: int, entry_id: int) -> Any:
    user = current_api_user()
    commission, entry = editable_time_entry(commission_id, entry_id, user)
    data = request.get_json(silent=True) or {}
    values = time_entry_values_from_payload(data, user)
    if (
        not settings.get(SettingKeys.MOBILE_TIME_COMPONENTS_ENABLED)
        and values["minutes"] == entry.minutes
    ):
        for field_name in (
            "work_minutes",
            "waiting_minutes",
            "material_minutes",
            "travel_minutes",
            "documentation_minutes",
            "extra_minutes",
        ):
            values[field_name] = getattr(entry, field_name)
    entry.task_template = values["task"]
    if values["service_date"] is not None:
        entry.service_date = (
            values["service_date"]
        )
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
    audit(
        "time.update",
        "time_entry",
        entry.id,
        {
            "commission_id": commission.id,
            "minutes": entry.minutes,
            "task_template_id": (
                entry.task_template_id
            ),
            "service_date": (
                entry.service_date.isoformat()
            ),
        },
    )
    db.session.commit()
    return jsonify({"item": serialize_time_entry(entry, viewer=user)})


@api_bp.delete("/commissions/<int:commission_id>/time/<int:entry_id>")
@jwt_required()
def delete_time_entry(commission_id: int, entry_id: int) -> Any:
    user = current_api_user()
    commission, entry = editable_time_entry(commission_id, entry_id, user)
    audit(
        "time.delete",
        "time_entry",
        entry.id,
        {"commission_id": commission.id, "minutes": entry.minutes, "task_template_id": entry.task_template_id}
    )
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
    cancelled_ids = cancelled_inventory_movement_ids(
        commission.id
    )
    movements = [
        movement
        for movement in movements
        if movement.id not in cancelled_ids
    ]
    result = []
    for expectation in expectations:
        installed_quantity = sum(
            movement.quantity
            for movement in movements
            if (
                    movement.task_template_id
                    == expectation.task_template_id
                    and movement_matches_expected_material(movement, expectation)
                    and movement.usage_status
                    == MaterialUsageStatus.INSTALLED.value
            )
        )
        quantity_differs = (
                abs(
                    installed_quantity
                    - float(expectation.quantity)
                )
                > 0.0001
        )
        is_ok = (
                not quantity_differs
                or (
                        not expectation.required
                        and installed_quantity <= 0.0001
                )
        )
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
    query = TimeEntry.query.join(
        Commission, TimeEntry.commission_id == Commission.id).join(
        User, TimeEntry.user_id == User.id).outerjoin(
        TimeTaskTemplate, TimeEntry.task_template_id == TimeTaskTemplate.id)
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
    items = query.order_by(
        TimeEntry.service_date.desc(),
        TimeEntry.created_at.desc(),
        TimeEntry.id.desc(),
    ).limit(
        query_limit()
    ).all()
    return jsonify({"items": [serialize_time_entry(item, viewer=user) for item in items]})


@api_bp.get("/review/materials")
@jwt_required()
def review_materials() -> Any:
    user = current_api_user()
    require_review_permission(user)
    query = InventoryMovement.query.join(
        InventoryItem,
        InventoryMovement.item_id == InventoryItem.id).outerjoin(
        Commission,
        InventoryMovement.commission_id == Commission.id).outerjoin(
        User, InventoryMovement.user_id == User.id).outerjoin(
        TimeTaskTemplate, InventoryMovement.task_template_id == TimeTaskTemplate.id
    )
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


def viewable_commission_user_ids(
    user: User,
) -> set[int]:
    visible_user_ids = {user.id}

    visible_user_ids.update(
        visible_user.id
        for visible_user
        in user.visible_commission_users
        if (
            visible_user.is_active
            and visible_user.craftsman_erp_id
            is not None
        )
    )

    return visible_user_ids


def can_edit_commission(commission: Commission, user: User) -> bool:
    return user.is_admin or user.is_foreman or commission.user_id == user.id


def can_edit_time_entry(entry: TimeEntry, user: User) -> bool:
    return user.is_admin or user.is_foreman or entry.user_id == user.id


def viewable_commission(commission_id: int, user: User) -> Commission:
    commission = db.session.get(Commission, commission_id)
    if commission is None or commission.local_status == CommissionLocalStatus.DELETED.value:
        abort(404)
    if (
        commission.user_id
        not in viewable_commission_user_ids(
            user
        )
    ):
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


def find_inventory_item(
        code: str,
) -> InventoryItem | None:
    if not code:
        return None

    barcode = (
        InventoryItemBarcode.query
        .filter(
            InventoryItemBarcode.code == code,
            InventoryItemBarcode.active.is_(True),
        )
        .one_or_none()
    )

    if barcode is not None and barcode.item.active:
        return barcode.item

    return (
        InventoryItem.query
        .filter(
            InventoryItem.active.is_(True),
            or_(
                InventoryItem.qr_code_payload == code,
                InventoryItem.ean == code,
                InventoryItem.item_number == code,
            ),
        )
        .one_or_none()
    )


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
    service_date_text = str(
        data.get("service_date") or ""
    ).strip()
    service_date = None

    if service_date_text:
        try:
            service_date = date.fromisoformat(
                service_date_text
            )
        except ValueError:
            abort(
                400,
                description=(
                    "Das Leistungsdatum ist ungültig."
                ),
            )
    work_minutes = int_payload(data, "work_minutes")
    waiting_minutes = int_payload(data, "waiting_minutes")
    material_minutes = int_payload(data, "material_minutes")
    travel_minutes = int_payload(data, "travel_minutes")
    documentation_minutes = int_payload(data, "documentation_minutes")
    extra_minutes = int_payload(data, "extra_minutes")
    for value in [work_minutes,
                  waiting_minutes,
                  material_minutes,
                  travel_minutes,
                  documentation_minutes,
                  extra_minutes
                  ]:
        if value < 0:
            abort(400)
    component_sum = (work_minutes +
                     waiting_minutes +
                     material_minutes +
                     travel_minutes +
                     documentation_minutes +
                     extra_minutes
                     )
    if component_sum > 0 and not settings.get(SettingKeys.MOBILE_TIME_COMPONENTS_ENABLED):
        abort(
            400,
            description="Zeitbausteine sind deaktiviert. Bitte nur eine Gesamtzeit eingeben.",
        )
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
        "service_date": service_date,
    }


def api_date_value(
    raw_value: Any,
    error_message: str,
) -> date:
    try:
        return date.fromisoformat(
            str(raw_value or "").strip()
        )
    except ValueError:
        abort(
            400,
            description=error_message,
        )


def free_time_values_from_payload(
    data: dict[str, Any],
) -> dict[str, Any]:
    try:
        task_template_id = int(
            data.get("task_template_id")
        )
        minutes = int(data.get("minutes"))
    except (TypeError, ValueError):
        abort(
            400,
            description=(
                "Leistungsposition und Minuten "
                "müssen angegeben werden."
            ),
        )

    task = (
        TimeTaskTemplate.query
        .filter(
            TimeTaskTemplate.id
            == task_template_id,
            TimeTaskTemplate.active.is_(True),
            TimeTaskTemplate
            .free_time_enabled
            .is_(True),
        )
        .one_or_none()
    )

    if task is None:
        abort(
            400,
            description=(
                "Die Leistungsposition ist nicht für "
                "freie Zeiten freigegeben."
            ),
        )

    if not 1 <= minutes <= 1440:
        abort(
            400,
            description=(
                "Die Zeit muss zwischen 1 und 1440 "
                "Minuten liegen."
            ),
        )

    service_date = api_date_value(
        data.get("service_date"),
        "Das Leistungsdatum ist ungültig.",
    )
    note = str(
        data.get("note") or ""
    ).strip()

    return {
        "task": task,
        "service_date": service_date,
        "minutes": minutes,
        "note": note or None,
    }


def serialize_work_time_profile(
    item: WorkTimeProfile,
) -> dict[str, Any]:
    return {
        "id": item.id,
        "name": item.name,
    }


def require_review_permission(user: User) -> None:
    if not (user.is_admin or user.is_foreman):
        abort(403)


def apply_time_review(
        entry: TimeEntry,
) -> None:
    task = entry.task_template
    entry.deviation_minutes = 0
    entry.deviation_percent = 0.0
    entry.review_level = (
        ReviewLevel.GREEN.value
    )

    if (
            task is None
            or task.suggested_minutes <= 0
    ):
        entry.review_status = (
            TimeReviewStatus.OK.value
        )
        return

    signed_difference = (
            entry.minutes
            - task.suggested_minutes
    )
    absolute_difference = abs(
        signed_difference
    )
    percent = (
            absolute_difference
            / task.suggested_minutes
            * 100
    )

    entry.deviation_minutes = (
        absolute_difference
    )
    entry.deviation_percent = percent

    if (
            signed_difference < 0
            and not task.review_when_shorter
    ):
        entry.review_status = (
            TimeReviewStatus.OK.value
        )
        return

    if (
            absolute_difference
            > settings.get(SettingKeys.TIME_DEVIATION_MINUTES)
            or percent
            > settings.get(SettingKeys.TIME_DEVIATION_PERCENT)
    ):
        entry.review_status = (
            TimeReviewStatus.NEEDS_REVIEW.value
        )
        entry.review_level = (
            ReviewLevel.YELLOW.value
            if (
                    entry.deviation_reason
                    or entry.note
            )
            else ReviewLevel.RED.value
        )
    else:
        entry.review_status = (
            TimeReviewStatus.OK.value
        )


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
    matching = [
        expected
        for expected in expectations
        if movement_matches_expected_material(
            movement,
            expected,
        )
    ]
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
        "appointment.sent": "Terminvorschläge gesendet",
        "appointment.accepted": "Termin bestätigt",
        "appointment.rejected": "Terminvorschläge abgelehnt",
        "appointment.cancelled": "Bestätigten Termin aufgehoben",
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
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "craftsman_erp_id": user.craftsman_erp_id,
        "is_admin": user.is_admin,
        "is_foreman": user.is_foreman
    }


def serialize_commission(
        commission: Commission,
        include_details: bool = False,
        viewer: User | None = None
) -> dict[str, Any]:
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
        "is_high_priority": bool(commission.is_high_priority),
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
        "accepted_appointment_from": serialize_accepted_appointment_from(
            commission
        ),
        "accepted_appointment_to": iso_datetime(
            commission.accepted_appointment_to
        ),
        "accepted_appointment_comment": (
            commission.accepted_appointment_comment
        ),
        "accepted_appointment_has_time": commission.accepted_appointment_has_time,
        "accepted_appointment_is_manual": commission.accepted_appointment_is_manual,
    }

    if viewer is not None:
        item["is_own"] = commission.user_id == viewer.id
        item["can_edit"] = can_edit_commission(commission, viewer)

    if include_details:
        item["raw_payload"] = commission.raw_payload

    return item


def serialize_inventory_location(
        item: InventoryLocation,
) -> dict[str, Any]:
    return {
        "id": item.id,
        "code": item.code,
        "name": item.name,
        "location_type": item.location_type,
        "is_default": item.is_default,
    }


def serialize_inventory_stock(
        item: InventoryStock,
) -> dict[str, Any]:
    return {
        "id": item.id,
        "quantity": item.quantity,
        "location": serialize_inventory_location(
            item.location
        ),
    }


def serialize_inventory_item_group(
        item: InventoryItemGroup,
        serial_number_required: bool = False,
) -> dict[str, Any]:
    return {
        "id": item.id,
        "code": item.code,
        "name": item.name,
        "serial_number_required": (
            serial_number_required
        ),
        "active": item.active,
    }


def serialize_inventory_item_type(
        item: InventoryItemType,
) -> dict[str, Any]:
    return {
        "id": item.id,
        "code": item.code,
        "name": item.name,
        "unit": item.unit,
        "serial_number_required": (
            item.serial_number_required
        ),
        "active": item.active,
        "group": (
            serialize_inventory_item_group(
                item.group,
                serial_number_required=(
                    item.serial_number_required
                ),
            )
        ),
    }


def serialize_inventory_item(
        item: InventoryItem,
) -> dict[str, Any]:
    stocks = sorted(
        [
            stock
            for stock in item.stocks
            if stock.location.active
        ],
        key=lambda stock: (
            not stock.location.is_default,
            stock.location.name.lower(),
        ),
    )

    barcodes = sorted(
        [
            barcode.code
            for barcode in item.barcodes
            if barcode.active
        ]
    )

    return {
        "id": item.id,
        "item_number": item.item_number,
        "name": item.name,
        "unit": item.unit,
        "ean": item.ean,
        "qr_code_payload": item.qr_code_payload,
        "stock_quantity": item.stock_quantity,
        "reorder_level": item.reorder_level,
        "barcodes": barcodes,
        "stocks": [
            serialize_inventory_stock(stock)
            for stock in stocks
        ],
        "item_type": (
            serialize_inventory_item_type(
                item.item_type
            )
            if item.item_type
            else None
        ),
        "serial_number_required": (
            item.serial_number_required
        ),
    }


def serialize_inventory_movement(
        item: InventoryMovement,
        cancelled_ids: set[int] | None = None,
        can_edit: bool = False,
) -> dict[str, Any]:
    cancelled_ids = cancelled_ids or set()
    is_cancelled = item.id in cancelled_ids
    can_cancel = (
            can_edit
            and item.movement_type
            == InventoryMovementType.WITHDRAWAL.value
            and not is_cancelled
    )

    return {
        "id": item.id,
        "item": serialize_inventory_item(item.item),
        "user": (
            serialize_user(item.user)
            if item.user
            else None
        ),
        "commission_id": item.commission_id,
        "task_template": (
            serialize_time_task(item.task_template)
            if item.task_template
            else None
        ),
        "movement_type": item.movement_type,
        "quantity": item.quantity,
        "source": item.source,
        "source_location": (
            serialize_inventory_location(
                item.source_location
            )
            if item.source_location
            else None
        ),
        "target_location": (
            serialize_inventory_location(
                item.target_location
            )
            if item.target_location
            else None
        ),
        "usage_status": item.usage_status,
        "deviation_reason": item.deviation_reason,
        "note": item.note,
        "review_status": item.review_status,
        "review_level": item.review_level,
        "occurred_at": iso_datetime(item.occurred_at),
        "is_cancelled": is_cancelled,
        "can_cancel": can_cancel,
        "serial_number": item.serial_number,
        "item_type": (
            serialize_inventory_item_type(
                item.item_type_snapshot
                or item.item.item_type
            )
            if (
                    item.item_type_snapshot
                    or item.item.item_type
            )
            else None
        ),
        "item_group": (
            serialize_inventory_item_group(
                item.item_group_snapshot
                or (
                    item.item.item_type.group
                    if item.item.item_type
                    else None
                )
            )
            if (
                    item.item_group_snapshot
                    or item.item.item_type
            )
            else None
        ),
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
        "review_when_shorter": item.review_when_shorter,
        "free_time_enabled": item.free_time_enabled,
    }


def serialize_expected_material(
        item: TimeTaskExpectedMaterial,
) -> dict[str, Any]:
    return {
        "id": item.id,
        "task_template_id": (
            item.task_template_id
        ),
        "item": (
            serialize_inventory_item(
                item.item
            )
            if item.item
            else None
        ),
        "item_type": (
            serialize_inventory_item_type(
                item.item_type
            )
            if item.item_type
            else None
        ),
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


def serialize_time_entry(
    item: TimeEntry,
    viewer: User | None = None,
) -> dict[str, Any]:
    can_edit = False

    if viewer is not None:
        can_edit = (
            can_edit_time_entry(item, viewer)
            and (
                item.commission is None
                or can_edit_commission(
                    item.commission,
                    viewer,
                )
            )
        )

    commission = item.commission

    return {
        "id": item.id,
        "commission_id": item.commission_id,
        "commission": (
            {
                "id": commission.id,
                "number": str(
                    commission.erp_idnum
                    or commission.code
                    or commission.id
                ),
                "title": (
                    commission.title
                    or "Auftrag"
                ),
            }
            if commission is not None
            else None
        ),
        "is_free_time": commission is None,
        "user": serialize_user(item.user),
        "task_template": (
            serialize_time_task(
                item.task_template
            )
            if item.task_template
            else None
        ),
        "minutes": item.minutes,
        "work_minutes": item.work_minutes,
        "waiting_minutes": item.waiting_minutes,
        "material_minutes": item.material_minutes,
        "travel_minutes": item.travel_minutes,
        "documentation_minutes": (
            item.documentation_minutes
        ),
        "extra_minutes": item.extra_minutes,
        "deviation_reason": item.deviation_reason,
        "note": item.note,
        "deviation_minutes": item.deviation_minutes,
        "deviation_percent": item.deviation_percent,
        "review_status": item.review_status,
        "review_level": item.review_level,
        "service_date": item.service_date.isoformat(),
        "created_at": iso_datetime(item.created_at),
        "can_edit": can_edit,
    }


def serialize_commission_photo(
        photo: CommissionPhoto,
        commission_id: int | None = None,
) -> dict[str, Any]:
    effective_commission_id = (
        photo.commission_id
        if photo.commission_id is not None
        else commission_id
    )

    if effective_commission_id is None:
        raise ValueError(
            "Für das Foto fehlt der Auftrag."
        )
    return {
        "id": photo.id,
        "commission_id": effective_commission_id,
        "source": photo.source,
        "external_id": photo.external_id,
        "context": photo.context,
        "external_document_id": (
            photo.external_document_id
        ),
        "external_entity_name": (
            photo.external_entity_name
        ),
        "external_entity_id": (
            photo.external_entity_id
        ),
        "picture_type_id": (
            photo.picture_type_erp_id
        ),
        "picture_type_name": (
            photo.picture_type_name
        ),
        "erp_sync_state": (
            photo.erp_sync_state
        ),
        "erp_sync_message": (
            photo.erp_sync_message
        ),
        "erp_synced_at": iso_datetime(
            photo.erp_synced_at
        ),
        "original_filename": photo.original_filename,
        "mime_type": photo.mime_type,
        "size_bytes": photo.size_bytes,
        "caption": photo.caption,
        "uploaded_by": (
            serialize_user(photo.uploaded_by)
            if photo.uploaded_by
            else None
        ),
        "created_at": iso_datetime(
            photo.created_at
        ),
        "updated_at": iso_datetime(
            photo.updated_at
        ),
        "content_path": (
            f"/api/commissions/"
            f"{effective_commission_id}/photos/"
            f"{photo.id}/content"
        ),
        "thumbnail_path": (
            f"/api/commissions/"
            f"{effective_commission_id}/photos/"
            f"{photo.id}/thumbnail"
        ),
    }


def serialize_commission_attachment(
        attachment: CommissionAttachment,
) -> dict[str, Any]:
    thumbnail_storage_name = getattr(
        attachment,
        "thumbnail_storage_name",
        None,
    )

    return {
        "id": attachment.id,
        "commission_id": attachment.commission_id,
        "source_entity_name": (
            attachment.source_entity_name
        ),
        "source_entity_id": (
            attachment.source_entity_id
        ),
        "external_document_id": (
            attachment.external_document_id
        ),
        "original_filename": (
            attachment.original_filename
        ),
        "mime_type": attachment.mime_type,
        "size_bytes": attachment.size_bytes,
        "is_image": (
            attachment.mime_type.startswith(
                "image/"
            )
        ),
        "created_at": iso_datetime(
            attachment.created_at
        ),
        "content_path": (
            f"/api/commissions/"
            f"{attachment.commission_id}/"
            f"attachments/{attachment.id}/content"
        ),
        "thumbnail_path": (
            (
                f"/api/commissions/"
                f"{attachment.commission_id}/"
                f"attachments/{attachment.id}/thumbnail"
            )
            if thumbnail_storage_name
            else None
        ),
    }


def serialize_appointment_window(
        item: AppointmentWindow,
) -> dict[str, Any]:
    return {
        "id": item.id,
        "code": item.code,
        "name": item.name,
        "start_time": item.start_time.strftime(
            "%H:%M"
        ),
        "end_time": item.end_time.strftime(
            "%H:%M"
        ),
        "sort_order": item.sort_order,
    }


def serialize_appointment_proposal(
        item,
) -> dict[str, Any]:
    return {
        "id": item.id,
        "window_id": item.window_id,
        "proposed_date": item.proposed_date.isoformat(),
        "start_time": item.start_time.strftime(
            "%H:%M"
        ),
        "end_time": item.end_time.strftime(
            "%H:%M"
        ),
        "window_name": item.window_name,
        "accepted": item.accepted,
    }


def serialize_appointment_request(
        item: CommissionAppointmentRequest,
) -> dict[str, Any]:
    accepted_proposal = next(
        (
            proposal
            for proposal in item.proposals
            if proposal.accepted
        ),
        None,
    )

    return {
        "id": item.id,
        "commission_id": item.commission_id,
        "recipient_name": item.recipient_name,
        "recipient_email": item.recipient_email,
        "message": item.message,
        "status": item.status,
        "tenant_comment": item.tenant_comment,
        "sent_at": iso_datetime(
            item.sent_at
        ),
        "responded_at": iso_datetime(
            item.responded_at
        ),
        "expires_at": iso_datetime(
            item.expires_at
        ),
        "created_at": iso_datetime(
            item.created_at
        ),
        "created_by": (
            serialize_user(item.created_by)
            if item.created_by
            else None
        ),
        "accepted_proposal_id": (
            accepted_proposal.id
            if accepted_proposal
            else None
        ),
        "proposals": [
            serialize_appointment_proposal(
                proposal
            )
            for proposal in item.proposals
        ],
    }
