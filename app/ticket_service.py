from __future__ import annotations

import hashlib
import json
from typing import Any, Callable
from .external_craftsman_service import (
    available_external_craftsmen,
)
from .extensions import db
from .models import (
    CraftActivity,
    TicketAiAssignmentMode,
    TicketAiAssignmentRule,
    TicketAiState,
    TicketMirror,
    TicketMirrorState,
    WowiLicenseAgreement,
    utc_now, TimeTaskTemplate,
)
from .object_directory import (
    commission_service_positions,
)
from .openai_service import (
    TICKET_ANALYSIS_PROMPT_VERSION,
    analyze_ticket_content,
)
from .settings import SettingKeys, settings
from .utils import parse_datetime
from .wowiport import WowiportService


def sync_ticket_mirror(
    ticket_erp_id: int,
    service: WowiportService | None = None,
    *,
    deleted: bool = False,
) -> TicketMirror | None:
    if deleted:
        ticket = (
            TicketMirror.query.filter_by(erp_id=ticket_erp_id)
            .populate_existing().with_for_update().one_or_none()
        )
        if ticket is not None:
            ticket.deleted_at = utc_now()
            ticket.synced_at = utc_now()

        return ticket

    return upsert_ticket_row(fetch_ticket_row(ticket_erp_id, service))


def fetch_ticket_row(
    ticket_erp_id: int,
    service: WowiportService | None = None,
) -> dict[str, Any]:
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

    return row


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
    *,
    validate_result: Callable[[int, str], TicketMirror] | None = None,
) -> TicketMirror:
    ticket_id = ticket.id
    ticket_erp_id = ticket.erp_id
    original_input_hash = ticket_analysis_input_hash(ticket)
    db.session.commit()
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

    if validate_result is not None:
        ticket = validate_result(ticket_id, original_input_hash)
    ticket = upsert_ticket_row(row, queue_analysis=False)

    configuration = ticket_analysis_configuration()
    crafts = configuration["crafts"]
    craft_options = configuration["craft_options"]
    service_positions = configuration[
        "service_positions"
    ]
    service_position_options = configuration[
        "service_position_options"
    ]
    assignment_rules = configuration[
        "assignment_rules"
    ]
    assignment_rule_options = configuration[
        "assignment_rule_options"
    ]

    ticket_id = ticket.id
    input_hash = build_ticket_analysis_input_hash(ticket, configuration)
    db.session.commit()
    if validate_result is not None:
        ticket = validate_result(ticket_id, input_hash)
        db.session.commit()

    result, model = analyze_ticket_content(
        analysis_ticket_data(row),
        craft_options,
        service_position_options,
        assignment_rule_options,
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

    service_positions_by_id = {
        item.id: item
        for item in service_positions
    }
    service_position_id = as_int(
        result.get("service_position_id")
    )
    selected_service_position = (
        service_positions_by_id.get(
            service_position_id
        )
    )

    if (
        selected_service_position is None
        or selected_service_position
        .craft_activity_id
        != craft_activity_id
    ):
        service_position_id = None

    missing_information = result.get(
        "missing_information"
    )

    if not isinstance(missing_information, list):
        missing_information = []

    assignment_rule = resolve_assignment_rule(
        result,
        assignment_rules,
    )

    if assignment_rule is not None:
        if (
                assignment_rule
                        .target_task_template_id
                is not None
        ):
            selected_service_position = (
                service_positions_by_id.get(
                    assignment_rule
                    .target_task_template_id
                )
            )

            if selected_service_position is None:
                raise RuntimeError(
                    "Die ausgewählte KI-Regel verweist "
                    "auf eine nicht verfügbare "
                    "Leistungsposition."
                )

            service_position_id = (
                selected_service_position.id
            )
            craft_activity_id = (
                selected_service_position
                .craft_activity_id
            )

        if (
                assignment_rule
                        .target_craft_activity_id
                is not None
        ):
            craft_activity_id = (
                assignment_rule
                .target_craft_activity_id
            )

            if (
                    selected_service_position is not None
                    and selected_service_position
                    .craft_activity_id
                    != craft_activity_id
            ):
                service_position_id = None
                selected_service_position = None

    if (
        assignment_rule is not None
        and assignment_rule.target_external_craftsman_id
        is not None
    ):
        available_external_ids = {
            craftsman.id
            for craftsman in available_external_craftsmen(
                craft_activity_id
            )
        } if craft_activity_id is not None else set()

        if (
            assignment_rule.target_external_craftsman_id
            not in available_external_ids
        ):
            raise RuntimeError(
                "Der externe Handwerker der "
                "KI-Zuweisungsregel ist für das "
                "ermittelte Gewerk nicht verfügbar."
            )
    if validate_result is not None:
        ticket = validate_result(ticket_id, input_hash)

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

    ticket.ai_task_template_id = (
        service_position_id
    )

    ticket.ai_assignment_rule_id = (
        assignment_rule.id
        if assignment_rule is not None
        else None
    )
    ticket.ai_assigned_user_id = (
        assignment_rule.target_user_id
        if assignment_rule is not None
        else None
    )
    ticket.ai_assignment_mode = (
        assignment_rule.enforcement_mode
        if assignment_rule is not None
        else None
    )

    ticket.ai_model = model
    ticket.ai_input_hash = input_hash
    ticket.ai_generated_at = utc_now()
    ticket.ai_error = None

    return ticket


def ticket_analysis_configuration() -> dict[
    str,
    Any,
]:
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
    craft_ids = {
        item.id
        for item in crafts
    }
    service_positions = (
        commission_service_positions()
        .filter(
            TimeTaskTemplate.craft_activity_id.in_(
                craft_ids
            )
        )
        .order_by(
            TimeTaskTemplate.craft_activity_id.asc(),
            TimeTaskTemplate.title.asc(),
            TimeTaskTemplate.code.asc(),
        )
        .all()
    )
    service_position_options = [
        {
            "id": item.id,
            "craft_activity_id": (
                item.craft_activity_id
            ),
            "code": item.code,
            "title": item.title,
            "erp_idnum": item.erp_idnum,
        }
        for item in service_positions
    ]
    assignment_rules = (
        TicketAiAssignmentRule.query
        .filter(
            TicketAiAssignmentRule.active.is_(True)
        )
        .order_by(
            TicketAiAssignmentRule.priority.desc(),
            TicketAiAssignmentRule.id.asc(),
        )
        .all()
    )
    allowed_modes = {
        item.value
        for item in TicketAiAssignmentMode
    }

    for rule in assignment_rules:
        target_user = rule.target_user
        target_craft_activity = (
            rule.target_craft_activity
        )
        target_task_template = (
            rule.target_task_template
        )

        if (
            rule.target_user_id is None
            and rule.target_external_craftsman_id is None
            and rule.target_assignment_type is None
            and rule.target_craft_activity_id is None
            and rule.target_task_template_id is None
        ):
            raise RuntimeError(
                "Die aktive KI-Zuweisungsregel "
                f"„{rule.name}“ besitzt kein Ergebnis."
            )

        if rule.target_assignment_type not in {
            None,
            "internal",
            "external",
        }:
            raise RuntimeError(
                "Die aktive KI-Zuweisungsregel "
                f"„{rule.name}“ hat eine ungültige "
                "Vergabeart."
            )

        if (
            rule.target_assignment_type == "external"
            and rule.target_user_id is not None
        ):
            raise RuntimeError(
                f"Die KI-Zuweisungsregel „{rule.name}“ "
                "kombiniert externe Vergabe mit einem "
                "internen Handwerker."
            )

        if (
            rule.target_assignment_type != "external"
            and rule.target_external_craftsman_id
            is not None
        ):
            raise RuntimeError(
                f"Die KI-Zuweisungsregel „{rule.name}“ "
                "kombiniert einen externen Handwerker "
                "mit einer anderen Vergabeart."
            )

        if (
            rule.target_user_id is not None
            and (
                target_user is None
                or not target_user.is_active
                or target_user.craftsman_erp_id is None
            )
        ):
            raise RuntimeError(
                "Die aktive KI-Zuweisungsregel "
                f"„{rule.name}“ verweist nicht auf "
                "einen aktiven Handwerker."
            )

        if (
                target_craft_activity is not None
                and (
                not target_craft_activity.active
                or not target_craft_activity.app_enabled
        )
        ):
            raise RuntimeError(
                "Die aktive KI-Zuweisungsregel "
                f"„{rule.name}“ verweist auf ein "
                "nicht verfügbares Gewerk."
            )

        if (
                target_task_template is not None
                and (
                not target_task_template.active
                or target_task_template
                        .erp_service_catalogue_id is None
        )
        ):
            raise RuntimeError(
                "Die aktive KI-Zuweisungsregel "
                f"„{rule.name}“ verweist auf eine "
                "nicht verfügbare Leistungsposition."
            )

        if (
                target_craft_activity is not None
                and target_task_template is not None
                and target_task_template
                .craft_activity_id
                != target_craft_activity.id
        ):
            raise RuntimeError(
                "Gewerk und Leistungsposition der "
                f"KI-Zuweisungsregel „{rule.name}“ "
                "passen nicht zusammen."
            )

        if rule.target_assignment_type == "external":
            effective_craft_id = (
                rule.target_craft_activity_id
                or (
                    target_task_template.craft_activity_id
                    if target_task_template is not None
                    else None
                )
            )
            available_external_ids = {
                craftsman.id
                for craftsman in available_external_craftsmen(
                    effective_craft_id
                )
            }

            if not available_external_ids:
                raise RuntimeError(
                    f"Für die KI-Zuweisungsregel „{rule.name}“ "
                    "ist kein externer Handwerker verfügbar."
                )

            if (
                rule.target_external_craftsman_id
                is not None
                and rule.target_external_craftsman_id
                not in available_external_ids
            ):
                raise RuntimeError(
                    f"Der externe Handwerker der "
                    f"KI-Zuweisungsregel „{rule.name}“ "
                    "ist nicht verfügbar."
                )

        if rule.enforcement_mode not in allowed_modes:
            raise RuntimeError(
                "Die aktive KI-Zuweisungsregel "
                f"„{rule.name}“ hat ein ungültiges "
                "Verhalten."
            )

    assignment_rule_options = [
        {
            "id": rule.id,
            "name": rule.name,
            "condition": rule.condition_text,
            "priority": rule.priority,
        }
        for rule in assignment_rules
    ]

    return {
        "crafts": crafts,
        "craft_options": craft_options,
        "service_positions": service_positions,
        "service_position_options": (
            service_position_options
        ),
        "assignment_rules": assignment_rules,
        "assignment_rule_options": (
            assignment_rule_options
        ),
    }


def ticket_analysis_input_hash(
    ticket: TicketMirror,
) -> str:
    return build_ticket_analysis_input_hash(
        ticket,
        ticket_analysis_configuration(),
    )


def build_ticket_analysis_input_hash(
    ticket: TicketMirror,
    configuration: dict[str, Any],
) -> str:
    assignment_rules = configuration[
        "assignment_rules"
    ]
    data = {
        "prompt_version": (
            TICKET_ANALYSIS_PROMPT_VERSION
        ),
        "ticket_source_signature": (
            ticket.source_signature
        ),
        "model": str(
            settings.get(SettingKeys.OPENAI_MODEL)
        ).strip(),
        "crafts": configuration["craft_options"],
        "service_positions": configuration[
            "service_position_options"
        ],
        "assignment_rules": [
            {
                "id": rule.id,
                "name": rule.name,
                "condition": rule.condition_text,
                "target_user_id": rule.target_user_id,
                "target_assignment_type": (
                    rule.target_assignment_type
                ),
                "target_external_craftsman_id": (
                    rule.target_external_craftsman_id
                ),
                "target_craft_activity_id": (
                    rule.target_craft_activity_id
                ),
                "target_task_template_id": (
                    rule.target_task_template_id
                ),
                "enforcement_mode": (
                    rule.enforcement_mode
                ),
                "priority": rule.priority,
            }
            for rule in assignment_rules
        ],
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


def resolve_assignment_rule(
    result: dict[str, Any],
    assignment_rules: list[
        TicketAiAssignmentRule
    ],
) -> TicketAiAssignmentRule | None:
    raw_rule_ids = result.get(
        "matched_assignment_rule_ids"
    )

    if not isinstance(raw_rule_ids, list):
        raise RuntimeError(
            "Die KI-Analyse hat keine gültigen "
            "Zuweisungsregeln geliefert."
        )

    rules_by_id = {
        rule.id: rule
        for rule in assignment_rules
    }
    matched_rule_ids = set()

    for raw_rule_id in raw_rule_ids:
        rule_id = as_int(raw_rule_id)

        if rule_id not in rules_by_id:
            raise RuntimeError(
                "Die KI-Analyse hat eine ungültige "
                "Zuweisungsregel geliefert."
            )

        matched_rule_ids.add(rule_id)

    if not matched_rule_ids:
        return None

    matched_rules = [
        rules_by_id[rule_id]
        for rule_id in matched_rule_ids
    ]
    highest_priority = max(
        rule.priority
        for rule in matched_rules
    )
    highest_priority_rules = [
        rule
        for rule in matched_rules
        if rule.priority == highest_priority
    ]
    target_signatures = {
        (
            rule.target_user_id,
            rule.target_assignment_type,
            rule.target_external_craftsman_id,
            rule.target_craft_activity_id,
            rule.target_task_template_id,
            rule.enforcement_mode,
        )
        for rule in highest_priority_rules
    }

    if len(target_signatures) != 1:
        raise RuntimeError(
            "Mehrere gleich priorisierte "
            "KI-Zuweisungsregeln haben "
            "unterschiedliche Ergebnisse."
        )

    return min(
        highest_priority_rules,
        key=lambda rule: rule.id,
    )


def mark_ticket_analyses_stale() -> int:
    from .ticket_ai_service import enqueue_ticket_ai

    tickets = TicketMirror.query.filter_by(ai_state=TicketAiState.READY.value).all()
    for ticket in tickets:
        ticket.ai_state = TicketAiState.STALE.value
        enqueue_ticket_ai(ticket)
    return len(tickets)


def upsert_ticket_row(
    row: dict[str, Any],
    *,
    queue_analysis: bool = True,
) -> TicketMirror:
    ticket_erp_id = required_positive_int(
        value(row, "Id"),
        "Ticket-ID",
    )
    ticket = (
        TicketMirror.query.filter_by(erp_id=ticket_erp_id)
        .populate_existing().with_for_update().one_or_none()
    )
    signature = ticket_signature(row)
    is_new = ticket is None
    changed = ticket is not None and ticket.source_signature != signature

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

    if queue_analysis and (is_new or changed):
        from .ticket_ai_service import enqueue_ticket_ai

        enqueue_ticket_ai(ticket)

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
