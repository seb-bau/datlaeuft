from flask import abort
from sqlalchemy import or_

from .audit import audit
from .extensions import db
from .models import Commission, CommissionLocalStatus, CommissionSyncState, ErpOperationType, PushEventType, User, utc_now
from .settings import validate_debug_email_address


APPROVER_FIELDS = {
    "internal": "internal_commission_approver_user_id",
    "external": "external_commission_approver_user_id",
}


def approval_user_choices(owner):
    users = User.query.filter(
        User.is_active_flag.is_(True), or_(User.is_admin.is_(True), User.is_foreman.is_(True)),
        User.internal_commission_approver_user_id.is_(None), User.external_commission_approver_user_id.is_(None),
    ).order_by(User.display_name, User.id).all()
    return [user for user in users if user.id != owner.id and user.is_active]


def validate_approver(approver, creator_id):
    if approver is None or not approver.is_active or not approver.can_review_work or approver.id == creator_id:
        raise ValueError("Für die Freigabe muss ein anderer aktiver Sachbearbeiter ohne eigene Freigabepflicht ausgewählt werden.")
    try:
        if not validate_debug_email_address(approver.email):
            raise ValueError
    except ValueError as exc:
        raise ValueError("Die E-Mail-Adresse des Freigebers ist ungültig.") from exc


def save_user_approval_settings(user, form):
    if form.get("commission_approval_settings_present") != "1":
        return
    values = {}
    with db.session.no_autoflush:
        for field in APPROVER_FIELDS.values():
            raw_value = str(form.get(field) or "").strip()
            try:
                approver_id = int(raw_value) if raw_value else None
            except ValueError as exc:
                raise ValueError("Die Auswahl für die Auftragsfreigabe ist ungültig.") from exc
            if approver_id is not None:
                validate_approver(db.session.get(User, approver_id), user.id)
            values[field] = approver_id
        if user.id is not None and (any(values.values()) or not user.is_active_flag or not (user.is_admin or user.is_foreman)):
            assigned = User.query.filter(or_(
                User.internal_commission_approver_user_id == user.id,
                User.external_commission_approver_user_id == user.id,
            )).first()
            pending = Commission.query.filter_by(approval_state="pending", approval_approver_user_id=user.id).first()
            if assigned is not None or pending is not None:
                raise ValueError("Dieser Benutzer ist noch als Freigeber eingetragen. Bitte zuerst die zugeordneten Freigaben erledigen und die Benutzerzuordnungen ändern.")
    for field, value in values.items():
        setattr(user, field, value)


def prepare_commission_approval(commission, creator):
    db.session.refresh(creator, attribute_names=list(APPROVER_FIELDS.values()), with_for_update={"read": True})
    approver_id = getattr(creator, APPROVER_FIELDS[commission.assignment_type])
    if approver_id is None:
        return False
    approver = User.query.filter_by(id=approver_id).with_for_update(read=True).populate_existing().one_or_none()
    validate_approver(approver, creator.id)
    commission.approval_state = "pending"
    commission.approval_approver_user_id = approver_id
    commission.approval_requested_at = utc_now()
    commission.local_status = CommissionLocalStatus.WAITING_FOR_APPROVAL.value
    commission.completed_at = None
    commission.completion_successful = None
    from .workflow_notifications import enqueue_notification

    enqueue_notification(f"commission-approval:{commission.id}", "approval_request", commission=commission, payload={"approver_user_id": approver_id})
    audit("commission.approval.requested", "commission", commission.id, {"approver_user_id": approver_id}, actor_user_id=creator.id)
    return True


def can_approve_commission(commission, user):
    return bool(
        commission.approval_state == "pending" and commission.deleted_at is None
        and user.can_review_work and user.id != commission.created_by_user_id
        and (user.is_admin or user.id == commission.approval_approver_user_id)
    )


def approve_commission(commission_id, reviewer):
    reviewer = User.query.filter_by(id=reviewer.id).with_for_update(read=True).populate_existing().one()
    if not reviewer.is_active or not reviewer.can_review_work:
        abort(403)
    commission = Commission.query.filter_by(id=commission_id).with_for_update().populate_existing().first_or_404()
    if reviewer.id == commission.created_by_user_id or not (reviewer.is_admin or reviewer.id == commission.approval_approver_user_id):
        abort(403)
    if commission.approval_state == "approved":
        return commission, False
    if not can_approve_commission(commission, reviewer) or commission.erp_id is not None:
        raise ValueError("Dieser Auftrag wartet nicht auf eine Freigabe.")
    now = utc_now()
    commission.approval_state = "approved"
    commission.approved_by_user_id = reviewer.id
    commission.approved_at = now
    commission.status_changed_at = now
    commission.local_status = CommissionLocalStatus.RECORDED.value
    commission.erp_sync_state = CommissionSyncState.PENDING.value
    if commission.is_retrospective:
        commission.local_status = CommissionLocalStatus.DONE.value if commission.assignment_type == "internal" else CommissionLocalStatus.AWAITING_INVOICE.value
        commission.completed_at = now
        commission.completion_successful = True
    from .erp_sync import enqueue_erp_operation
    from .push_service import enqueue_commission_push

    enqueue_erp_operation(commission, ErpOperationType.CREATE_COMMISSION, f"commission:create:{commission.public_id}")
    enqueue_commission_push(commission, PushEventType.NEW_COMMISSION)
    audit("commission.approval.approved", "commission", commission.id, {"approver_user_id": commission.approval_approver_user_id}, actor_user_id=reviewer.id)
    return commission, True
