from sqlalchemy import and_, case, or_
from sqlalchemy.orm import selectinload
from flask import url_for

from .models import Commission, CommissionLocalStatus, ExternalCraftsman
from .utils import format_local_datetime


OBJECT_REFERENCES = {
    "license_agreement": ("license_agreement", Commission.license_agreement_id, Commission.license_agreement_idnum),
    "use_unit": ("use_unit", Commission.use_unit_id, Commission.use_unit_idnum),
    "building": ("building", Commission.building_erp_id, Commission.building_idnum),
    "economic_unit": ("economic_unit", Commission.economic_unit_erp_id, Commission.economic_unit_idnum),
}
STATUS_LABELS = {
    "waiting_for_approval": "Wartet auf Freigabe",
    "recorded": "Erfasst", "waiting_for_acceptance": "Warte auf Annahme", "open": "Beauftragt",
    "in_progress": "In Bearbeitung", "completion_review": "Nicht abgeschlossen",
    "tenant_charge_review": "Mieterbelastung", "done": "Erledigt",
    "awaiting_invoice": "Rechnungseingang erwartet",
}


def open_commission_filter():
    return or_(
        Commission.local_status == CommissionLocalStatus.COMPLETION_REVIEW.value,
        and_(
            Commission.completed_at.is_(None),
            Commission.local_status.notin_((
                CommissionLocalStatus.DONE.value, CommissionLocalStatus.AWAITING_INVOICE.value,
                CommissionLocalStatus.TENANT_CHARGE_REVIEW.value, CommissionLocalStatus.DELETED.value,
            )),
            or_(Commission.status_id.is_(None), Commission.status_id.notin_((9, 10, 11))),
        ),
    )


def commission_object_history_query(choice):
    object_type = choice["object_type"]
    reference_key, id_column, number_column = OBJECT_REFERENCES[object_type]
    reference = choice[reference_key]
    company = choice["company_code"]
    identity = id_column == reference["erp_id"]
    if reference.get("id_num") and (company.get("erp_id") is not None or company.get("code")):
        company_match = Commission.company_code_erp_id == company["erp_id"] if company.get("erp_id") is not None else False
        if company.get("code"):
            company_match = or_(company_match, and_(Commission.company_code_erp_id.is_(None), Commission.company_code_code == company["code"]))
        identity = or_(identity, and_(
            or_(id_column.is_(None), id_column == 0), number_column == reference["id_num"], company_match,
        ))
    explicit_scope = Commission.raw_payload["commission_scope"]["object_type"].as_string()
    inherited_references = []
    for scope, (_, child_id, child_number) in OBJECT_REFERENCES.items():
        if scope == object_type:
            break
        inherited_references.extend((
            or_(child_id.is_(None), child_id == 0), or_(child_number.is_(None), child_number == ""),
        ))
    return Commission.query.filter(
        identity,
        or_(explicit_scope == object_type, and_(
            or_(explicit_scope.is_(None), explicit_scope.notin_(tuple(OBJECT_REFERENCES))),
            *inherited_references,
        )),
        Commission.deleted_at.is_(None), Commission.local_status != CommissionLocalStatus.DELETED.value,
    )


def commission_object_history(choice, *, summary=False, view="all", page=1):
    query = commission_object_history_query(choice)
    open_filter = open_commission_filter()
    total = query.count()
    open_count = query.filter(open_filter).count()
    result = {"total": total, "open_count": open_count}
    if summary:
        return result
    if view == "open":
        query = query.filter(open_filter)
    per_page = 20
    pages = max(1, ((open_count if view == "open" else total) + per_page - 1) // per_page)
    page = min(page, pages)
    items = query.options(
        selectinload(Commission.positions), selectinload(Commission.user), selectinload(Commission.created_by),
    ).order_by(case((open_filter, 0), else_=1), Commission.recording_date.desc(), Commission.id.desc()).offset((page - 1) * per_page).limit(per_page).all()
    external_ids = {item.craftsman_erp_id for item in items if item.assignment_type == "external" and item.craftsman_erp_id is not None}
    external_names = {
        item.craftsman_erp_id: item.display_name
        for item in ExternalCraftsman.query.filter(ExternalCraftsman.craftsman_erp_id.in_(external_ids)).all()
    } if external_ids else {}
    open_ids = {row[0] for row in query.filter(open_filter, Commission.id.in_([item.id for item in items])).with_entities(Commission.id).all()}
    result.update({"page": page, "pages": pages, "view": view, "items": [{
        "id": item.id, "number": item.erp_idnum, "title": item.display_title,
        "description": "\n\n".join(position.commission_text or position.display_text for position in item.positions) or item.description or "",
        "is_open": item.id in open_ids,
        "status": STATUS_LABELS.get(item.local_status, "Unbekannt"),
        "created_at": format_local_datetime(item.recording_date),
        "created_by": item.created_by.display_name if item.created_by else "Wowiport",
        "craftsman": external_names.get(item.craftsman_erp_id, "") if item.assignment_type == "external" else (item.user.display_name if item.user else ""),
        "url": url_for("admin.commission_detail", commission_id=item.id),
    } for item in items]})
    return result
