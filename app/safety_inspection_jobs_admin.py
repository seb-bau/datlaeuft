from __future__ import annotations

from datetime import date
from typing import Any

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import case
from sqlalchemy.exc import IntegrityError

from .audit import audit
from .auth import (
    safety_inspection_manage_required,
    safety_inspection_review_required,
)
from .extensions import db
from .models import (
    CommissionLocalStatus,
    SafetyInspectionAssignmentSource,
    SafetyInspectionDefect,
    SafetyInspectionDefectStatus,
    SafetyInspectionJob,
    SafetyInspectionJobStatus,
    SafetyInspectionObjectLevel,
    SafetyInspectionResultState,
    User,
    utc_now,
)
from .safety_inspection_service import (
    create_commission_from_safety_defect,
)


safety_inspection_jobs_admin_bp = Blueprint(
    "safety_inspection_jobs_admin",
    __name__,
)


@safety_inspection_jobs_admin_bp.get(
    "/admin/safety-inspections/jobs"
)
@login_required
@safety_inspection_review_required
def jobs() -> Any:
    view_mode = request.args.get(
        "view",
        "active",
    ).strip()
    overdue_only = (
        request.args.get("overdue") == "1"
    )
    query = SafetyInspectionJob.query

    if view_mode == "completed":
        query = query.filter(
            SafetyInspectionJob.status.in_(
                {
                    SafetyInspectionJobStatus.COMPLETED.value,
                    SafetyInspectionJobStatus
                    .COMPLETED_WITH_DEFECTS.value,
                }
            )
        )
    elif view_mode == "missed":
        query = query.filter(
            SafetyInspectionJob.status
            == SafetyInspectionJobStatus.MISSED.value
        )
    elif view_mode == "canceled":
        query = query.filter(
            SafetyInspectionJob.status
            == SafetyInspectionJobStatus.CANCELED.value
        )
    elif view_mode == "all":
        pass
    else:
        view_mode = "active"
        query = query.filter(
            SafetyInspectionJob.status.in_(
                {
                    SafetyInspectionJobStatus.PLANNED.value,
                    SafetyInspectionJobStatus.IN_PROGRESS.value,
                }
            )
        )

    if overdue_only:
        query = query.filter(
            SafetyInspectionJob.status.in_(
                {
                    SafetyInspectionJobStatus.PLANNED.value,
                    SafetyInspectionJobStatus.IN_PROGRESS.value,
                }
            ),
            SafetyInspectionJob.due_to < date.today(),
        )

    items = query.order_by(
        SafetyInspectionJob.due_to.asc(),
        SafetyInspectionJob.due_from.asc(),
        SafetyInspectionJob.id.asc(),
    ).all()

    return render_template(
        "admin/safety_inspection_jobs.html",
        items=items,
        view_mode=view_mode,
        overdue_only=overdue_only,
        status_labels=_job_status_labels(),
        object_level_labels=_object_level_labels(),
    )


@safety_inspection_jobs_admin_bp.get(
    "/admin/safety-inspections/jobs/<int:job_id>"
)
@login_required
@safety_inspection_review_required
def job_detail(job_id: int) -> Any:
    item = db.session.get(
        SafetyInspectionJob,
        job_id,
    )

    if item is None:
        abort(404)

    craftsmen = (
        User.query.filter(
            User.is_active_flag.is_(True),
            User.craftsman_erp_id.isnot(None),
        )
        .order_by(User.display_name.asc())
        .all()
    )

    return render_template(
        "admin/safety_inspection_job_detail.html",
        item=item,
        craftsmen=craftsmen,
        status_labels=_job_status_labels(),
        result_state_labels=_result_state_labels(),
        defect_status_labels=_defect_status_labels(),
        object_level_labels=_object_level_labels(),
        assignment_source_labels=(
            _assignment_source_labels()
        ),
        commission_status_labels=(
            _commission_status_labels()
        ),
    )


@safety_inspection_jobs_admin_bp.post(
    "/admin/safety-inspections/jobs/"
    "<int:job_id>/cancel"
)
@login_required
@safety_inspection_review_required
def job_cancel(job_id: int) -> Any:
    item = db.session.get(
        SafetyInspectionJob,
        job_id,
    )

    if item is None:
        abort(404)

    reason = request.form.get(
        "reason",
        "",
    ).strip()

    if not reason:
        flash(
            "Für den Abbruch ist eine "
            "Begründung erforderlich.",
            "danger",
        )
    elif item.is_terminal:
        flash(
            "Dieser Prüfauftrag kann nicht "
            "mehr abgebrochen werden.",
            "danger",
        )
    else:
        item.status = (
            SafetyInspectionJobStatus.CANCELED.value
        )
        item.cancellation_reason = reason
        item.canceled_at = utc_now()
        item.canceled_by = current_user

        audit(
            "safety_inspection.job_canceled",
            "safety_inspection_job",
            item.id,
            {"reason": reason},
        )

        db.session.commit()

        flash(
            "Der Prüfauftrag wurde abgebrochen.",
            "success",
        )

    return redirect(
        url_for(
            "safety_inspection_jobs_admin.job_detail",
            job_id=job_id,
        )
    )


@safety_inspection_jobs_admin_bp.post(
    "/admin/safety-inspections/jobs/"
    "<int:job_id>/assign"
)
@login_required
@safety_inspection_manage_required
def job_assign(job_id: int) -> Any:
    item = db.session.get(
        SafetyInspectionJob,
        job_id,
    )

    if item is None:
        abort(404)

    raw_user_id = request.form.get(
        "user_id",
        "",
    ).strip()

    if item.is_terminal:
        flash(
            "Die Zuständigkeit eines abgeschlossenen "
            "Prüfauftrags kann nicht geändert werden.",
            "danger",
        )
    elif not raw_user_id.isdigit():
        flash(
            "Bitte einen Handwerker auswählen.",
            "danger",
        )
    else:
        user = db.session.get(
            User,
            int(raw_user_id),
        )

        if (
            user is None
            or not user.is_active
            or user.craftsman_erp_id is None
        ):
            flash(
                "Der ausgewählte Handwerker "
                "ist nicht verfügbar.",
                "danger",
            )
        else:
            item.assigned_user = user
            item.assignment_source = (
                SafetyInspectionAssignmentSource.MANUAL.value
            )
            item.assignment_error = None

            audit(
                "safety_inspection.job_assigned",
                "safety_inspection_job",
                item.id,
                {"user_id": user.id},
            )

            db.session.commit()

            flash(
                "Der Prüfauftrag wurde "
                f"{user.display_name} zugewiesen.",
                "success",
            )

    return redirect(
        url_for(
            "safety_inspection_jobs_admin.job_detail",
            job_id=job_id,
        )
    )


@safety_inspection_jobs_admin_bp.get(
    "/admin/safety-inspections/defects"
)
@login_required
@safety_inspection_review_required
def defects() -> Any:
    view_mode = request.args.get(
        "view",
        "active",
    ).strip()
    query = SafetyInspectionDefect.query

    if view_mode == "resolved":
        query = query.filter(
            SafetyInspectionDefect.status
            == SafetyInspectionDefectStatus.RESOLVED.value
        )
    elif view_mode == "dismissed":
        query = query.filter(
            SafetyInspectionDefect.status
            == SafetyInspectionDefectStatus.DISMISSED.value
        )
    elif view_mode == "all":
        pass
    else:
        view_mode = "active"
        query = query.filter(
            SafetyInspectionDefect.status.in_(
                {
                    SafetyInspectionDefectStatus.OPEN.value,
                    SafetyInspectionDefectStatus
                    .COMMISSION_CREATED.value,
                    SafetyInspectionDefectStatus.IN_PROGRESS.value,
                    SafetyInspectionDefectStatus
                    .REVIEW_REQUIRED.value,
                }
            )
        )

    items = query.order_by(
        case(
            (
                SafetyInspectionDefect.severity
                == SafetyInspectionResultState
                .DEFECT_URGENT.value,
                0,
            ),
            else_=1,
        ).asc(),
        SafetyInspectionDefect.created_at.desc(),
        SafetyInspectionDefect.id.desc(),
    ).all()

    return render_template(
        "admin/safety_inspection_defects.html",
        items=items,
        view_mode=view_mode,
        status_labels=_defect_status_labels(),
        severity_labels=_result_state_labels(),
        commission_status_labels=(
            _commission_status_labels()
        ),
    )


@safety_inspection_jobs_admin_bp.post(
    "/admin/safety-inspections/defects/"
    "<int:defect_id>/commission"
)
@login_required
@safety_inspection_review_required
def defect_commission(defect_id: int) -> Any:
    defect = db.session.get(
        SafetyInspectionDefect,
        defect_id,
    )

    if defect is None:
        abort(404)

    if defect.commission_links:
        flash(
            "Für diesen Mangel wurde bereits ein "
            "Instandhaltungsauftrag erstellt.",
            "warning",
        )

        return redirect(
            url_for(
                "safety_inspection_jobs_admin.job_detail",
                job_id=defect.job_id,
            )
        )

    try:
        commission = create_commission_from_safety_defect(
            defect,
            current_user,
        )

        audit(
            "safety_inspection.defect_commission_created",
            "safety_inspection_defect",
            defect.id,
            {"commission_id": commission.id},
        )

        db.session.commit()
    except (ValueError, IntegrityError) as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    except Exception:
        db.session.rollback()

        current_app.logger.exception(
            "safety_inspection_defect_commission_failed "
            "defect_id=%s",
            defect.id,
        )

        flash(
            "Der Instandhaltungsauftrag konnte "
            "nicht erstellt werden.",
            "danger",
        )
    else:
        flash(
            "Der Instandhaltungsauftrag wurde erstellt "
            "und zur Übertragung nach Wowiport vorgemerkt.",
            "success",
        )

    return redirect(
        url_for(
            "safety_inspection_jobs_admin.job_detail",
            job_id=defect.job_id,
        )
    )


@safety_inspection_jobs_admin_bp.post(
    "/admin/safety-inspections/defects/"
    "<int:defect_id>/dismiss"
)
@login_required
@safety_inspection_review_required
def defect_dismiss(defect_id: int) -> Any:
    defect = db.session.get(
        SafetyInspectionDefect,
        defect_id,
    )

    if defect is None:
        abort(404)

    reason = request.form.get(
        "reason",
        "",
    ).strip()

    if not reason:
        flash(
            "Für das Verwerfen ist eine "
            "Begründung erforderlich.",
            "danger",
        )
    elif (
        defect.status
        == SafetyInspectionDefectStatus.RESOLVED.value
    ):
        flash(
            "Ein bereits behobener Mangel kann "
            "nicht verworfen werden.",
            "danger",
        )
    else:
        defect.status = (
            SafetyInspectionDefectStatus.DISMISSED.value
        )
        defect.review_note = reason
        defect.reviewer = current_user
        defect.dismissed_by = current_user
        defect.dismissed_at = utc_now()

        audit(
            "safety_inspection.defect_dismissed",
            "safety_inspection_defect",
            defect.id,
            {"reason": reason},
        )

        db.session.commit()

        flash(
            "Der Mangel wurde verworfen.",
            "success",
        )

    return redirect(
        url_for(
            "safety_inspection_jobs_admin.job_detail",
            job_id=defect.job_id,
        )
    )


@safety_inspection_jobs_admin_bp.post(
    "/admin/safety-inspections/defects/"
    "<int:defect_id>/reopen"
)
@login_required
@safety_inspection_review_required
def defect_reopen(defect_id: int) -> Any:
    defect = db.session.get(
        SafetyInspectionDefect,
        defect_id,
    )

    if defect is None:
        abort(404)

    if (
        defect.status
        != SafetyInspectionDefectStatus.DISMISSED.value
    ):
        flash(
            "Nur ein verworfener Mangel kann "
            "wieder geöffnet werden.",
            "danger",
        )
    else:
        defect.status = (
            SafetyInspectionDefectStatus.OPEN.value
        )
        defect.review_note = None
        defect.reviewer = current_user
        defect.dismissed_by = None
        defect.dismissed_at = None

        audit(
            "safety_inspection.defect_reopened",
            "safety_inspection_defect",
            defect.id,
        )

        db.session.commit()

        flash(
            "Der Mangel wurde wieder geöffnet.",
            "success",
        )

    return redirect(
        url_for(
            "safety_inspection_jobs_admin.job_detail",
            job_id=defect.job_id,
        )
    )


def _job_status_labels() -> dict[str, str]:
    return {
        SafetyInspectionJobStatus.PLANNED.value: "Geplant",
        SafetyInspectionJobStatus.IN_PROGRESS.value: (
            "In Bearbeitung"
        ),
        SafetyInspectionJobStatus.COMPLETED.value: (
            "Abgeschlossen"
        ),
        SafetyInspectionJobStatus
        .COMPLETED_WITH_DEFECTS.value: (
            "Abgeschlossen mit Mängeln"
        ),
        SafetyInspectionJobStatus.MISSED.value: "Abgelaufen",
        SafetyInspectionJobStatus.CANCELED.value: (
            "Abgebrochen"
        ),
    }


def _result_state_labels() -> dict[str, str]:
    return {
        SafetyInspectionResultState.NOT_CHECKED.value: (
            "Nicht geprüft"
        ),
        SafetyInspectionResultState.NO_DEFECT.value: (
            "Ohne Mangel"
        ),
        SafetyInspectionResultState.NOT_PRESENT.value: (
            "Nicht vorhanden"
        ),
        SafetyInspectionResultState.DEFECT_INFO.value: (
            "Mangel (Info)"
        ),
        SafetyInspectionResultState.DEFECT_URGENT.value: (
            "Mangel (dringend)"
        ),
    }


def _defect_status_labels() -> dict[str, str]:
    return {
        SafetyInspectionDefectStatus.OPEN.value: "Offen",
        SafetyInspectionDefectStatus
        .COMMISSION_CREATED.value: "Auftrag erstellt",
        SafetyInspectionDefectStatus.IN_PROGRESS.value: (
            "In Bearbeitung"
        ),
        SafetyInspectionDefectStatus.RESOLVED.value: (
            "Behoben"
        ),
        SafetyInspectionDefectStatus
        .REVIEW_REQUIRED.value: "Prüfung erforderlich",
        SafetyInspectionDefectStatus.DISMISSED.value: (
            "Verworfen"
        ),
    }


def _object_level_labels() -> dict[str, str]:
    return {
        SafetyInspectionObjectLevel
        .ECONOMIC_UNIT.value: "Wirtschaftseinheit",
        SafetyInspectionObjectLevel.BUILDING.value: (
            "Gebäude"
        ),
        SafetyInspectionObjectLevel.USE_UNIT.value: (
            "Nutzungseinheit"
        ),
    }


def _assignment_source_labels() -> dict[str, str]:
    return {
        SafetyInspectionAssignmentSource
        .JURISDICTION.value: "Wowiport-Zuständigkeit",
        SafetyInspectionAssignmentSource.FALLBACK.value: (
            "Ersatzzuständigkeit"
        ),
        SafetyInspectionAssignmentSource
        .UNASSIGNED.value: "Nicht zugeordnet",
        SafetyInspectionAssignmentSource.MANUAL.value: (
            "Manuell zugeordnet"
        ),
    }


def _commission_status_labels() -> dict[str, str]:
    return {
        CommissionLocalStatus.RECORDED.value: "Erfasst",
        CommissionLocalStatus
        .WAITING_FOR_ACCEPTANCE.value: (
            "Warte auf Annahme"
        ),
        CommissionLocalStatus.OPEN.value: "Beauftragt",
        CommissionLocalStatus.IN_PROGRESS.value: (
            "In Bearbeitung"
        ),
        CommissionLocalStatus
        .COMPLETION_REVIEW.value: "Abschlussprüfung",
        CommissionLocalStatus
        .TENANT_CHARGE_REVIEW.value: "Mieterbelastung",
        CommissionLocalStatus.DONE.value: "Erledigt",
        CommissionLocalStatus.DELETED.value: "Gelöscht",
    }