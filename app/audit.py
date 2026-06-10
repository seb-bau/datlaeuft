from __future__ import annotations

from typing import Any

from flask import has_request_context, request
from flask_login import current_user

from .extensions import db
from .models import AuditLog


def audit(action: str, entity_type: str | None = None, entity_id: str | int | None = None, metadata: dict[str, Any] | None = None) -> None:
    actor_user_id = None
    ip_address = None
    user_agent = None
    if has_request_context():
        ip_address = request.headers.get("X-Forwarded-For", request.remote_addr)
        user_agent = request.headers.get("User-Agent")
        if getattr(current_user, "is_authenticated", False):
            actor_user_id = current_user.id
    db.session.add(AuditLog(actor_user_id=actor_user_id, action=action, entity_type=entity_type, entity_id=str(entity_id) if entity_id is not None else None, metadata_json=metadata, ip_address=ip_address, user_agent=user_agent))