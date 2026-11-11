from __future__ import annotations

from typing import Any

from sqlalchemy import func

from .models import TicketMirror


def open_tickets_query(query: Any) -> Any:
    status_code = func.lower(func.trim(func.coalesce(TicketMirror.status_code, "")))
    return query.filter(
        TicketMirror.deleted_at.is_(None),
        status_code.not_in(("erledigt", "beauftragt")),
        TicketMirror.commission_id.is_(None),
        ~TicketMirror.commission_links.any(),
    )
