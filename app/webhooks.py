from __future__ import annotations

from hmac import compare_digest
from typing import Any

from flask import Blueprint, jsonify, request

from .extensions import db
from .models import WebhookEvent
from .settings import SettingKeys, settings

webhooks_bp = Blueprint("webhooks", __name__, url_prefix="/webhooks")


@webhooks_bp.post("/wowiport")
def wowiport_webhook() -> Any:
    if not valid_bearer_token():
        return jsonify({"message": "Unauthorized"}), 401

    payload = request.get_json(silent=True) or {}

    event = WebhookEvent(
        entity_type=payload.get("entityType"),
        entity_name=payload.get("entityName"),
        entity_id=payload.get("id"),
        action=payload.get("action"),
        payload=payload,
    )

    db.session.add(event)
    db.session.commit()

    return jsonify({
        "message": "accepted",
        "event_id": event.id,
    }), 202


def valid_bearer_token() -> bool:
    expected = settings.get(
        SettingKeys.WOWI_WEBHOOK_TOKEN
    )

    if (
        not expected
        or expected
        == SettingKeys.WOWI_WEBHOOK_TOKEN.default
    ):
        return False

    header = request.headers.get(
        "Authorization",
        "",
    )
    prefix = "Bearer "

    if not header.startswith(prefix):
        return False

    return compare_digest(
        header[len(prefix):].strip(),
        expected,
    )
