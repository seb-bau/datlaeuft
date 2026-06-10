from __future__ import annotations

from hmac import compare_digest
from typing import Any

from flask import Blueprint, current_app, jsonify, request

from .audit import audit
from .extensions import db
from .models import WebhookEvent
from .settings import SettingKeys, settings
from .wowiport import mark_commission_deleted, sync_commission_from_erp

webhooks_bp = Blueprint("webhooks", __name__, url_prefix="/webhooks")


@webhooks_bp.post("/wowiport")
def wowiport_webhook() -> Any:
    if not valid_bearer_token():
        return jsonify({"message": "Unauthorized"}), 401
    payload = request.get_json(silent=True) or {}
    event = WebhookEvent(entity_type=payload.get("entityType"), entity_name=payload.get("entityName"), entity_id=payload.get("id"), action=payload.get("action"), payload=payload)
    db.session.add(event)
    if payload.get("entityType") != "Commission":
        event.processed = True
        event.message = "Ignored entity"
        db.session.commit()
        return jsonify({"message": "ignored"})
    try:
        erp_id = int(payload.get("id"))
        action = str(payload.get("action", "")).lower()
        if action == "deleted":
            commission = mark_commission_deleted(erp_id)
        else:
            commission = sync_commission_from_erp(erp_id)
        event.processed = True
        event.message = "processed" if commission else "no local change"
        audit("webhook.wowiport", "commission", erp_id, {"action": action, "event_id": event.id})
        db.session.commit()
        return jsonify({"message": event.message})
    except Exception as exc:
        current_app.logger.exception("wowiport_webhook_failed")
        event.processed = False
        event.message = str(exc)
        db.session.commit()
        return jsonify({"message": "failed"}), 500


def valid_bearer_token() -> bool:
    expected = settings.get(SettingKeys.WOWI_WEBHOOK_TOKEN)
    header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    if not header.startswith(prefix):
        return False
    return compare_digest(header[len(prefix):].strip(), expected)
