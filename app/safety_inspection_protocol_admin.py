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
)
from flask_login import (
    current_user,
    login_required,
)
from sqlalchemy import select

from .extensions import db
from .models import (
    SafetyInspectionJob,
    SafetyInspectionProtocol,
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
    if not (
        current_user.is_admin
        or (
            current_user
            .can_manage_safety_inspections
        )
        or (
            current_user
            .can_review_safety_inspections
        )
    ):
        abort(403)


def job_redirect(job_id: int):
    return redirect(
        "/admin/safety-inspection-jobs/"
        f"{job_id}"
    )


@safety_inspection_protocol_admin_bp.get(
    "/admin/safety-inspection-jobs/"
    "<int:job_id>/protocols/"
    "<int:protocol_id>"
)
@login_required
def protocol_document(
    job_id: int,
    protocol_id: int,
) -> Any:
    require_safety_inspection_access()

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
    "/admin/safety-inspection-jobs/"
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
    "/admin/safety-inspection-jobs/"
    "<int:job_id>/protocols/"
    "<int:protocol_id>/archive"
)
@login_required
def protocol_archive(
    job_id: int,
    protocol_id: int,
) -> Any:
    require_safety_inspection_access()

    protocol = db.session.execute(
        select(
            SafetyInspectionProtocol
        )
        .where(
            SafetyInspectionProtocol.id
            == protocol_id,
            SafetyInspectionProtocol.job_id
            == job_id,
        )
        .with_for_update()
    ).scalar_one_or_none()

    if protocol is None:
        abort(404)

    try:
        result = (
            archive_safety_inspection_protocol(
                protocol,
                current_user,
            )
        )
        db.session.commit()

    except (
        SafetyInspectionArchiveError
    ) as exc:
        db.session.rollback()
        store_archive_failure(
            protocol_id,
            str(exc),
        )
        current_app.logger.exception(
            "safety_inspection_"
            "archive_failed "
            "protocol_id=%s",
            protocol_id,
        )
        flash(str(exc), "danger")

    except Exception:
        db.session.rollback()

        error_message = (
            "Unerwarteter Fehler bei der "
            "d.velop-Archivierung."
        )
        store_archive_failure(
            protocol_id,
            error_message,
        )
        current_app.logger.exception(
            "safety_inspection_"
            "archive_failed "
            "protocol_id=%s",
            protocol_id,
        )
        flash(
            "Das Prüfprotokoll konnte "
            "nicht nach d.velop "
            "übertragen werden.",
            "danger",
        )

    else:
        if result.already_archived:
            flash(
                "Das Prüfprotokoll war "
                "bereits in d.velop "
                "archiviert. Die lokale "
                "Zuordnung wurde "
                "aktualisiert.",
                "success",
            )
        else:
            flash(
                "Das Prüfprotokoll wurde "
                "erfolgreich in d.velop "
                "archiviert.",
                "success",
            )

    return job_redirect(job_id)