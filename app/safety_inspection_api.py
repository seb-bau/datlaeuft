from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

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

    items = query.order_by(
        SafetyInspectionJob.due_to.asc(),
        SafetyInspectionJob.due_from.asc(),
        SafetyInspectionJob.id.asc(),
    ).all()

    return jsonify({
        "items": [
            serialize_safety_inspection_job(item, user)
            for item in items
        ],
    })


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


def serialize_safety_inspection_job(
    job: SafetyInspectionJob,
    viewer: User,
    include_details: bool = False,
) -> dict[str, Any]:
    checked_count = sum(
        1
        for result in job.results
        if (
            result.state
            != SafetyInspectionResultState
            .NOT_CHECKED.value
        )
    )
    photo_count = sum(
        1
        for result in job.results
        for photo in result.photos
        if photo.active
    )

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
        "progress": {
            "result_count": len(job.results),
            "checked_count": checked_count,
            "defect_count": len(job.defects),
            "photo_count": photo_count,
        },
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