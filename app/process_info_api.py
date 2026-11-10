from __future__ import annotations

from .mobile_transaction import commit_mobile_changes, mobile_transaction_active

from typing import Any

from flask import (
    Blueprint,
    current_app,
    jsonify,
    request,
)
from flask_jwt_extended import jwt_required

from .api import (
    current_api_user,
    editable_commission,
    viewable_commission,
)
from .extensions import db
from .models import (
    ProcessInfo,
    ProcessInfoRead,
)
from .process_info_service import (
    create_outgoing_info,
    is_info_unread,
    mark_process_infos_read,
    sync_process_infos,
    unread_summary_for_user,
    write_outgoing_info,
)
from .utils import iso_datetime
from .process_info_notifications import (
    read_receipts,
    record_visible_reads,
    visible_items,
)


process_info_api_bp = Blueprint(
    "process_info_api",
    __name__,
    url_prefix="/api",
)


@process_info_api_bp.get(
    "/commissions/<int:commission_id>/infos"
)
@jwt_required()
def commission_infos(
    commission_id: int,
) -> Any:
    user = current_api_user()
    commission = viewable_commission(
        commission_id,
        user,
    )
    process_erp_id = (
        commission.crafts_process_erp_id
    )

    if process_erp_id is None:
        return jsonify(
            {
                "items": [],
                "sync_error": None,
            }
        )

    sync_error = None

    if not mobile_transaction_active():
        try:
            sync_process_infos(
                process_erp_id,
                notify=True,
            )
            commit_mobile_changes()
        except Exception as e:
            db.session.rollback()
            current_app.logger.exception(
                "process_info_api_sync_failed "
                "process_erp_id=%s exception=%s",
                process_erp_id, str(e)
            )
            return jsonify(
                {
                    "items": [],
                    "sync_error": None,
                }
            ), 200

    items = (
        ProcessInfo.query.filter(
            ProcessInfo.process_erp_id
            == process_erp_id,
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

    return jsonify(
        {
            "items": [
                serialize_process_info(
                    item,
                    user.id,
                )
                for item in items
            ],
            "sync_error": sync_error,
        }
    )


@process_info_api_bp.post(
    "/commissions/<int:commission_id>/infos"
)
@jwt_required()
def create_commission_info(
    commission_id: int,
) -> Any:
    user = current_api_user()
    commission = editable_commission(
        commission_id,
        user,
    )
    data = request.get_json(
        silent=True
    )

    if not isinstance(data, dict):
        return jsonify(
            {
                "message": (
                    "Ungültiger JSON-Body."
                )
            }
        ), 400

    try:
        item = create_outgoing_info(
            commission=commission,
            user=user,
            request_id=str(
                data.get("request_id", "")
            ),
            body=data.get("body"),
        )

        if item.state != "synced" and not mobile_transaction_active():
            item = write_outgoing_info(
                item.id
            )

    except ValueError as exc:
        db.session.rollback()
        return jsonify(
            {"message": str(exc)}
        ), 400

    except Exception as exc:
        return jsonify(
            {"message": str(exc)}
        ), 502

    return jsonify(
        {
            "item": serialize_process_info(
                item,
                user.id,
            )
        }
    ), 201


@process_info_api_bp.post(
    "/commissions/<int:commission_id>/infos/read"
)
@jwt_required()
def read_commission_infos(
    commission_id: int,
) -> Any:
    user = current_api_user()
    commission = viewable_commission(
        commission_id,
        user,
    )

    if commission.user_id != user.id:
        return jsonify(
            {
                "message": (
                    "Der Auftrag ist diesem "
                    "Benutzer nicht zugeordnet."
                )
            }
        ), 403

    if (
        commission.crafts_process_erp_id
        is None
    ):
        return jsonify(
            unread_summary_for_user(
                user.id
            )
        )

    data = request.get_json(
        silent=True
    )
    if isinstance(data, dict) and "items" in data:
        try:
            record_visible_reads(
                commission.crafts_process_erp_id,
                visible_items(data),
                "office",
                user_id=user.id,
            )
            commit_mobile_changes()
        except ValueError as exc:
            db.session.rollback()
            return jsonify({"message": str(exc)}), 400

        return jsonify(unread_summary_for_user(user.id))
    info_ids = (
        data.get("info_ids")
        if isinstance(data, dict)
        else None
    )

    if (
        not isinstance(info_ids, list)
        or any(
            not isinstance(info_id, str)
            for info_id in info_ids
        )
    ):
        return jsonify(
            {
                "message": (
                    "info_ids muss eine "
                    "Liste sein."
                )
            }
        ), 400

    try:
        mark_process_infos_read(
            commission
            .crafts_process_erp_id,
            user.id,
            info_ids,
        )
    except ValueError as exc:
        db.session.rollback()
        return jsonify(
            {"message": str(exc)}
        ), 403

    commit_mobile_changes()

    return jsonify(
        unread_summary_for_user(
            user.id
        )
    )


@process_info_api_bp.get(
    "/process-infos/unread"
)
@jwt_required()
def unread_process_infos() -> Any:
    user = current_api_user()

    return jsonify(
        unread_summary_for_user(
            user.id
        )
    )


def serialize_process_info(
    item: ProcessInfo,
    user_id: int,
) -> dict[str, Any]:
    receipt = db.session.get(
        ProcessInfoRead,
        (item.id, user_id),
    )

    return {
        "id": item.id,
        "process_erp_id": (
            item.process_erp_id
        ),
        "body": item.body,
        "author_name": item.visible_author_name,
        "direction": item.direction,
        "date": (
            item.erp_date.isoformat()
            if item.erp_date
            else None
        ),
        "created_at": iso_datetime(
            item.created_at
        ),
        "version": item.version,
        "state": item.state,
        "mail_state": item.mail_state,
        "read_receipts": read_receipts(item),
        "is_unread": (
            is_info_unread(
                item,
                user_id,
            )
            if (
                receipt is None
                or receipt.read_version
                < item.version
            )
            else False
        ),
    }
