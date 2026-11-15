from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import selectinload

from .commission_object_history import open_commission_filter
from .models import Commission, CommissionLocalStatus


RELATED_REFERENCES = (
    ("license_agreement", "license_agreement_id", "license_agreement_idnum"),
    ("use_unit", "use_unit_id", "use_unit_idnum"),
    ("building", "building_erp_id", "building_idnum"),
)


def related_identity(commission, id_attribute, number_attribute):
    reference_id = getattr(commission, id_attribute)
    reference_number = getattr(commission, number_attribute)
    id_column = getattr(Commission, id_attribute)
    number_column = getattr(Commission, number_attribute)
    identity = id_column == reference_id if reference_id else False
    if reference_number and (commission.company_code_erp_id or commission.company_code_code):
        company = Commission.company_code_erp_id == commission.company_code_erp_id if commission.company_code_erp_id else False
        if commission.company_code_code:
            company = or_(company, and_(Commission.company_code_erp_id.is_(None), Commission.company_code_code == commission.company_code_code))
        number_match = and_(number_column == reference_number, company)
        if reference_id:
            number_match = and_(or_(id_column.is_(None), id_column == 0), number_match)
        identity = or_(identity, number_match)
    return identity


def related_commission_query(commission):
    relationships = [(name, related_identity(commission, id_attribute, number_attribute))
                     for name, id_attribute, number_attribute in RELATED_REFERENCES]
    relationship = case(*[(condition, name) for name, condition in relationships], else_="building")
    query = Commission.query.filter(
        Commission.id != commission.id,
        Commission.deleted_at.is_(None), Commission.local_status != CommissionLocalStatus.DELETED.value,
        Commission.approval_state != "pending",
        Commission.erp_id.is_not(None), Commission.erp_idnum.is_not(None), Commission.erp_idnum != "",
        or_(*(condition for _, condition in relationships)),
    )
    return query, relationship


def related_commissions(commission, page=1):
    query, relationship = related_commission_query(commission)
    total = query.count()
    pages = max(1, (total + 19) // 20)
    page = min(page, pages)
    items = query.options(selectinload(Commission.positions), selectinload(Commission.user)).add_columns(relationship).order_by(
        case((open_commission_filter(), 0), else_=1), case((relationship == "license_agreement", 0), (relationship == "use_unit", 1), else_=2), Commission.recording_date.desc(), Commission.id.desc(),
    ).offset((page - 1) * 20).limit(20).all()
    counts = {name: count for name, count in query.with_entities(relationship, func.count(Commission.id)).group_by(relationship).all()}
    return {"total": total, "page": page, "pages": pages, "counts": counts}, items
