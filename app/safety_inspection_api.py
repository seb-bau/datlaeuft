from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from time import perf_counter

from sqlalchemy import case, func
from sqlalchemy.orm import raiseload, selectinload

from .models import SafetyInspectionTemplateVersion

from flask import (
    Blueprint,
    abort,
    current_app,
    jsonify,
    request,
    send_file,
)
from flask_jwt_extended import jwt_required

from .api import (
    current_api_user,
    serialize_user,
    viewable_commission_user_ids,
)
from .audit import audit
from .extensions import db
from .models import (
    SafetyInspectionDefect,
    SafetyInspectionJob,
    SafetyInspectionJobStatus,
    SafetyInspectionPhoto,
    SafetyInspectionPhotoSource,
    SafetyInspectionResult,
    SafetyInspectionResultState,
    User,
    utc_now,
    AuditLog,
)
from .photo_service import (
    MAX_PHOTO_BYTES,
    commission_photo_path,
    store_commission_photo_bytes,
)
from .safety_inspection_service import (
    complete_safety_inspection_job,
    ensure_job_editable,
    update_safety_inspection_result,
)
from .utils import iso_datetime
from .safety_inspection_document import (
    SafetyInspectionDocumentError,
)
import hashlib
import json

from uuid import UUID

from sqlalchemy.exc import IntegrityError
from werkzeug.exceptions import HTTPException

from .models import SafetyInspectionMobileOperation


safety_inspection_api_bp = Blueprint(
    "safety_inspection_api",
    __name__,
    url_prefix="/api",
)


@safety_inspection_api_bp.get("/safety-inspections")
@jwt_required()
def safety_inspections() -> Any:
    user = current_api_user()
    scope = request.args.get("scope", "mine").strip()
    status = request.args.get("status", "active").strip()
    user_id_filter = request.args.get("user_id", "").strip()
    overdue_only = request.args.get("overdue") == "1"

    if scope not in {"mine", "all"}:
        return jsonify({
            "message": "Der Benutzerfilter ist ungültig."
        }), 400

    allowed_user_ids = viewable_safety_inspection_user_ids(user)
    query = SafetyInspectionJob.query

    if scope == "mine":
        query = query.filter(
            SafetyInspectionJob.assigned_user_id == user.id
        )
    elif user_id_filter:
        try:
            selected_user_id = int(user_id_filter)
        except ValueError:
            return jsonify({
                "message": "Der ausgewählte Prüfer ist ungültig."
            }), 400

        if not 0 < selected_user_id <= 2**63 - 1:
            return jsonify({
                "message": "Der ausgewählte Prüfer ist ungültig."
            }), 400

        if (
            allowed_user_ids is not None
            and selected_user_id not in allowed_user_ids
        ):
            return jsonify({
                "message": "Für diesen Prüfer besteht keine Berechtigung."
            }), 403

        query = query.filter(
            SafetyInspectionJob.assigned_user_id == selected_user_id
        )
    elif allowed_user_ids is not None:
        query = query.filter(
            SafetyInspectionJob.assigned_user_id.in_(allowed_user_ids)
        )

    if status == "active":
        query = query.filter(
            SafetyInspectionJob.status.in_({
                SafetyInspectionJobStatus.PLANNED.value,
                SafetyInspectionJobStatus.IN_PROGRESS.value,
            })
        )
    elif status == "completed":
        query = query.filter(
            SafetyInspectionJob.status.in_({
                SafetyInspectionJobStatus.COMPLETED.value,
                SafetyInspectionJobStatus.COMPLETED_WITH_DEFECTS.value,
            })
        )
    elif status in {item.value for item in SafetyInspectionJobStatus}:
        query = query.filter(SafetyInspectionJob.status == status)
    elif status != "all":
        return jsonify({
            "message": "Der Statusfilter ist ungültig."
        }), 400

    if overdue_only:
        query = query.filter(
            SafetyInspectionJob.status.in_({
                SafetyInspectionJobStatus.PLANNED.value,
                SafetyInspectionJobStatus.IN_PROGRESS.value,
            }),
            SafetyInspectionJob.due_to < date.today(),
        )

    query_started_at = perf_counter()

    items = query.options(
        selectinload(SafetyInspectionJob.group),
        selectinload(SafetyInspectionJob.template_version).selectinload(
            SafetyInspectionTemplateVersion.template
        ),
        selectinload(SafetyInspectionJob.department),
        selectinload(SafetyInspectionJob.assigned_user),
        selectinload(SafetyInspectionJob.completed_by),
        raiseload("*"),
    ).order_by(
        SafetyInspectionJob.due_to.asc(),
        SafetyInspectionJob.due_from.asc(),
        SafetyInspectionJob.id.asc(),
    ).all()

    progress_by_job = _safety_inspection_progress_by_job(
        [item.id for item in items]
    )
    query_duration_ms = (perf_counter() - query_started_at) * 1000

    serialization_started_at = perf_counter()
    response = jsonify({
        "items": [
            serialize_safety_inspection_job(
                item,
                user,
                progress=progress_by_job[item.id],
            )
            for item in items
        ],
    })
    serialization_duration_ms = (
                                        perf_counter() - serialization_started_at
                                ) * 1000

    response.headers["Server-Timing"] = (
        f"database;dur={query_duration_ms:.1f}, "
        f"serialization;dur={serialization_duration_ms:.1f}"
    )
    current_app.logger.info(
        "safety_inspections_list count=%s database_ms=%.1f serialization_ms=%.1f",
        len(items),
        query_duration_ms,
        serialization_duration_ms,
    )
    return response


@safety_inspection_api_bp.get("/safety-inspections/users")
@jwt_required()
def safety_inspection_users() -> Any:
    user = current_api_user()
    allowed_user_ids = viewable_safety_inspection_user_ids(user)
    query = User.query.filter(User.id != user.id)

    if allowed_user_ids is None:
        assigned_user_ids = db.session.query(
            SafetyInspectionJob.assigned_user_id
        ).filter(
            SafetyInspectionJob.assigned_user_id.isnot(None)
        )
        query = query.filter(User.id.in_(assigned_user_ids))
    else:
        query = query.filter(User.id.in_(allowed_user_ids))

    items = [user]
    items.extend(
        query.order_by(User.display_name.asc(), User.id.asc()).all()
    )

    return jsonify({
        "items": [serialize_user(item) for item in items],
    })


@safety_inspection_api_bp.get(
    "/safety-inspections/<int:job_id>"
)
@jwt_required()
def safety_inspection_detail(
    job_id: int,
) -> Any:
    user = current_api_user()
    job = viewable_safety_inspection_job(
        job_id,
        user,
    )

    return jsonify(
        {
            "item": serialize_safety_inspection_job(
                job,
                user,
                include_details=True,
            )
        }
    )


@safety_inspection_api_bp.patch(
    "/safety-inspections/<int:job_id>/"
    "results/<int:result_id>"
)
@jwt_required()
def update_safety_inspection_result_api(
    job_id: int,
    result_id: int,
) -> Any:
    user = current_api_user()
    job, result = viewable_safety_result(
        job_id,
        result_id,
        user,
    )
    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify(
            {
                "message": (
                    "Ungültiger JSON-Body."
                )
            }
        ), 400

    expected_version = None

    if data.get("version") is not None:
        try:
            expected_version = int(
                data["version"]
            )
        except (TypeError, ValueError):
            return jsonify(
                {
                    "message": (
                        "Die Versionsnummer "
                        "ist ungültig."
                    )
                }
            ), 400

    try:
        update_safety_inspection_result(
            result=result,
            state=str(
                data.get("state") or ""
            ).strip(),
            note=data.get("note"),
            immediate_action=data.get(
                "immediate_action"
            ),
            user=user,
            expected_version=expected_version,
        )

        audit(
            "safety_inspection.result_updated",
            "safety_inspection_result",
            result.id,
            {
                "job_id": job.id,
                "state": result.state,
                "version": result.version,
            },
        )

        db.session.commit()
    except ValueError as exc:
        db.session.rollback()

        return jsonify(
            {"message": str(exc)}
        ), 409

    return jsonify(
        {
            "item": (
                serialize_safety_inspection_result(
                    result
                )
            ),
            "job": (
                serialize_safety_inspection_job(
                    job,
                    user,
                )
            ),
        }
    )


@safety_inspection_api_bp.post(
    "/safety-inspections/<int:job_id>/complete"
)
@jwt_required()
def complete_safety_inspection(
    job_id: int,
) -> Any:
    user = current_api_user()
    job = viewable_safety_inspection_job(
        job_id,
        user,
    )
    data = request.get_json(silent=True) or {}

    if not isinstance(data, dict):
        return jsonify(
            {
                "message": (
                    "Ungültiger JSON-Body."
                )
            }
        ), 400

    try:
        defects = complete_safety_inspection_job(
            job=job,
            user=user,
            overall_note=data.get(
                "overall_note"
            ),
        )

        audit(
            "safety_inspection.job_completed",
            "safety_inspection_job",
            job.id,
            {
                "status": job.status,
                "defect_count": len(defects),
            },
        )

        db.session.commit()
    except ValueError as exc:
        db.session.rollback()

        return jsonify(
            {"message": str(exc)}
        ), 409
    except SafetyInspectionDocumentError as exc:
        db.session.rollback()

        current_app.logger.exception(
            "safety_inspection_protocol_"
            "creation_failed job_id=%s",
            job.id,
        )

        return jsonify(
            {"message": str(exc)}
        ), 500

    return jsonify(
        {
            "item": serialize_safety_inspection_job(
                job,
                user,
                include_details=True,
            )
        }
    )


@safety_inspection_api_bp.post(
    "/safety-inspections/<int:job_id>/"
    "results/<int:result_id>/photos"
)
@jwt_required()
def upload_safety_inspection_photo(
    job_id: int,
    result_id: int,
) -> Any:
    user = current_api_user()
    job, result = viewable_safety_result(
        job_id,
        result_id,
        user,
    )

    try:
        ensure_job_editable(job, user)
    except ValueError as exc:
        return jsonify(
            {"message": str(exc)}
        ), 409

    upload = request.files.get("photo")

    if upload is None or not upload.filename:
        return jsonify(
            {
                "message": (
                    "Es wurde kein Foto übermittelt."
                )
            }
        ), 400

    source = request.form.get(
        "source",
        SafetyInspectionPhotoSource.CAMERA.value,
    ).strip()

    if source not in {
        item.value
        for item in SafetyInspectionPhotoSource
    }:
        return jsonify(
            {
                "message": (
                    "Die Fotoquelle ist ungültig."
                )
            }
        ), 400

    caption = clean_optional_text(
        request.form.get("caption"),
        maximum_length=500,
    )

    try:
        captured_at = parse_optional_datetime(
            request.form.get("captured_at")
        )
    except ValueError as exc:
        return jsonify(
            {"message": str(exc)}
        ), 400

    raw_data = upload.stream.read(
        MAX_PHOTO_BYTES + 1
    )

    try:
        stored_photo = (
            store_commission_photo_bytes(
                commission=None,
                raw_data=raw_data,
                original_filename=(
                    upload.filename
                ),
                storage_directory=(
                    Path("safety_inspections")
                    / job.public_id
                    / result.public_id
                ),
            )
        )
    except ValueError as exc:
        return jsonify(
            {"message": str(exc)}
        ), 400

    existing_photo = (
        SafetyInspectionPhoto.query.filter(
            SafetyInspectionPhoto.result_id
            == result.id,
            SafetyInspectionPhoto.content_sha256
            == stored_photo.content_sha256,
            SafetyInspectionPhoto.active.is_(True),
        ).one_or_none()
    )

    if existing_photo is not None:
        remove_stored_photo(
            stored_photo.storage_name,
            stored_photo.thumbnail_storage_name,
        )

        return jsonify(
            {
                "item": (
                    serialize_safety_inspection_photo(
                        existing_photo
                    )
                ),
                "duplicate": True,
            }
        )

    photo = SafetyInspectionPhoto(
        result=result,
        uploaded_by=user,
        source=source,
        original_filename=(
            stored_photo.original_filename
        ),
        storage_name=stored_photo.storage_name,
        thumbnail_storage_name=(
            stored_photo.thumbnail_storage_name
        ),
        mime_type=stored_photo.mime_type,
        size_bytes=stored_photo.size_bytes,
        content_sha256=(
            stored_photo.content_sha256
        ),
        caption=caption,
        captured_at=captured_at,
        active=True,
    )

    try:
        db.session.add(photo)
        db.session.flush()

        audit(
            "safety_inspection.photo_uploaded",
            "safety_inspection_result",
            result.id,
            {
                "job_id": job.id,
                "photo_id": photo.id,
                "source": photo.source,
            },
        )

        db.session.commit()
    except Exception:
        db.session.rollback()

        remove_stored_photo(
            stored_photo.storage_name,
            stored_photo.thumbnail_storage_name,
        )

        current_app.logger.exception(
            "safety_inspection_photo_upload_failed "
            "job_id=%s result_id=%s",
            job.id,
            result.id,
        )

        return jsonify(
            {
                "message": (
                    "Das Foto konnte nicht "
                    "gespeichert werden."
                )
            }
        ), 500

    return jsonify(
        {
            "item": (
                serialize_safety_inspection_photo(
                    photo
                )
            ),
            "duplicate": False,
        }
    ), 201


@safety_inspection_api_bp.patch(
    "/safety-inspections/<int:job_id>/"
    "results/<int:result_id>/photos/"
    "<int:photo_id>"
)
@jwt_required()
def update_safety_inspection_photo(
    job_id: int,
    result_id: int,
    photo_id: int,
) -> Any:
    user = current_api_user()
    job, result = viewable_safety_result(
        job_id,
        result_id,
        user,
    )

    try:
        ensure_job_editable(job, user)
    except ValueError as exc:
        return jsonify(
            {"message": str(exc)}
        ), 409

    photo = active_safety_inspection_photo(
        result,
        photo_id,
    )
    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify(
            {
                "message": (
                    "Ungültiger JSON-Body."
                )
            }
        ), 400

    photo.caption = clean_optional_text(
        data.get("caption"),
        maximum_length=500,
    )

    if "captured_at" in data:
        try:
            photo.captured_at = (
                parse_optional_datetime(
                    data.get("captured_at")
                )
            )
        except ValueError as exc:
            return jsonify(
                {"message": str(exc)}
            ), 400

    audit(
        "safety_inspection.photo_updated",
        "safety_inspection_result",
        result.id,
        {
            "job_id": job.id,
            "photo_id": photo.id,
        },
    )

    db.session.commit()

    return jsonify(
        {
            "item": (
                serialize_safety_inspection_photo(
                    photo
                )
            )
        }
    )


@safety_inspection_api_bp.delete(
    "/safety-inspections/<int:job_id>/"
    "results/<int:result_id>/photos/"
    "<int:photo_id>"
)
@jwt_required()
def delete_safety_inspection_photo(
    job_id: int,
    result_id: int,
    photo_id: int,
) -> Any:
    user = current_api_user()
    job, result = viewable_safety_result(
        job_id,
        result_id,
        user,
    )

    try:
        ensure_job_editable(job, user)
    except ValueError as exc:
        return jsonify(
            {"message": str(exc)}
        ), 409

    photo = active_safety_inspection_photo(
        result,
        photo_id,
    )
    photo.active = False
    photo.deleted_at = utc_now()

    audit(
        "safety_inspection.photo_deleted",
        "safety_inspection_result",
        result.id,
        {
            "job_id": job.id,
            "photo_id": photo.id,
        },
    )

    db.session.commit()

    return jsonify({"deleted": True})


@safety_inspection_api_bp.get(
    "/safety-inspections/<int:job_id>/"
    "results/<int:result_id>/photos/"
    "<int:photo_id>/content"
)
@jwt_required()
def safety_inspection_photo_content(
    job_id: int,
    result_id: int,
    photo_id: int,
) -> Any:
    user = current_api_user()
    _, result = viewable_safety_result(
        job_id,
        result_id,
        user,
    )
    photo = active_safety_inspection_photo(
        result,
        photo_id,
    )
    path = commission_photo_path(
        photo.storage_name
    )

    if not path.is_file():
        abort(404)

    return send_file(
        path,
        mimetype=photo.mime_type,
        conditional=True,
        max_age=86_400,
    )


@safety_inspection_api_bp.get(
    "/safety-inspections/<int:job_id>/"
    "results/<int:result_id>/photos/"
    "<int:photo_id>/thumbnail"
)
@jwt_required()
def safety_inspection_photo_thumbnail(
    job_id: int,
    result_id: int,
    photo_id: int,
) -> Any:
    user = current_api_user()
    _, result = viewable_safety_result(
        job_id,
        result_id,
        user,
    )
    photo = active_safety_inspection_photo(
        result,
        photo_id,
    )
    path = commission_photo_path(
        photo.thumbnail_storage_name
    )

    if not path.is_file():
        abort(404)

    return send_file(
        path,
        mimetype=photo.mime_type,
        conditional=True,
        max_age=86_400,
    )


def can_view_all_jobs(user: User) -> bool:
    return (
        user.is_admin
        or user.is_foreman
        or user.can_manage_safety_inspections
        or user.can_review_safety_inspections
    )


def can_edit_safety_inspection_job(
    job: SafetyInspectionJob,
    user: User,
) -> bool:
    return (
        job.assigned_user_id == user.id
        or user.is_admin
        or user.can_manage_safety_inspections
        or user.can_review_safety_inspections
    )


def viewable_safety_inspection_user_ids(
    user: User,
) -> set[int] | None:
    if can_view_all_jobs(user):
        return None

    return viewable_commission_user_ids(user)


def viewable_safety_inspection_job(
    job_id: int,
    user: User,
) -> SafetyInspectionJob:
    write_request = request.method not in {"GET", "HEAD", "OPTIONS"}
    job = db.session.get(
        SafetyInspectionJob,
        job_id,
        with_for_update=write_request,
        populate_existing=write_request,
    )

    if job is None:
        abort(404)

    if write_request:
        if not can_edit_safety_inspection_job(job, user):
            abort(403)
    else:
        allowed_user_ids = viewable_safety_inspection_user_ids(user)

        if (
            allowed_user_ids is not None
            and job.assigned_user_id not in allowed_user_ids
        ):
            abort(403)

    return job


def viewable_safety_result(
    job_id: int,
    result_id: int,
    user: User,
) -> tuple[
    SafetyInspectionJob,
    SafetyInspectionResult,
]:
    job = viewable_safety_inspection_job(
        job_id,
        user,
    )
    query = SafetyInspectionResult.query.filter_by(
        id=result_id,
        job_id=job.id,
    )
    if request.method in {"POST", "PATCH", "DELETE"}:
        query = query.populate_existing().with_for_update()

    result = query.one_or_none()

    if result is None:
        abort(404)

    return job, result


def active_safety_inspection_photo(
    result: SafetyInspectionResult,
    photo_id: int,
) -> SafetyInspectionPhoto:
    photo = (
        SafetyInspectionPhoto.query.filter_by(
            id=photo_id,
            result_id=result.id,
            active=True,
        ).one_or_none()
    )

    if photo is None:
        abort(404)

    return photo


def _safety_inspection_progress_by_job(
    job_ids: list[int],
) -> dict[int, dict[str, int]]:
    progress_by_job = {
        job_id: {
            "result_count": 0,
            "checked_count": 0,
            "defect_count": 0,
            "photo_count": 0,
        }
        for job_id in job_ids
    }

    for offset in range(0, len(job_ids), 500):
        batch_ids = job_ids[offset:offset + 500]

        result_rows = (
            db.session.query(
                SafetyInspectionResult.job_id,
                func.count(SafetyInspectionResult.id),
                func.sum(
                    case(
                        (
                            SafetyInspectionResult.state
                            != SafetyInspectionResultState.NOT_CHECKED.value,
                            1,
                        ),
                        else_=0,
                    )
                ),
            )
            .filter(SafetyInspectionResult.job_id.in_(batch_ids))
            .group_by(SafetyInspectionResult.job_id)
            .all()
        )

        for job_id, result_count, checked_count in result_rows:
            progress_by_job[job_id]["result_count"] = int(result_count)
            progress_by_job[job_id]["checked_count"] = int(checked_count or 0)

        defect_rows = (
            db.session.query(
                SafetyInspectionDefect.job_id,
                func.count(SafetyInspectionDefect.id),
            )
            .filter(SafetyInspectionDefect.job_id.in_(batch_ids))
            .group_by(SafetyInspectionDefect.job_id)
            .all()
        )

        for job_id, defect_count in defect_rows:
            progress_by_job[job_id]["defect_count"] = int(defect_count)

        photo_rows = (
            db.session.query(
                SafetyInspectionResult.job_id,
                func.count(SafetyInspectionPhoto.id),
            )
            .select_from(SafetyInspectionPhoto)
            .join(
                SafetyInspectionResult,
                SafetyInspectionResult.id == SafetyInspectionPhoto.result_id,
            )
            .filter(
                SafetyInspectionResult.job_id.in_(batch_ids),
                SafetyInspectionPhoto.active.is_(True),
            )
            .group_by(SafetyInspectionResult.job_id)
            .all()
        )

        for job_id, photo_count in photo_rows:
            progress_by_job[job_id]["photo_count"] = int(photo_count)

    return progress_by_job


def serialize_safety_inspection_job(
    job: SafetyInspectionJob,
    viewer: User,
    include_details: bool = False,
    progress: dict[str, int] | None = None,
) -> dict[str, Any]:
    if progress is None:
        progress = {
            "result_count": len(job.results),
            "checked_count": sum(
                result.state != SafetyInspectionResultState.NOT_CHECKED.value
                for result in job.results
            ),
            "defect_count": len(job.defects),
            "photo_count": sum(
                photo.active
                for result in job.results
                for photo in result.photos
            ),
        }


    item = {
        "id": job.id,
        "public_id": job.public_id,
        "status": job.status,
        "period_key": job.period_key,
        "period_start": (
            job.period_start.isoformat()
        ),
        "period_end": job.period_end.isoformat(),
        "due_from": job.due_from.isoformat(),
        "due_to": job.due_to.isoformat(),
        "warning_on": job.warning_on.isoformat(),
        "missed_on": job.missed_on.isoformat(),
        "is_due": job.is_due,
        "is_overdue": job.is_overdue,
        "is_terminal": job.is_terminal,
        "can_edit": (
            not job.is_terminal
            and can_edit_safety_inspection_job(job, viewer)
        ),
        "group": {
            "id": job.group.id,
            "name": job.group.name,
        },
        "template": {
            "id": (
                job.template_version.template.id
            ),
            "name": (
                job.template_version.template.name
            ),
            "version": (
                job.template_version.version_number
            ),
        },
        "department": (
            {
                "id": job.department.id,
                "erp_id": job.department.erp_id,
                "name": job.department.name,
            }
            if job.department is not None
            else None
        ),
        "assigned_user": (
            serialize_user(job.assigned_user)
            if job.assigned_user is not None
            else None
        ),
        "object": {
            "level": job.object_level,
            "erp_id": job.object_erp_id,
            "address": job.object_address,
            "location": job.object_location,
            "company_code_erp_id": (
                job.company_code_erp_id
            ),
            "company_code_code": (
                job.company_code_code
            ),
            "economic_unit_erp_id": (
                job.economic_unit_erp_id
            ),
            "economic_unit_idnum": (
                job.economic_unit_idnum
            ),
            "economic_unit_name": (
                job.economic_unit_name
            ),
            "building_erp_id": (
                job.building_erp_id
            ),
            "building_idnum": job.building_idnum,
            "use_unit_erp_id": (
                job.use_unit_erp_id
            ),
            "use_unit_idnum": job.use_unit_idnum,
            "latitude": job.latitude,
            "longitude": job.longitude,
            "has_coordinates": (
                job.latitude is not None
                and job.longitude is not None
            ),
        },
        "progress": progress,
        "overall_note": job.overall_note,
        "started_at": iso_datetime(
            job.started_at
        ),
        "completed_at": iso_datetime(
            job.completed_at
        ),
        "completed_by": (
            serialize_user(job.completed_by)
            if job.completed_by is not None
            else None
        ),
        "created_at": iso_datetime(
            job.created_at
        ),
        "updated_at": iso_datetime(
            job.updated_at
        ),
    }

    if include_details:
        item["results"] = [
            serialize_safety_inspection_result(
                result
            )
            for result in job.results
        ]

    return item


def serialize_safety_inspection_result(
    result: SafetyInspectionResult,
) -> dict[str, Any]:
    photos = sorted(
        (
            photo
            for photo in result.photos
            if photo.active
        ),
        key=lambda photo: (
            photo.created_at,
            photo.id,
        ),
    )

    return {
        "id": result.id,
        "public_id": result.public_id,
        "position_index": result.position_index,
        "title": result.item_title,
        "catalog_position": result.item_catalog_position,
        "section_key": result.section_key,
        "section_title": result.section_title,
        "description": result.item_description,
        "help_text": result.item_help_text,
        "required": result.required,
        "allow_not_present": (
            result.allow_not_present
        ),
        "photo_requirements": {
            "no_defect": (
                result.photo_required_no_defect
            ),
            "not_present": (
                result.photo_required_not_present
            ),
            "defect_info": (
                result.photo_required_defect_info
            ),
            "defect_urgent": (
                result.photo_required_defect_urgent
            ),
        },
        "state": result.state,
        "note": result.note,
        "immediate_action": (
            result.immediate_action
        ),
        "version": result.version,
        "checked_at": iso_datetime(
            result.checked_at
        ),
        "checked_by": (
            serialize_user(result.checked_by)
            if result.checked_by is not None
            else None
        ),
        "photos": [
            serialize_safety_inspection_photo(
                photo
            )
            for photo in photos
        ],
        "defect": (
            serialize_safety_inspection_defect(
                result.defect
            )
            if result.defect is not None
            else None
        ),
        "updated_at": iso_datetime(
            result.updated_at
        ),
    }


def serialize_safety_inspection_photo(
    photo: SafetyInspectionPhoto,
) -> dict[str, Any]:
    result = photo.result
    base_path = (
        f"/api/safety-inspections/"
        f"{result.job_id}/results/{result.id}/"
        f"photos/{photo.id}"
    )

    return {
        "id": photo.id,
        "public_id": photo.public_id,
        "source": photo.source,
        "original_filename": (
            photo.original_filename
        ),
        "mime_type": photo.mime_type,
        "size_bytes": photo.size_bytes,
        "caption": photo.caption,
        "captured_at": iso_datetime(
            photo.captured_at
        ),
        "created_at": iso_datetime(
            photo.created_at
        ),
        "updated_at": iso_datetime(
            photo.updated_at
        ),
        "uploaded_by": serialize_user(
            photo.uploaded_by
        ),
        "content_path": (
            f"{base_path}/content"
        ),
        "thumbnail_path": (
            f"{base_path}/thumbnail"
        ),
    }


def serialize_safety_inspection_defect(
    defect: SafetyInspectionDefect,
) -> dict[str, Any]:
    return {
        "id": defect.id,
        "public_id": defect.public_id,
        "severity": defect.severity,
        "status": defect.status,
        "description": defect.description,
        "immediate_action": (
            defect.immediate_action
        ),
        "review_note": defect.review_note,
        "created_at": iso_datetime(
            defect.created_at
        ),
        "commission_ids": [
            link.commission_id
            for link in defect.commission_links
        ],
    }


def parse_optional_datetime(
    value: Any,
) -> datetime | None:
    normalized = str(value or "").strip()

    if not normalized:
        return None

    try:
        parsed = datetime.fromisoformat(
            normalized.replace(
                "Z",
                "+00:00",
            )
        )
    except ValueError as exc:
        raise ValueError(
            "Das Aufnahmedatum ist ungültig."
        ) from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(
            tzinfo=timezone.utc
        )

    return parsed


def clean_optional_text(
    value: Any,
    maximum_length: int,
) -> str | None:
    normalized = str(value or "").strip()

    if not normalized:
        return None

    return normalized[:maximum_length]


def remove_stored_photo(
    storage_name: str,
    thumbnail_storage_name: str,
) -> None:
    for name in {
        storage_name,
        thumbnail_storage_name,
    }:
        try:
            commission_photo_path(name).unlink(
                missing_ok=True
            )
        except OSError:
            current_app.logger.exception(
                "safety_inspection_photo_cleanup_failed "
                "storage_name=%s",
                name,
            )


@safety_inspection_api_bp.post("/safety-inspections/offline-details")
@jwt_required()
def safety_inspection_offline_details() -> Any:
    user = current_api_user()
    data = request.get_json(silent=True)
    ids = data.get("ids") if isinstance(data, dict) else None

    if (
        not isinstance(ids, list)
        or not 1 <= len(ids) <= 50
        or any(type(value) is not int or not 0 < value <= 2**63 - 1 for value in ids)
    ):
        return jsonify({"message": "Bitte 1 bis 50 gültige Prüfaufträge angeben."}), 400

    ids = list(dict.fromkeys(ids))
    query = SafetyInspectionJob.query.filter(SafetyInspectionJob.id.in_(ids))
    allowed_ids = viewable_safety_inspection_user_ids(user)

    if allowed_ids is not None:
        query = query.filter(SafetyInspectionJob.assigned_user_id.in_(allowed_ids))

    items = query.options(
        selectinload(SafetyInspectionJob.group),
        selectinload(SafetyInspectionJob.template_version).selectinload(
            SafetyInspectionTemplateVersion.template
        ),
        selectinload(SafetyInspectionJob.department),
        selectinload(SafetyInspectionJob.assigned_user),
        selectinload(SafetyInspectionJob.completed_by),
        selectinload(SafetyInspectionJob.defects),
        selectinload(SafetyInspectionJob.results).selectinload(
            SafetyInspectionResult.checked_by
        ),
        selectinload(SafetyInspectionJob.results).selectinload(
            SafetyInspectionResult.photos
        ).selectinload(SafetyInspectionPhoto.uploaded_by),
        selectinload(SafetyInspectionJob.results).selectinload(
            SafetyInspectionResult.defect
        ).selectinload(SafetyInspectionDefect.commission_links),
    ).all()

    by_id = {item.id: item for item in items}
    return jsonify({
        "items": [
            serialize_safety_inspection_job(by_id[job_id], user, include_details=True)
            for job_id in ids
            if job_id in by_id
        ],
        "unavailable_ids": [job_id for job_id in ids if job_id not in by_id],
    })


@safety_inspection_api_bp.post(
    "/safety-inspections/<int:job_id>/offline-operations"
)
@jwt_required()
def safety_inspection_offline_operation(job_id: int) -> Any:
    user = current_api_user()
    try:
        data = (
            json.loads(request.form.get("operation", "{}"))
            if request.mimetype == "multipart/form-data"
            else request.get_json(silent=True)
        )
    except ValueError:
        return jsonify({"message": "Ungültiger JSON-Body."}), 400

    if not isinstance(data, dict):
        return jsonify({"message": "Ungültiger Vorgang."}), 400

    try:
        operation_id = str(UUID(str(data.get("operation_id", ""))))
        if type(data.get("job_id")) is not int or data["job_id"] != job_id:
            raise ValueError("Der Prüfauftrag stimmt nicht mit dem Vorgang überein.")
        kind = data.get("kind")
        if kind not in {"result", "photo", "delete_photo", "complete"}:
            raise ValueError("Der Vorgang ist ungültig.")
        occurred_at = parse_optional_datetime(data.get("occurred_at"))
        upload = request.files.get("photo")
        raw_data = upload.stream.read(MAX_PHOTO_BYTES + 1) if upload else b""
        if kind == "photo" and not raw_data:
            raise ValueError("Die Bilddatei fehlt.")
        if len(raw_data) > MAX_PHOTO_BYTES:
            raise ValueError("Die Bilddatei ist zu groß.")
        data["operation_id"] = operation_id
        request_sha256 = hashlib.sha256(
            json.dumps(
                data, sort_keys=True, ensure_ascii=False,
                separators=(",", ":"), allow_nan=False,
            ).encode("utf-8")
            + b"\0" + hashlib.sha256(raw_data).digest()
        ).hexdigest()
    except (ValueError, TypeError) as exc:
        return jsonify({"message": str(exc)}), 400

    def existing_response(locking=False):
        receipt = db.session.get(
            SafetyInspectionMobileOperation,
            (user.id, operation_id),
            populate_existing=True,
            with_for_update=locking,
        )
        if receipt is None:
            return None
        if receipt.job_id != job_id or receipt.request_sha256 != request_sha256:
            return jsonify({
                "message": "Diese Vorgangs-ID wurde bereits für andere Daten verwendet."
            }), 409
        return jsonify(receipt.response_payload)

    existing = existing_response()
    if existing is not None:
        return existing

    stored_photo = None
    try:
        job = viewable_safety_inspection_job(job_id, user)
        existing = existing_response(locking=True)
        if existing is not None:
            return existing
        ensure_job_editable(job, user)

        result = None
        photo_id = None
        if kind != "complete":
            result_id = data.get("result_id")
            if type(result_id) is not int:
                raise ValueError("Der Prüfpunkt fehlt.")
            result = SafetyInspectionResult.query.filter_by(
                id=result_id, job_id=job.id,
            ).populate_existing().with_for_update().one_or_none()
            if result is None:
                raise ValueError("Der Prüfpunkt gehört nicht zu diesem Prüfauftrag.")

        if kind == "result":
            version = data.get("version")
            if type(version) is not int or version < 1:
                raise ValueError("Die Ausgangsversion fehlt.")
            update_safety_inspection_result(
                result=result,
                state=str(data.get("state") or "").strip(),
                note=data.get("note"),
                immediate_action=data.get("immediate_action"),
                user=user,
                expected_version=version,
            )

        elif kind == "photo":
            source = data.get("source")
            if not isinstance(source, str) or source not in {
                item.value for item in SafetyInspectionPhotoSource
            }:
                raise ValueError("Die Fotoquelle ist ungültig.")
            stored_photo = store_commission_photo_bytes(
                commission=None,
                raw_data=raw_data,
                original_filename=upload.filename or "prueffoto.jpg",
                storage_directory=(
                    Path("safety_inspections") / job.public_id / result.public_id
                ),
            )
            photo = SafetyInspectionPhoto.query.filter_by(
                result_id=result.id,
                content_sha256=stored_photo.content_sha256,
                active=True,
            ).order_by(SafetyInspectionPhoto.id).first()
            if photo is None:
                photo = SafetyInspectionPhoto(
                    public_id=operation_id,
                    result=result,
                    uploaded_by=user,
                    source=source,
                    original_filename=stored_photo.original_filename,
                    storage_name=stored_photo.storage_name,
                    thumbnail_storage_name=stored_photo.thumbnail_storage_name,
                    mime_type=stored_photo.mime_type,
                    size_bytes=stored_photo.size_bytes,
                    content_sha256=stored_photo.content_sha256,
                    caption=clean_optional_text(data.get("caption"), 500),
                    captured_at=parse_optional_datetime(data.get("captured_at")),
                    active=True,
                )
                db.session.add(photo)
                db.session.flush()
            else:
                remove_stored_photo(
                    stored_photo.storage_name, stored_photo.thumbnail_storage_name
                )
                stored_photo = None
            photo_id = photo.id

        elif kind == "delete_photo":
            photo_id = data.get("photo_id")
            parent_id = data.get("photo_operation_id")
            if parent_id:
                parent = db.session.get(
                    SafetyInspectionMobileOperation,
                    (user.id, str(UUID(str(parent_id)))),
                )
                if parent is None or parent.job_id != job.id:
                    raise ValueError("Der vorherige Foto-Upload fehlt.")
                photo_id = parent.response_payload.get("photo_id")
            if type(photo_id) is not int:
                raise ValueError("Das Foto fehlt.")
            photo = SafetyInspectionPhoto.query.filter_by(
                id=photo_id, result_id=result.id,
            ).one_or_none()
            if photo is None:
                raise ValueError("Das Foto gehört nicht zu diesem Prüfpunkt.")
            photo.active = False
            photo.deleted_at = utc_now()

        else:
            complete_safety_inspection_job(
                job, user, overall_note=data.get("overall_note")
            )

        job.updated_at = utc_now()
        db.session.flush()
        db.session.expire(job, ["results", "defects"])
        payload = {
            "item": serialize_safety_inspection_job(job, user, include_details=True),
            "operation_id": operation_id,
            "photo_id": photo_id,
        }
        db.session.add(SafetyInspectionMobileOperation(
            user_id=user.id,
            operation_id=operation_id,
            job_id=job.id,
            request_sha256=request_sha256,
            response_payload=payload,
            occurred_at=occurred_at,
        ))
        audit("safety_inspection.offline_operation", "safety_inspection_job", job.id, {
            "operation_id": operation_id,
            "kind": kind,
            "user_id": user.id,
            "occurred_at": data.get("occurred_at"),
            "replaces_operation_id": data.get("replaces_operation_id"),
        })
        db.session.commit()
        return jsonify(payload)
    except HTTPException as exc:
        db.session.rollback()
        if stored_photo is not None:
            remove_stored_photo(
                stored_photo.storage_name,
                stored_photo.thumbnail_storage_name,
            )
        response = jsonify({"message": exc.description})
        if exc.code in {403, 404, 409, 422}:
            record_safety_inspection_mobile_issue(
                user, job_id, data, exc.description, exc.code
            )
        response.status_code = exc.code
        return response
    except (ValueError, IntegrityError) as exc:
        db.session.rollback()
        if stored_photo is not None:
            remove_stored_photo(
                stored_photo.storage_name,
                stored_photo.thumbnail_storage_name,
            )
        if isinstance(exc, IntegrityError):
            existing = existing_response()
            if existing is not None:
                return existing
            record_safety_inspection_mobile_issue(
                user,
                job_id,
                data,
                "Der Vorgang konnte nicht eindeutig gespeichert werden.",
                409,
            )
            return jsonify({
                "message": "Der Vorgang konnte nicht eindeutig gespeichert werden."
            }), 409
        record_safety_inspection_mobile_issue(
            user, job_id, data, str(exc), 409
        )
        return jsonify({"message": str(exc)}), 409
    except Exception:
        db.session.rollback()
        if stored_photo is not None:
            remove_stored_photo(
                stored_photo.storage_name,
                stored_photo.thumbnail_storage_name,
            )
        current_app.logger.exception(
            "safety_inspection_offline_failed job_id=%s operation_id=%s",
            job_id,
            operation_id,
        )
        return jsonify({
            "message": "Der Vorgang wird später erneut übertragen."
        }), 503

def record_safety_inspection_mobile_issue(
    user: User,
    job_id: int,
    data: dict[str, Any],
    reason: str,
    status: int,
) -> None:
    try:
        entity_id = f"{user.id}:{data['operation_id']}"
        existing = AuditLog.query.filter_by(
            action="safety_inspection.mobile_issue",
            entity_type="safety_inspection_mobile_operation",
            entity_id=entity_id,
        ).first()

        if existing is None:
            audit(
                "safety_inspection.mobile_issue",
                "safety_inspection_mobile_operation",
                entity_id,
                {
                    "job_id": job_id,
                    "operation": data,
                    "reason": reason,
                    "status": status,
                },
                actor_user_id=user.id,
            )
            db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "safety_inspection_mobile_issue_failed job_id=%s operation_id=%s",
            job_id,
            data.get("operation_id"),
        )
