from __future__ import annotations

from typing import Any

from .audit import audit
from .extensions import db
from .models import (
    Commission,
    CommissionLocalStatus,
    CommissionTicketLink,
    PushEventType,
)
from .push_service import enqueue_commission_push


def parse_commission_priority(value: Any) -> bool:
    if value not in ("normal", "high"):
        raise ValueError("Die Priorität muss Normal oder Hoch sein.")
    return value == "high"


def set_manual_commission_priority(
    commission: Commission,
    value: str,
) -> bool:
    high_priority = parse_commission_priority(value)
    db.session.flush()
    db.session.refresh(
        commission,
        attribute_names=["is_high_priority", "priority_is_manual"],
        with_for_update=True,
    )
    result = db.session.execute(
        db.update(Commission)
        .where(
            Commission.id == commission.id,
            Commission.is_high_priority.is_(not high_priority),
        )
        .values(is_high_priority=high_priority, priority_is_manual=True)
        .execution_options(synchronize_session=False)
    )
    db.session.expire(commission, ["is_high_priority", "priority_is_manual"])
    if result.rowcount != 1:
        return False

    audit(
        "commission.priority.web", "commission", commission.id,
        {"priority": value, "priority_is_manual": True},
    )
    if high_priority:
        enqueue_commission_push(
            commission, PushEventType.COMMISSION_PRIORITY_RAISED,
        )
    return True


def _ticket_value(row: dict[str, Any], name: str) -> Any:
    return next(
        (value for key, value in row.items() if key.casefold() == name.casefold()),
        None,
    )


def _ticket_id(value: Any) -> int:
    if isinstance(value, bool) or not str(value).isdigit() or int(value) <= 0:
        raise RuntimeError("OPENWOWI hat eine ungültige Ticket-ID geliefert.")
    return int(value)


def sync_commission_priority(
    commission: Commission,
    service: Any,
    *,
    notify: bool = True,
) -> bool:
    db.session.flush()
    db.session.refresh(
        commission,
        attribute_names=["is_high_priority", "priority_is_manual"],
        with_for_update=True,
    )
    if (
        commission.priority_is_manual
        or commission.erp_id is None
        or commission.deleted_at is not None
        or commission.local_status == CommissionLocalStatus.DELETED.value
    ):
        return False

    tickets = service.get_priority_ticket_rows(commission_erp_id=commission.erp_id)
    ticket_ids: set[int] = set()
    high_priority = False

    for ticket in tickets:
        ticket_ids.add(_ticket_id(_ticket_value(ticket, "Id")))
        priority = _ticket_value(ticket, "Priority")
        if not isinstance(priority, dict):
            raise RuntimeError("OPENWOWI hat keine gültige Ticketpriorität geliefert.")
        priority_id = _ticket_id(_ticket_value(priority, "Id"))
        high_priority = high_priority or priority_id == 3

    links = {
        link.ticket_erp_id: link
        for link in CommissionTicketLink.query.filter_by(
            commission_id=commission.id,
        ).all()
    }
    for ticket_id, link in links.items():
        if ticket_id not in ticket_ids:
            db.session.delete(link)
    for ticket_id in ticket_ids - links.keys():
        db.session.add(CommissionTicketLink(
            commission_id=commission.id, ticket_erp_id=ticket_id,
        ))

    previous_priority = commission.is_high_priority
    changed = False
    if previous_priority != high_priority:
        result = db.session.execute(
            db.update(Commission)
            .where(
                Commission.id == commission.id,
                Commission.priority_is_manual.is_(False),
                Commission.is_high_priority.is_(previous_priority),
            )
            .values(is_high_priority=high_priority)
            .execution_options(synchronize_session=False)
        )
        changed = result.rowcount == 1

    db.session.expire(commission, ["is_high_priority", "priority_is_manual"])
    became_high = changed and high_priority

    if changed:
        audit(
            "commission.priority.wowiport", "commission", commission.id,
            {
                "priority": "high" if high_priority else "normal",
                "ticket_ids": sorted(ticket_ids),
            },
        )
    if notify and became_high:
        enqueue_commission_push(
            commission, PushEventType.COMMISSION_PRIORITY_RAISED,
        )
    return became_high


def sync_ticket_commission_priorities(
    ticket_erp_id: int,
    service: Any,
    *,
    deleted: bool = False,
) -> int:
    commission_ids = {
        row[0]
        for row in db.session.query(CommissionTicketLink.commission_id)
        .filter(CommissionTicketLink.ticket_erp_id == ticket_erp_id)
        .all()
    }
    erp_ids: set[int] = set()
    tickets = [] if deleted else service.get_priority_ticket_rows(
        ticket_erp_id=ticket_erp_id,
    )

    for ticket in tickets:
        assignments = _ticket_value(ticket, "Assignment") or []
        if not isinstance(assignments, list):
            raise RuntimeError("OPENWOWI hat ungültige Ticketverknüpfungen geliefert.")
        assignments = list(assignments)
        main_assignment = _ticket_value(ticket, "MainAssignment")
        if main_assignment:
            assignments.append(main_assignment)

        for assignment in assignments:
            if not isinstance(assignment, dict):
                raise RuntimeError("Eine OPENWOWI-Ticketverknüpfung ist ungültig.")
            entity = _ticket_value(assignment, "AssignmentEntity") or {}
            if _ticket_value(entity, "Id") in (7, "7"):
                erp_ids.add(_ticket_id(_ticket_value(assignment, "EntityId")))

    if erp_ids:
        commission_ids.update(
            row[0] for row in db.session.query(Commission.id)
            .filter(Commission.erp_id.in_(erp_ids)).all()
        )

    for commission_id in sorted(commission_ids):
        commission = db.session.get(Commission, commission_id)
        if commission is not None:
            sync_commission_priority(commission, service)

    return len(commission_ids)
