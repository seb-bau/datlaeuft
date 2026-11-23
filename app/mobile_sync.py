from datetime import timedelta, timezone
from hashlib import sha256
import json
import re
from uuid import UUID

from flask import Blueprint, current_app, jsonify, request
from flask_jwt_extended import jwt_required
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from werkzeug.exceptions import HTTPException

from .extensions import db
from .models import Commission, CommissionLocalStatus, ErpOutboxOperation, MobileSyncOperation, MobileSyncDelivery, ProcessInfo, utc_now
from .mobile_transaction import current_mobile_transaction


mobile_sync_bp = Blueprint("mobile_sync", __name__, url_prefix="/api/mobile")
read_pattern = re.compile(r"^/api/(?:commissions(?:/(?:simple-create-options|create-options|create-tasks|objects|users|[1-9]\d*(?:/(?:history|contacts|appointments|attachments|photos|materials|time|infos|completion-check|related(?:/[1-9]\d*)?))?))?|commission-objects/search|time-tasks(?:/search)?|inventory/(?:search|lookup|locations)|appointment-windows)$")
write_pattern = re.compile(r"^/api/commissions(?:/simple|/[1-9]\d*/(?:start|reject|complete|time(?:/[1-9]\d*)?|materials(?:/[1-9]\d*/cancel)?|photos(?:/[1-9]\d*)?|appointments(?:/accepted)?|infos(?:/read)?))$")
parent_pattern = re.compile(r"^/api/commissions/([1-9]\d*)(?:/|$)")
booking_pattern = re.compile(r"^/api/commissions/[1-9]\d*/(?:time|materials)$")
COMPLETED_STATUSES = {
    CommissionLocalStatus.DONE.value,
    CommissionLocalStatus.AWAITING_INVOICE.value,
    CommissionLocalStatus.TENANT_CHARGE_REVIEW.value,
}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def same_value(left, right):
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(same_value(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(same_value(first, second) for first, second in zip(left, right))
    return left == right


def normalized_value(resource, payload, target_id=None):
    if re.fullmatch(r"/api/commissions/[1-9]\d*", resource):
        item = payload.get("item", payload)
        return {key: item.get(key) for key in (
            "local_status", "assigned_user", "can_edit", "is_actionable",
            "accepted_appointment_from", "accepted_appointment_to",
            "accepted_appointment_comment", "accepted_appointment_is_manual",
            "accepted_appointment_has_time",
        )}
    if target_id is not None:
        item = next((item for item in payload.get("items", []) if item.get("id") == target_id), None)
        if item is None:
            return None
        keys = ("id", "user", "task_template", "service_date", "minutes", "note", "deviation_reason", "work_minutes", "waiting_minutes", "material_minutes", "travel_minutes", "documentation_minutes", "extra_minutes", "review_status", "review_level", "quantity", "usage_status", "is_cancelled", "can_cancel", "caption", "source", "updated_at")
        return {key: item.get(key) for key in keys if key in item}
    return payload


def resource_key(path, query=None):
    from urllib.parse import urlencode
    return path + ("?" + urlencode(sorted((item["name"], item.get("value", "")) for item in query or [])) if query else "")


def dispatch(path, method="GET", query=None, body=None, content_type=None):
    from urllib.parse import urlencode
    authorization = request.headers.get("Authorization", "")
    query_string = urlencode([(item["name"], item.get("value", "")) for item in query or []])
    with current_app.test_request_context(path, method=method, query_string=query_string, data=body, content_type=content_type, headers={"Authorization": authorization}):
        return current_app.full_dispatch_request()


def valid_query(value):
    return isinstance(value, list) and len(value) <= 20 and all(isinstance(item, dict) and isinstance(item.get("name"), str) and isinstance(item.get("value", ""), str) and len(item["name"]) <= 80 and len(item.get("value", "")) <= 1000 for item in value)


def already_applied_status(commission, action, body):
    status = commission.local_status
    if action == "start":
        return status == CommissionLocalStatus.IN_PROGRESS.value or status in COMPLETED_STATUSES
    if action != "complete" or type(body.get("completed")) is not bool or commission.completion_submitted_at is None:
        return False
    completed = body["completed"]
    if (completed and status not in COMPLETED_STATUSES) or (not completed and status != CommissionLocalStatus.COMPLETION_REVIEW.value):
        return False
    return (
        commission.completion_successful is completed
        and bool(commission.tenant_charge_requested) == (completed and bool(body.get("tenant_charge")))
        and (commission.completion_reason or "") == str(body.get("reason") or "").strip()
    )


def matches_baseline(current, expected, status_change, *, allow_own_completion=False):
    if not isinstance(current, dict) or not isinstance(expected, dict):
        return same_value(current, expected)
    if status_change:
        fields = ("local_status", "assigned_user", "can_edit", "is_actionable")
        return all(same_value(current.get(field), expected.get(field)) for field in fields)
    if (
        allow_own_completion
        and expected.get("local_status") == CommissionLocalStatus.IN_PROGRESS.value
        and current.get("local_status") in COMPLETED_STATUSES
    ):
        expected = {**expected, "local_status": current["local_status"]}
    return same_value(current, expected)


def matching_booking(user_id, path, fingerprint, device_id, known_ids):
    candidates = set()
    payloads = db.session.query(MobileSyncOperation.response_payload).filter(
        MobileSyncOperation.user_id == user_id,
        MobileSyncOperation.response_payload["booking_fingerprint"].as_string() == fingerprint,
    )
    for (payload,) in payloads:
        item_id = payload.get("payload", {}).get("item", {}).get("id")
        if type(item_id) is int and item_id not in known_ids and payload.get("booking_device") != device_id:
            candidates.add(item_id)
    if not candidates:
        return None
    response = dispatch(path)
    if response.status_code >= 400:
        return None
    return next((item["id"] for item in response.get_json().get("items", [])
                 if item.get("id") in candidates and not item.get("is_cancelled")), None)


@mobile_sync_bp.post("/offline-data")
@jwt_required()
def offline_data():
    from .api import current_api_user
    current_api_user()
    data = request.get_json(silent=True) or {}
    resources = data.get("resources")
    if not isinstance(resources, list) or not 1 <= len(resources) <= 30:
        return jsonify(message="Bitte 1 bis 30 Datenbereiche angeben."), 400
    items = []
    token = current_mobile_transaction.set({"read_only": True})
    try:
        for resource in resources:
            if not isinstance(resource, dict):
                return jsonify(message="Ungültiger Datenbereich."), 400
            path, query = resource.get("path", ""), resource.get("query", [])
            if not isinstance(path, str) or not read_pattern.fullmatch(path) or not valid_query(query):
                return jsonify(message="Dieser Datenbereich ist nicht freigegeben."), 400
            response = dispatch(path, query=query)
            payload = response.get_json(silent=True)
            items.append({"path": path, "query": query, "status": response.status_code, "payload": payload})
        db.session.rollback()
        return jsonify(items=items)
    finally:
        current_mobile_transaction.reset(token)


@mobile_sync_bp.post("/offline-operations")
@jwt_required()
def offline_operation():
    from .api import can_edit_commission, current_api_user, viewable_commission
    user = current_api_user()
    user_id = user.id
    try:
        operation = json.loads(request.form.get("operation", "{}")) if request.mimetype == "multipart/form-data" else request.get_json(silent=True)
        if not isinstance(operation, dict):
            raise ValueError("Ungültiger Vorgang.")
        operation_id = str(UUID(operation.get("id", "")))
        path, method = operation.get("path", ""), operation.get("method", "")
        if not isinstance(path, str) or not write_pattern.fullmatch(path) or method not in {"POST", "PATCH", "DELETE"}:
            raise ValueError("Dieser Vorgang ist nicht freigegeben.")
        body = operation.get("body", {})
        if not isinstance(body, dict):
            raise ValueError("Ungültige Vorgangsdaten.")
        predecessor_id = operation.get("predecessor_id")
        if predecessor_id is not None:
            predecessor_id = str(UUID(predecessor_id))
        device_id = operation.get("device_id")
        if device_id is not None:
            device_id = str(UUID(device_id))
        known_ids = operation.get("known_booking_ids", [])
        if not isinstance(known_ids, list) or len(known_ids) > 2000 or any(type(value) is not int or value <= 0 for value in known_ids):
            raise ValueError("Ungültiger Buchungsstand.")
        upload = request.files.get("request_body")
        raw_body = upload.read(80 * 1024 * 1024 + 1) if upload else canonical(body).encode()
        if len(raw_body) > 80 * 1024 * 1024:
            raise ValueError("Die übermittelten Dateien sind zu groß.")
        content_type = operation.get("content_type", "application/json")
        if not isinstance(content_type, str) or not (content_type == "application/json" or content_type.startswith("multipart/form-data; boundary=")):
            raise ValueError("Ungültiges Datenformat.")
        signature = sha256(canonical(operation).encode() + b"\0" + raw_body).hexdigest()
        fingerprint = sha256(path.encode() + b"\0" + raw_body).hexdigest() if method == "POST" and booking_pattern.fullmatch(path) else None
    except (ValueError, TypeError, AttributeError) as error:
        return jsonify(message=str(error)), 400

    def receipt():
        row = db.session.get(MobileSyncOperation, (user_id, operation_id), populate_existing=True)
        if row is None:
            return None
        if row.request_sha256 != signature:
            return jsonify(message="Diese Vorgangs-ID wurde für andere Daten verwendet.", code="operation_reused"), 409
        return jsonify(row.response_payload)

    existing = receipt()
    if existing is not None:
        return existing
    photo_ids = operation.get("local_photo_ids", [])
    if not isinstance(photo_ids, list) or len(photo_ids) > 100 or any(type(value) is not int or value >= 0 for value in photo_ids):
        return jsonify(message="Ungültige lokale Fotokennungen."), 400
    transaction = {"user_id": user_id, "operation_id": operation_id, "files": [], "local_photo_ids": photo_ids}
    token = current_mobile_transaction.set(transaction)
    succeeded = False
    try:
        db.session.rollback()
        receipt_row = MobileSyncOperation(user_id=user_id, operation_id=operation_id, request_sha256=signature, response_payload={})
        db.session.add(receipt_row)
        db.session.flush()
        match = parent_pattern.match(path)
        parent = f"/api/commissions/{match.group(1)}" if match else None
        status_change = bool(parent and method == "POST" and path in {parent + "/start", parent + "/reject", parent + "/complete"})
        settled = False
        if match:
            commission_id = int(match.group(1))
            commission = db.session.get(Commission, commission_id, populate_existing=True, with_for_update=True)
            if commission is None or commission.local_status in {CommissionLocalStatus.CANCELED.value, CommissionLocalStatus.DELETED.value}:
                return jsonify(message="Der Auftrag ist nicht mehr verfügbar. Lokale Änderungen bleiben zur Prüfung erhalten.", code="commission_unavailable"), 404
            commission = viewable_commission(commission_id, user)
            if commission.rejected_at is not None or commission.review_resolved_at is not None:
                return jsonify(message="Der Auftrag wurde abgelehnt oder abschließend geprüft.", code="commission_closed"), 409
            if commission.approval_state == "pending" and not re.search(r"/photos(?:/\d+)?$", path):
                return jsonify(message="Der Auftrag wartet auf die Freigabe.", code="awaiting_approval"), 503
            if not commission.is_actionable and not re.search(r"/photos(?:/\d+)?$", path):
                return jsonify(message="Der Auftrag wartet noch auf die Wowiport-Bestätigung.", code="awaiting_erp"), 503
            settled = status_change and already_applied_status(commission, path.rsplit("/", 1)[-1], body) and can_edit_commission(commission, user)
        predecessor = db.session.get(MobileSyncOperation, (user_id, predecessor_id)) if predecessor_id and predecessor_id != operation_id else None
        if not settled and predecessor_id and predecessor is None:
            return jsonify(message="Ein vorheriger Vorgang steht noch aus.", code="dependency_pending"), 409
        base = operation.get("base")
        if not settled and base is not None:
            if not isinstance(base, dict) or not isinstance(base.get("path"), str) or not read_pattern.fullmatch(base["path"]) or not valid_query(base.get("query", [])):
                return jsonify(message="Ungültiger Vergleichsstand."), 400
            if parent is None or not (base["path"] == parent or base["path"].startswith(parent + "/")):
                return jsonify(message="Der Vergleichsstand gehört nicht zum Auftrag."), 400
            if status_change and base["path"] != parent:
                return jsonify(message="Der Auftragsstatus fehlt im Vergleichsstand."), 400
            current = dispatch(base["path"], query=base.get("query", []))
            if current.status_code >= 400:
                return current
            current_value = normalized_value(base["path"], current.get_json(), base.get("target_id"))
            expected_value = base.get("value")
            allow_own_completion = (
                method == "POST"
                and path == parent + "/time"
                and base["path"] == parent
                and commission.completion_successful is True
                and commission.completion_submitted_by_user_id == user_id
            )
            baseline_matches = matches_baseline(
                current_value, expected_value, status_change, allow_own_completion=allow_own_completion,
            )
            if not baseline_matches and predecessor is not None:
                key = base["path"] + (f"#{base['target_id']}" if base.get("target_id") is not None else "")
                if key in predecessor.response_payload.get("baselines", {}):
                    expected_value = predecessor.response_payload["baselines"][key]
                    baseline_matches = matches_baseline(
                        current_value, expected_value, status_change, allow_own_completion=allow_own_completion,
                    )
            if not baseline_matches:
                return jsonify(message="Der Serverstand wurde zwischenzeitlich geändert.", code="conflict", current=current_value, local=operation.get("body"), base=expected_value), 409
        elif not settled and parent and not path.endswith(("/infos", "/infos/read")) and not (method == "POST" and path.endswith(("/time", "/materials", "/photos"))):
            return jsonify(message="Der Vergleichsstand fehlt.", code="missing_base"), 409
        if fingerprint and device_id and operation.get("confirm_duplicate") is not True and can_edit_commission(commission, user):
            duplicate = matching_booking(user_id, path, fingerprint, device_id, set(known_ids))
            if duplicate is not None:
                return jsonify(message="Eine gleichartige Buchung ist bereits auf dem Server vorhanden. Prüfe sie, bevor du erneut buchst.", code="duplicate_booking", booking_id=duplicate), 409
        response = dispatch(parent) if settled else dispatch(path, method, body=raw_body, content_type=content_type)
        payload = response.get_json(silent=True)
        if response.status_code >= 400:
            db.session.rollback()
            return jsonify(message=(payload or {}).get("message", "Der Vorgang wurde abgelehnt."), code=(payload or {}).get("code", "rejected"), details=payload), response.status_code
        if payload is None:
            payload = {}
        baselines = {}
        if path == "/api/commissions/simple" and isinstance(payload.get("item"), dict):
            parent = f"/api/commissions/{payload['item']['id']}"
        if parent:
            refreshed = response if settled else dispatch(parent)
            if refreshed.status_code < 400:
                baselines[parent] = normalized_value(parent, refreshed.get_json())
            section_match = re.match(r"^/api/commissions/\d+/(time|materials|photos)(?:/(\d+))?", path)
            if section_match:
                resource = parent + "/" + section_match.group(1)
                entity_id = payload.get("item", {}).get("id") or (int(section_match.group(2)) if section_match.group(2) else None)
                if entity_id is not None:
                    current = dispatch(resource)
                    if current.status_code < 400:
                        baselines[f"{resource}#{entity_id}"] = normalized_value(resource, current.get_json(), entity_id)
            if "/appointments" in path:
                resource = parent + "/appointments"
                current = dispatch(resource)
                if current.status_code < 400:
                    baselines[resource] = current.get_json()
        time_days = {}
        if parent and re.match(r"^/api/commissions/\d+/time(?:/\d+)?$", path):
            dates = {operation.get("body", {}).get("service_date")}
            if isinstance(base, dict) and isinstance(base.get("value"), dict):
                dates.add(base["value"].get("service_date"))
            for service_date in dates - {None, ""}:
                day = dispatch("/api/my-time", query=[{"name": "date", "value": str(service_date)}])
                if day.status_code < 400:
                    time_days[str(service_date)] = day.get_json()
        result = {"id": operation_id, "payload": payload, "status": response.status_code, "baselines": baselines, "my_time": time_days, "references": transaction.get("references", {})}
        if settled:
            result["already_applied"] = True
        if fingerprint:
            result.update(booking_fingerprint=fingerprint, booking_device=device_id)
        if receipt_row not in db.session:
            db.session.add(receipt_row)
        receipt_row.response_payload = result
        db.session.commit()
        succeeded = True
        return jsonify(result)
    except HTTPException as error:
        db.session.rollback()
        return jsonify(message=error.description, code="rejected"), error.code
    except IntegrityError:
        db.session.rollback()
        existing = receipt()
        return existing if existing is not None else (jsonify(message="Die Speicherung wird erneut versucht.", code="retry"), 503)
    except Exception:
        db.session.rollback()
        current_app.logger.exception("mobile_operation_failed operation_id=%s", operation_id)
        return jsonify(message="Die Übertragung wird später erneut versucht.", code="retry"), 503
    finally:
        if not succeeded:
            db.session.rollback()
            for path in transaction["files"]:
                path.unlink(missing_ok=True)
        current_mobile_transaction.reset(token)


@mobile_sync_bp.get("/delivery-issues")
@jwt_required()
def delivery_issues():
    from .api import current_api_user
    user = current_api_user()
    rows = MobileSyncDelivery.query.filter(MobileSyncDelivery.user_id == user.id, MobileSyncDelivery.state.in_(["failed", "expired"])).order_by(MobileSyncDelivery.created_at.desc()).limit(100).all()
    items = [{"id": row.id, "module": "commissions", "message": row.error, "created_at": row.created_at.isoformat()} for row in rows]
    failed_info_ids = {row.payload.get("info_id") for row in rows if row.kind == "process_info"}
    failures = ErpOutboxOperation.query.join(Commission).filter(
        or_(Commission.user_id == user.id, Commission.created_by_user_id == user.id),
        ErpOutboxOperation.state == "failed",
    ).order_by(ErpOutboxOperation.created_at.desc()).limit(100).all()
    for row in failures:
        items.append({"id": f"erp:{row.id}", "module": "commissions", "message": f"Auftrag {row.commission.erp_idnum or row.commission.id} · Wowiport: {row.last_error or 'Übertragung fehlgeschlagen.'}", "created_at": row.created_at.isoformat()})
    messages = ProcessInfo.query.filter(
        ProcessInfo.sender_user_id == user.id,
        or_(ProcessInfo.state.in_(["failed", "uncertain"]), ProcessInfo.mail_state == "failed"),
    ).order_by(ProcessInfo.updated_at.desc()).limit(100).all()
    for row in messages:
        if row.id in failed_info_ids:
            continue
        items.append({"id": f"info:{row.id}", "module": "commissions", "message": "Vorgangsinformation: " + (row.last_error or "Die Übertragung wird erneut geprüft."), "created_at": row.created_at.isoformat()})
    return jsonify(items=items)


@mobile_sync_bp.get("/offline-operations/<operation_id>")
@jwt_required()
def operation_receipt(operation_id):
    from .api import current_api_user
    user = current_api_user()
    try:
        operation_id = str(UUID(operation_id))
    except ValueError:
        return jsonify(message="Ungültige Vorgangskennung."), 400
    row = db.session.get(MobileSyncOperation, (user.id, operation_id))
    if row is None:
        return jsonify(message="Der Vorgang wurde noch nicht bestätigt."), 404
    return jsonify(row.response_payload)


@mobile_sync_bp.post("/delivery-issues/retry")
@jwt_required()
def retry_deliveries():
    from .api import current_api_user
    user = current_api_user()
    for row in MobileSyncDelivery.query.filter_by(user_id=user.id, state="failed").all():
        row.available_at = utc_now()
    failures = ErpOutboxOperation.query.join(Commission).filter(
        or_(Commission.user_id == user.id, Commission.created_by_user_id == user.id),
        ErpOutboxOperation.state == "failed",
    ).all()
    for row in failures:
        row.available_at = utc_now()
    for row in ProcessInfo.query.filter_by(sender_user_id=user.id, mail_state="failed").all():
        row.mail_available_at = utc_now()
    db.session.commit()
    return jsonify(queued=True)


def process_mobile_deliveries(limit=20):
    processed = 0
    for _ in range(limit):
        now = utc_now()
        row = MobileSyncDelivery.query.filter(MobileSyncDelivery.state.in_(["pending", "failed", "processing"]), MobileSyncDelivery.available_at <= now).order_by(MobileSyncDelivery.created_at).with_for_update(skip_locked=True).first()
        if row is None:
            db.session.rollback()
            break
        row_id = row.id
        row.state = "processing"
        row.available_at = now + timedelta(minutes=10)
        db.session.commit()
        try:
            if row.kind == "process_info":
                from .process_info_service import write_outgoing_info
                write_outgoing_info(row.payload["info_id"])
                row.state = "sent"
            elif row.kind == "appointment":
                from .appointment_service import send_appointment_request_email
                from .models import CommissionAppointmentRequest
                appointment = db.session.get(CommissionAppointmentRequest, row.payload["appointment_id"])
                if appointment is None or appointment.status != "pending":
                    row.state = "cancelled"
                else:
                    expires_at = appointment.expires_at
                    if expires_at is not None and expires_at.replace(tzinfo=expires_at.tzinfo or timezone.utc) <= now:
                        row.state = "expired"
                        row.error = "Die Terminvorschläge sind vor dem Versand abgelaufen. Bitte neue Vorschläge erstellen."
                        row.payload = {}
                        db.session.commit()
                        processed += 1
                        continue
                    send_appointment_request_email(appointment, row.payload["public_token"])
                    row.state = "sent"
            else:
                from .process_info_service import send_commission_status_email
                commission = db.session.get(Commission, row.payload["commission_id"])
                if commission is None:
                    raise ValueError("Der Auftrag ist nicht mehr verfügbar.")
                send_commission_status_email(commission, **{key: row.payload[key] for key in ("event_name", "reason", "submitted_by_name")})
                row.state = "sent"
            row.error = None
            if row.state in {"sent", "cancelled"}:
                row.payload = {}
            db.session.commit()
        except Exception as error:
            current_app.logger.exception("mobile_delivery_failed delivery_id=%s", row_id)
            db.session.rollback()
            row = db.session.get(MobileSyncDelivery, row_id, populate_existing=True, with_for_update=True)
            if row is None or row.state != "processing":
                db.session.rollback()
                continue
            row.attempts += 1
            row.state = "failed"
            row.error = str(error)
            row.available_at = now + timedelta(seconds=min(3600, 30 * 2 ** min(row.attempts, 7)))
            db.session.commit()
        processed += 1
    return processed
