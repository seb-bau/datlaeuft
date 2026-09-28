from __future__ import annotations

from typing import Any

from flask import g, has_request_context, request
from flask_login import current_user

from .extensions import db
from decimal import Decimal
from enum import Enum

from .models import AuditLog, ExternalCraftsman, User

def audit(
    action: str,
    entity_type: str | None = None,
    entity_id: str | int | None = None,
    metadata: dict[str, Any] | None = None,
    *,
    actor_user_id: int | None = None,
    actor_external_craftsman_id: int | None = None,
) -> None:
    ip_address = None
    user_agent = None

    if has_request_context():
        ip_address = request.headers.get(
            "X-Forwarded-For",
            request.remote_addr,
        )
        user_agent = request.headers.get(
            "User-Agent"
        )
        api_user = getattr(
            g,
            "api_user",
            None,
        )

        if (
            actor_user_id is None
            and actor_external_craftsman_id is None
        ):
            if api_user is not None:
                actor_user_id = api_user.id
            elif getattr(
                current_user,
                "is_authenticated",
                False,
            ):
                actor_user_id = current_user.id
            else:
                external_craftsman = getattr(
                    g,
                    "external_craftsman",
                    None,
                )

                if external_craftsman is not None:
                    actor_external_craftsman_id = (
                        external_craftsman.id
                    )

    record_metadata = dict(metadata or {})

    if actor_user_id is not None:
        actor = db.session.get(User, actor_user_id)
        record_metadata["actor_label"] = (
            actor.display_name if actor else f"Benutzer #{actor_user_id}"
        )
    elif actor_external_craftsman_id is not None:
        actor = db.session.get(
            ExternalCraftsman,
            actor_external_craftsman_id,
        )
        record_metadata["actor_label"] = (
            actor.display_name
            if actor
            else f"Externer Handwerker #{actor_external_craftsman_id}"
        )
    else:
        record_metadata["actor_label"] = "System"

    db.session.add(
        AuditLog(
            actor_user_id=actor_user_id,
            actor_external_craftsman_id=(
                actor_external_craftsman_id
            ),
            action=action,
            entity_type=entity_type,
            entity_id=(
                str(entity_id)
                if entity_id is not None
                else None
            ),
            metadata_json=record_metadata,
            ip_address=ip_address,
            user_agent=user_agent,
        )
    )


AUDIT_FIELDS = {
    "inventory_item": (
        "item_number",
        "name",
        "item_type_id",
        "unit",
        "ean",
        "reorder_level",
        "billing_base_price_net",
        "billing_vat_rate",
        "active",
    ),
}


def audit_snapshot(entity: Any, entity_type: str) -> dict[str, Any]:
    fields = AUDIT_FIELDS[entity_type]
    return {field: getattr(entity, field) for field in fields}


def audit_value(value: Any) -> str | int | float | bool | None:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Nicht freigegebener Audit-Wert: {type(value).__name__}")


def audit_diff(
    action: str,
    entity_type: str,
    entity_id: str | int,
    before: dict[str, Any],
    after: dict[str, Any],
) -> None:
    changes = {}

    for field in AUDIT_FIELDS[entity_type]:
        old_value = before.get(field)
        new_value = after.get(field)

        if old_value != new_value:
            changes[field] = {
                "old": audit_value(old_value),
                "new": audit_value(new_value),
            }

    if changes:
        audit(
            action,
            entity_type,
            entity_id,
            metadata={"changes": changes},
        )
