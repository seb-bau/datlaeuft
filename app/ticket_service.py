from __future__ import annotations

import hashlib
import json
from typing import Any

from .extensions import db
from .models import (
    CraftActivity,
    TicketAiState,
    TicketMirror,
    TicketMirrorState,
    WowiLicenseAgreement,
    utc_now,
)
from .openai_service import analyze_ticket_content
from .utils import parse_datetime
from .wowiport import WowiportService


def sync_ticket_mirror(
    ticket_erp_id: int,
    service: WowiportService | None = None,
    *,
    deleted: bool = False,
) -> TicketMirror | None:
    ticket = TicketMirror.query.filter_by(
        erp_id=ticket_erp_id
    ).one_or_none()

    if deleted:
        if ticket is not None:
            ticket.deleted_at = utc_now()
            ticket.synced_at = utc_now()

        return ticket

    wowiport = service or WowiportService()
    rows = wowiport.get_ticket_rows(
        ticket_erp_id=ticket_erp_id,
        include_comments=True,
    )
    row = next(
        (
            item
            for item in rows
            if as_int(value(item, "Id"))
            == ticket_erp_id
        ),
        None,
    )

    if row is None:
        raise RuntimeError(
            f"OPENWOWI-Ticket {ticket_erp_id} "
            "wurde nicht gefunden."
        )

    return upsert_ticket_row(row)


def sync_all_ticket_mirrors(
    service: WowiportService | None = None,
) -> dict[str, int]:
    wowiport = service or WowiportService()
    rows = wowiport.get_ticket_rows(
        include_comments=False
    )
    result = {
        "seen": 0,
        "created": 0,
        "updated": 0,
    }

    for row in rows:
        ticket_erp_id = required_positive_int(
            value(row, "Id"),
            "Ticket-ID",
        )
        existing = TicketMirror.query.filter_by(
            erp_id=ticket_erp_id
        ).one_or_none()

        upsert_ticket_row(row)
        result["seen"] += 1
        result[
            "created"
            if existing is None
            else "updated"
        ] += 1

    return result


def analyze_ticket_mirror(
    ticket: TicketMirror,
    actor_user_id: int,
    service: WowiportService | None = None,
) -> TicketMirror:
    wowiport = service or WowiportService()
    rows = wowiport.get_ticket_rows(
        ticket_erp_id=ticket.erp_id,
        include_comments=True,
    )
    row = next(
        (
            item
            for item in rows
            if as_int(value(item, "Id"))
            == ticket.erp_id
        ),
        None,
    )

    if row is None:
        raise RuntimeError(
            f"OPENWOWI-Ticket {ticket.erp_id} "
            "wurde nicht gefunden."
        )

    ticket = upsert_ticket_row(row)

    crafts = (
        CraftActivity.query
        .filter(
            CraftActivity.active.is_(True),
            CraftActivity.app_enabled.is_(True),
            CraftActivity.erp_id.isnot(None),
        )
        .order_by(
            CraftActivity.name.asc(),
            CraftActivity.id.asc(),
        )
        .all()
    )

    craft_options = [
        {
            "id": item.id,
            "code": item.code,
            "name": item.name,
        }
        for item in crafts
    ]

    result, model = analyze_ticket_content(
        analysis_ticket_data(row),
        craft_options,
        actor_user_id,
    )

    craft_ids = {
        item.id
        for item in crafts
    }
    craft_activity_id = as_int(
        result.get("craft_activity_id")
    )

    if craft_activity_id not in craft_ids:
        craft_activity_id = None

    missing_information = result.get(
        "missing_information"
    )

    if not isinstance(missing_information, list):
        missing_information = []

    ticket.ai_state = TicketAiState.READY.value
    ticket.ai_candidate = bool(
        result.get("is_commission_candidate")
    )
    ticket.ai_classification = limited_text(
        result.get("classification"),
        30,
    )
    ticket.ai_craft_activity_id = (
        craft_activity_id
    )
    ticket.ai_title = limited_text(
        result.get("title"),
        255,
    )
    ticket.ai_commission_text = limited_text(
        result.get("commission_text"),
        4000,
    )
    ticket.ai_damage_location = limited_text(
        result.get("damage_location"),
        255,
    )
    ticket.ai_missing_information = [
        text
        for text in (
            limited_text(item, 255)
            for item in missing_information[:10]
        )
        if text
    ]
    ticket.ai_model = model
    ticket.ai_input_hash = ticket.source_signature
    ticket.ai_generated_at = utc_now()
    ticket.ai_error = None

    return ticket


def upsert_ticket_row(
    row: dict[str, Any],
) -> TicketMirror:
    ticket_erp_id = required_positive_int(
        value(row, "Id"),
        "Ticket-ID",
    )
    ticket = TicketMirror.query.filter_by(
        erp_id=ticket_erp_id
    ).one_or_none()
    signature = ticket_signature(row)

    if ticket is None:
        ticket = TicketMirror(
            erp_id=ticket_erp_id,
            state=TicketMirrorState.NEW.value,
            ai_state=(
                TicketAiState.NOT_REQUESTED.value
            ),
        )
        db.session.add(ticket)
    elif (
        ticket.source_signature
        and ticket.source_signature != signature
        and ticket.ai_state
        == TicketAiState.READY.value
    ):
        ticket.ai_state = TicketAiState.STALE.value

    status_id, status_code = reference(
        row,
        "Status",
        "Code",
    )
    priority_id, priority_code = reference(
        row,
        "Priority",
        "Code",
    )
    source_id, source_code = reference(
        row,
        "Source",
        "Code",
    )
    department_id, department_name = reference(
        row,
        "Department",
        "Name",
    )

    ticket.id_num = limited_text(
        value(row, "IdNum"),
        120,
    )
    ticket.subject = limited_text(
        value(row, "Subject"),
        500,
    )
    ticket.content_excerpt = ticket_excerpt(row)
    ticket.received_at = parse_datetime(
        value(row, "TimeReceived")
    )
    ticket.status_erp_id = status_id
    ticket.status_code = status_code
    ticket.priority_erp_id = priority_id
    ticket.priority_code = priority_code
    ticket.source_erp_id = source_id
    ticket.source_code = source_code
    ticket.department_erp_id = department_id
    ticket.department_name = department_name
    ticket.assigned_user_erp_id = as_int(
        value(row, "UserId")
    )
    ticket.assignments = ticket_assignments(row)
    ticket.source_signature = signature
    ticket.synced_at = utc_now()
    ticket.deleted_at = None

    return ticket


def analysis_ticket_data(
    row: dict[str, Any],
) -> dict[str, Any]:
    comments = []

    for comment in (
        value(row, "Comments") or []
    )[-30:]:
        if not isinstance(comment, dict):
            continue

        content = limited_text(
            value(comment, "Content"),
            2000,
        )

        if not content:
            continue

        comments.append(
            {
                "direction": (
                    "incoming"
                    if value(
                        comment,
                        "InboundComment",
                    ) is True
                    else "outgoing"
                ),
                "content": content,
            }
        )

    return {
        "subject": limited_text(
            value(row, "Subject"),
            1000,
        ),
        "content": limited_text(
            value(row, "Content"),
            8000,
        ),
        "comments": comments,
    }


def ticket_assignments(
    row: dict[str, Any],
) -> list[dict[str, Any]]:
    assignments = []
    main_assignment = value(
        row,
        "MainAssignment",
    )

    if isinstance(main_assignment, dict):
        assignments.append(main_assignment)

    for assignment in (
        value(row, "Assignment") or []
    ):
        if (
            isinstance(assignment, dict)
            and assignment not in assignments
        ):
            assignments.append(assignment)

    return assignments


def ticket_license_agreement_local_id(
    ticket: TicketMirror,
) -> int | None:
    assignments = (
        ticket.assignments
        if isinstance(ticket.assignments, list)
        else []
    )
    agreement_erp_ids: set[int] = set()

    for assignment in assignments:
        if not isinstance(assignment, dict):
            continue

        assignment_entity = value(
            assignment,
            "AssignmentEntity",
        )

        if not isinstance(assignment_entity, dict):
            continue

        entity_code = "".join(
            character
            for character in str(
                value(
                    assignment_entity,
                    "Code",
                )
                or ""
            ).casefold()
            if character.isalnum()
        )

        if not any(
            code_part in entity_code
            for code_part in (
                "licenseagreement",
                "leaseagreement",
                "rentalagreement",
                "tenancyagreement",
                "mietvertrag",
                "vertrag"
            )
        ):
            continue

        entity_id = as_int(
            value(
                assignment,
                "EntityId",
            )
        )

        if entity_id is not None:
            agreement_erp_ids.add(entity_id)

    if len(agreement_erp_ids) != 1:
        return None

    agreement = (
        WowiLicenseAgreement.query
        .filter(
            WowiLicenseAgreement.erp_id
            == next(iter(agreement_erp_ids)),
            WowiLicenseAgreement.active.is_(True),
        )
        .one_or_none()
    )

    return (
        agreement.id
        if agreement is not None
        else None
    )


def ticket_excerpt(
    row: dict[str, Any],
) -> str | None:
    content = " ".join(
        str(value(row, "Content") or "").split()
    )
    return content[:1200] or None


def ticket_signature(
    row: dict[str, Any],
) -> str:
    comments = []

    for comment in value(row, "Comments") or []:
        if not isinstance(comment, dict):
            continue

        comments.append(
            {
                "id": value(comment, "Id"),
                "content": value(
                    comment,
                    "Content",
                ),
                "inbound": value(
                    comment,
                    "InboundComment",
                ),
            }
        )

    data = {
        "subject": value(row, "Subject"),
        "content": value(row, "Content"),
        "status": value(row, "Status"),
        "priority": value(row, "Priority"),
        "source": value(row, "Source"),
        "department": value(row, "Department"),
        "user_id": value(row, "UserId"),
        "assignments": ticket_assignments(row),
        "comments": comments,
    }
    serialized = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )

    return hashlib.sha256(
        serialized.encode("utf-8")
    ).hexdigest()


def reference(
    row: dict[str, Any],
    name: str,
    label_name: str,
) -> tuple[int | None, str | None]:
    item = value(row, name)

    if not isinstance(item, dict):
        return None, None

    return (
        as_int(value(item, "Id")),
        limited_text(
            value(item, label_name),
            255,
        ),
    )


def value(
    row: dict[str, Any],
    name: str,
) -> Any:
    return next(
        (
            item
            for key, item in row.items()
            if key.casefold() == name.casefold()
        ),
        None,
    )


def required_positive_int(
    value_to_parse: Any,
    label: str,
) -> int:
    parsed = as_int(value_to_parse)

    if parsed is None or parsed <= 0:
        raise RuntimeError(
            f"OPENWOWI hat eine ungültige "
            f"{label} geliefert."
        )

    return parsed


def as_int(
    value_to_parse: Any,
) -> int | None:
    if (
        isinstance(value_to_parse, bool)
        or value_to_parse is None
        or value_to_parse == ""
    ):
        return None

    try:
        return int(value_to_parse)
    except (
        TypeError,
        ValueError,
    ):
        return None


def limited_text(
    value_to_parse: Any,
    maximum: int,
) -> str | None:
    if value_to_parse is None:
        return None

    text = str(value_to_parse).strip()
    return text[:maximum] or None