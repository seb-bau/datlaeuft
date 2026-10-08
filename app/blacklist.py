from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar

from flask import has_app_context, jsonify, session
from flask_login import logout_user
from sqlalchemy import event, or_, select
from sqlalchemy.orm import Session, with_loader_criteria

from .extensions import db, jwt
from .models import (
    BlacklistEntry,
    ExternalCraftsman,
    User,
    UserJurisdictionPersonMapping,
    WowiEconomicUnit,
    WowiEconomicUnitJurisdiction,
    WowiResponsibleOfficial,
)

_blacklist_bypass = ContextVar("blacklist_bypass", default=False)


@contextmanager
def include_blacklisted():
    token = _blacklist_bypass.set(True)
    try:
        yield
    finally:
        _blacklist_bypass.reset(token)


def clear_blacklist_cache(session, *args):
    session.info.pop("blacklist_ids", None)


def blacklist_ids(target_type):
    cached = db.session.info.get("blacklist_ids")
    if cached is not None:
        return cached[target_type]

    kinds = ("user", "official", "external", "person", "erp_user", "craftsman")
    blocked = {kind: set() for kind in kinds}
    entry_table = BlacklistEntry.__table__
    entries = db.session.execute(
        select(entry_table.c.target_type, entry_table.c.target_id)
    ).all()

    if entries:
        graph = defaultdict(set)

        def connect(left_type, left_id, right_type, right_id):
            if left_id is None or right_id is None:
                return
            left = (left_type, left_id)
            right = (right_type, right_id)
            graph[left].add(right)
            graph[right].add(left)

        user_table = User.__table__
        for user_id, person_id, craftsman_id in db.session.execute(
            select(
                user_table.c.id,
                user_table.c.person_erp_id,
                user_table.c.craftsman_erp_id,
            )
        ):
            connect("user", user_id, "person", person_id)
            connect("user", user_id, "craftsman", craftsman_id)

        official_table = WowiResponsibleOfficial.__table__
        for official_id, person_id, erp_user_id in db.session.execute(
            select(
                official_table.c.erp_id,
                official_table.c.person_erp_id,
                official_table.c.user_erp_id,
            )
        ):
            connect("official", official_id, "person", person_id)
            connect("official", official_id, "erp_user", erp_user_id)
            connect("person", person_id, "erp_user", erp_user_id)

        jurisdiction_table = WowiEconomicUnitJurisdiction.__table__
        for official_id, person_id, erp_user_id in db.session.execute(
            select(
                jurisdiction_table.c.responsible_official_erp_id,
                jurisdiction_table.c.responsible_person_erp_id,
                jurisdiction_table.c.responsible_user_erp_id,
            ).distinct()
        ):
            connect("official", official_id, "person", person_id)
            connect("official", official_id, "erp_user", erp_user_id)
            connect("person", person_id, "erp_user", erp_user_id)

        unit_table = WowiEconomicUnit.__table__
        for official_id, person_id, erp_user_id in db.session.execute(
            select(
                unit_table.c.universal_responsible_official_erp_id,
                unit_table.c.universal_responsible_person_erp_id,
                unit_table.c.universal_responsible_user_erp_id,
            ).distinct()
        ):
            connect("official", official_id, "person", person_id)
            connect("official", official_id, "erp_user", erp_user_id)
            connect("person", person_id, "erp_user", erp_user_id)

        external_table = ExternalCraftsman.__table__
        for external_id, craftsman_id in db.session.execute(
            select(external_table.c.id, external_table.c.craftsman_erp_id)
        ):
            connect("external", external_id, "craftsman", craftsman_id)

        pending = list(entries)
        visited = set()
        while pending:
            node = tuple(pending.pop())
            if node in visited:
                continue
            visited.add(node)
            blocked[node[0]].add(node[1])
            pending.extend(graph[node] - visited)

    db.session.info["blacklist_ids"] = blocked
    return blocked[target_type]


def is_blacklisted(target_type, target_id):
    return target_id is not None and target_id in blacklist_ids(target_type)


def apply_blacklist_filter(execute_state):
    if (
        not has_app_context()
        or not execute_state.is_select
        or not execute_state.is_orm_statement
        or execute_state.is_column_load
        or _blacklist_bypass.get()
    ):
        return

    user_ids = sorted(blacklist_ids("user"))
    official_ids = sorted(blacklist_ids("official"))
    external_ids = sorted(blacklist_ids("external"))
    person_ids = sorted(blacklist_ids("person"))
    erp_user_ids = sorted(blacklist_ids("erp_user"))

    if not any((user_ids, official_ids, external_ids, person_ids, erp_user_ids)):
        return

    criteria = (
        (User, User.id.not_in(user_ids)),
        (ExternalCraftsman, ExternalCraftsman.id.not_in(external_ids)),
        (WowiResponsibleOfficial, WowiResponsibleOfficial.erp_id.not_in(official_ids)),
        (
            UserJurisdictionPersonMapping,
            UserJurisdictionPersonMapping.user_id.not_in(user_ids)
            & UserJurisdictionPersonMapping.jurisdiction_person_erp_id.not_in(person_ids),
        ),
        (
            WowiEconomicUnitJurisdiction,
            or_(
                WowiEconomicUnitJurisdiction.responsible_official_erp_id.is_(None),
                WowiEconomicUnitJurisdiction.responsible_official_erp_id.not_in(official_ids),
            )
            & or_(
                WowiEconomicUnitJurisdiction.responsible_person_erp_id.is_(None),
                WowiEconomicUnitJurisdiction.responsible_person_erp_id.not_in(person_ids),
            )
            & or_(
                WowiEconomicUnitJurisdiction.responsible_user_erp_id.is_(None),
                WowiEconomicUnitJurisdiction.responsible_user_erp_id.not_in(erp_user_ids),
            ),
        ),
    )
    execute_state.statement = execute_state.statement.options(*(
        with_loader_criteria(
            model, condition, include_aliases=True, propagate_to_loaders=False,
        )
        for model, condition in criteria
    ))


def reject_blacklisted_token(header, payload):
    identity = str(payload.get("sub") or "")
    if not identity.isdigit():
        return True
    user = db.session.get(User, int(identity))
    return user is None or not user.is_active


def reject_blacklisted_token_response(header, payload):
    return jsonify({"message": "Der Benutzerzugang ist gesperrt."}), 401


def clear_blacklisted_session():
    user_id = str(session.get("_user_id") or "")
    if user_id.isdigit() and is_blacklisted("user", int(user_id)):
        logout_user()

    external_id = session.get("external_craftsman_id")
    if is_blacklisted("external", external_id):
        session.pop("external_craftsman_id", None)
        session.pop("external_craftsman_authenticated_at", None)
        session.pop("external_next", None)


def init_blacklist(app):
    if not event.contains(Session, "do_orm_execute", apply_blacklist_filter):
        event.listen(Session, "do_orm_execute", apply_blacklist_filter)
        for event_name in ("after_flush", "after_commit", "after_rollback"):
            event.listen(Session, event_name, clear_blacklist_cache)

    jwt.token_in_blocklist_loader(reject_blacklisted_token)
    jwt.revoked_token_loader(reject_blacklisted_token_response)
    app.before_request(clear_blacklisted_session)