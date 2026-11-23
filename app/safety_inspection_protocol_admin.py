from __future__ import annotations

from typing import Any

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    request,
    send_file,
    url_for,
)
from flask_login import (
    current_user,
    login_required,
)
from sqlalchemy import select

from .auth import can_review_safety_inspections
from .extensions import db
from .models import (
    SafetyInspectionJob,
    SafetyInspectionProtocol,
    TimeEntry,
)
from .safety_inspection_archive import (
    SafetyInspectionArchiveError,
    archive_safety_inspection_protocol,
    mark_safety_inspection_archive_failure,
)
from .safety_inspection_document import (
    SafetyInspectionDocumentError,
    create_safety_inspection_protocol,
    resolve_safety_inspection_protocol_path,
)


safety_inspection_protocol_admin_bp = (
    Blueprint(
        "safety_inspection_protocol_admin",
        __name__,
    )
)


def require_safety_inspection_access(
) -> None:
    if not can_review_safety_inspections(current_user):
        abort(403)


def job_redirect(job_id: int):
    return redirect(
        url_for(
            "safety_inspection_jobs_admin.job_detail",
            job_id=job_id,
        )
    )


@safety_inspection_protocol_admin_bp.get(
    "/admin/safety-inspections/jobs/"
    "<int:job_id>/protocols/"
    "<int:protocol_id>"
)
@login_required
def protocol_document(
    job_id: int,
    protocol_id: int,
) -> Any:
    if not can_review_safety_inspections(current_user):
        if not current_user.can_review_work or not TimeEntry.query.filter_by(
            safety_inspection_job_id=job_id,
            safety_inspection_protocol_id=protocol_id,
        ).first():
            abort(403)

    protocol = db.session.execute(
        select(
            SafetyInspectionProtocol
        ).where(
            SafetyInspectionProtocol.id
            == protocol_id,
            SafetyInspectionProtocol.job_id
            == job_id,
        )
    ).scalar_one_or_none()

    if protocol is None:
        abort(404)

    try:
        document_path = (
            resolve_safety_inspection_protocol_path(
                protocol.document_filename
            )
        )
    except SafetyInspectionDocumentError:
        abort(404)

    if not document_path.is_file():
        abort(404)

    return send_file(
        document_path,
        mimetype="application/pdf",
        as_attachment=False,
        download_name=(
            "Pruefprotokoll-"
            f"{protocol.job.public_id}-"
            f"v{protocol.version_number}.pdf"
        ),
    )


@safety_inspection_protocol_admin_bp.post(
    "/admin/safety-inspections/jobs/"
    "<int:job_id>/protocols"
)
@login_required
def protocol_create(
    job_id: int,
) -> Any:
    require_safety_inspection_access()

    job = db.session.execute(
        select(SafetyInspectionJob)
        .where(
            SafetyInspectionJob.id
            == job_id
        )
        .with_for_update()
    ).scalar_one_or_none()

    if job is None:
        abort(404)

    try:
        protocol = (
            create_safety_inspection_protocol(
                job,
                current_user,
                request.form.get(
                    "correction_reason"
                ),
            )
        )
        db.session.commit()

    except (
        SafetyInspectionDocumentError
    ) as exc:
        db.session.rollback()
        current_app.logger.exception(
            "safety_inspection_protocol_"
            "creation_failed job_id=%s",
            job_id,
        )
        flash(str(exc), "danger")

    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "safety_inspection_protocol_"
            "creation_failed job_id=%s",
            job_id,
        )
        flash(
            "Das Prüfprotokoll konnte "
            "nicht erstellt werden.",
            "danger",
        )

    else:
        flash(
            "Protokollversion "
            f"{protocol.version_number} "
            "wurde erstellt.",
            "success",
        )

    return job_redirect(job_id)


def store_archive_failure(
    protocol_id: int,
    error_message: str,
) -> None:
    protocol = db.session.get(
        SafetyInspectionProtocol,
        protocol_id,
    )

    if protocol is None:
        return

    mark_safety_inspection_archive_failure(
        protocol,
        error_message,
    )
    db.session.commit()


@safety_inspection_protocol_admin_bp.post(
    "/admin/safety-inspections/jobs/"
    "<int:job_id>/protocols/"
    "<int:protocol_id>/archive"
)
@login_required
def protocol_archive(job_id: int, protocol_id: int) -> Any:
    from .dms_client import DmsError
    from .dms_service import retry_latest_document

    require_safety_inspection_access()
    protocol = db.get_or_404(SafetyInspectionProtocol, protocol_id)
    if protocol.job_id != job_id:
        abort(404)
    try:
        document = retry_latest_document(
            "safety_protocol", protocol_id,
        )
        db.session.commit()
        flash(
            "Bereits archiviert."
            if document.status == "archived"
            else "Archivierungsauftrag vorgemerkt.",
            "success",
        )
    except DmsError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return job_redirect(job_id)
