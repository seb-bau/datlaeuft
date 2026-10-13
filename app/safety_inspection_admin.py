from __future__ import annotations

import json
from datetime import date
from typing import Any
from uuid import uuid4
import json
import re

from flask import (
    Blueprint,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
    current_app,
)
from flask_login import current_user, login_required
from sqlalchemy.orm import selectinload
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .audit import audit
from .auth import (
    safety_inspection_manage_required,
    safety_inspection_review_required,
)
from .extensions import db
from .models import (
    CraftActivity,
    SafetyInspectionDefect,
    SafetyInspectionDefectStatus,
    SafetyInspectionGenerationMode,
    SafetyInspectionGroup,
    SafetyInspectionGroupTarget,
    SafetyInspectionJob,
    SafetyInspectionJobStatus,
    SafetyInspectionObjectLevel,
    SafetyInspectionSchedule,
    SafetyInspectionScheduleFrequency,
    SafetyInspectionTemplate,
    SafetyInspectionTemplateItem,
    SafetyInspectionTemplateVersion,
    SafetyInspectionTemplateVersionStatus,
    TimeTaskTemplate,
    User,
    WowiBuilding,
    WowiCommissionType,
    WowiDepartment,
    WowiEconomicUnit,
    WowiUseUnit,
    PushEventType,
    SafetyInspectionSelectionMode,
    SafetyInspectionRegexAction,
)
from .safety_inspection_import import (
    MAX_IMPORT_BYTES,
    resolve_safety_inspection_import,
)
from .safety_inspection_reporting import build_safety_inspection_report
from .safety_inspection_progress import get_safety_inspection_group_progress
from .safety_inspection_reset import reset_unprocessed_safety_inspection_jobs
from .safety_inspection_service import (
    create_safety_inspection_revision,
    create_safety_inspection_template,
    generate_safety_inspection_jobs,
    publish_safety_inspection_version,
    resolve_group_object_contexts,
    validate_safety_inspection_group,
    validate_safety_inspection_schedule,
)
from .push_service import (
    enqueue_safety_inspection_push,
)


safety_inspection_admin_bp = Blueprint(
    "safety_inspection_admin",
    __name__,
)


@safety_inspection_admin_bp.get("/admin/reports/safety-inspections")
@login_required
@safety_inspection_review_required
def report() -> Any:
    groups = (
        SafetyInspectionGroup.query
        .options(selectinload(SafetyInspectionGroup.schedule))
        .order_by(SafetyInspectionGroup.name, SafetyInspectionGroup.id)
        .all()
    )
    report_data = build_safety_inspection_report(groups, request.args)
    return render_template(
        "admin/report_safety_inspections.html",
        groups=groups,
        report=report_data,
    ), 400 if report_data["error"] else 200


@safety_inspection_admin_bp.get(
    "/admin/safety-inspections"
)
@login_required
@safety_inspection_review_required
def dashboard() -> Any:
    open_statuses = {
        SafetyInspectionJobStatus.PLANNED.value,
        SafetyInspectionJobStatus.IN_PROGRESS.value,
    }
    open_defect_statuses = {
        SafetyInspectionDefectStatus.OPEN.value,
        SafetyInspectionDefectStatus
        .COMMISSION_CREATED.value,
        SafetyInspectionDefectStatus
        .IN_PROGRESS.value,
        SafetyInspectionDefectStatus
        .REVIEW_REQUIRED.value,
    }

    return render_template(
        "admin/safety_inspection_dashboard.html",
        template_count=(
            SafetyInspectionTemplate.query.filter_by(
                active=True
            ).count()
        ),
        schedule_count=(
            SafetyInspectionSchedule.query.filter_by(
                active=True
            ).count()
        ),
        group_count=(
            SafetyInspectionGroup.query.filter_by(
                active=True
            ).count()
        ),
        open_job_count=(
            SafetyInspectionJob.query.filter(
                SafetyInspectionJob.status.in_(
                    open_statuses
                )
            ).count()
        ),
        overdue_job_count=(
            SafetyInspectionJob.query.filter(
                SafetyInspectionJob.status.in_(
                    open_statuses
                ),
                SafetyInspectionJob.due_to
                < date.today(),
            ).count()
        ),
        open_defect_count=(
            SafetyInspectionDefect.query.filter(
                SafetyInspectionDefect.status.in_(
                    open_defect_statuses
                )
            ).count()
        ),
    )


@safety_inspection_admin_bp.get(
    "/admin/safety-inspections/templates"
)
@login_required
@safety_inspection_manage_required
def templates() -> Any:
    items = SafetyInspectionTemplate.query.order_by(
        SafetyInspectionTemplate.active.desc(),
        SafetyInspectionTemplate.name.asc(),
    ).all()

    return render_template(
        "admin/safety_inspection_templates.html",
        items=items,
        version_status_labels={
            SafetyInspectionTemplateVersionStatus
            .DRAFT.value: "Entwurf",
            SafetyInspectionTemplateVersionStatus
            .PUBLISHED.value: "Veröffentlicht",
            SafetyInspectionTemplateVersionStatus
            .ARCHIVED.value: "Archiviert",
        },
    )


@safety_inspection_admin_bp.route(
    "/admin/safety-inspections/templates/new",
    methods=["GET", "POST"],
)
@login_required
@safety_inspection_manage_required
def template_new() -> Any:
    if request.method == "POST":
        try:
            template, version = (
                create_safety_inspection_template(
                    name=request.form.get(
                        "name",
                        "",
                    ),
                    description=request.form.get(
                        "description"
                    ),
                    legal_reference=request.form.get(
                        "legal_reference"
                    ),
                    user=current_user,
                )
            )
            db.session.commit()
        except (ValueError, IntegrityError) as exc:
            db.session.rollback()
            flash(_write_error(exc), "danger")
        else:
            audit(
                "safety_inspection.template_created",
                "safety_inspection_template",
                template.id,
            )
            db.session.commit()
            flash(
                "Der Prüfdetailplan wurde angelegt.",
                "success",
            )
            return redirect(
                url_for(
                    "safety_inspection_admin."
                    "template_edit",
                    template_id=template.id,
                    version_id=version.id,
                )
            )

    return render_template(
        "admin/safety_inspection_template_new.html"
    )


@safety_inspection_admin_bp.route(
    "/admin/safety-inspections/templates/"
    "<int:template_id>",
    methods=["GET", "POST"],
)
@login_required
@safety_inspection_manage_required
def template_edit(template_id: int) -> Any:
    template = db.session.get(
        SafetyInspectionTemplate,
        template_id,
    )

    if template is None:
        abort(404)

    version = _selected_template_version(template)

    if version is None:
        abort(404)

    if request.method == "POST":
        if version.status != (
            SafetyInspectionTemplateVersionStatus
            .DRAFT.value
        ):
            flash(
                "Nur ein Entwurf kann bearbeitet "
                "werden.",
                "danger",
            )
            return redirect(
                url_for(
                    "safety_inspection_admin."
                    "template_edit",
                    template_id=template.id,
                    version_id=version.id,
                )
            )

        try:
            name = request.form.get(
                "name",
                "",
            ).strip()

            if not name:
                raise ValueError(
                    "Der Prüfdetailplan benötigt "
                    "einen Namen."
                )

            template.name = name
            template.description = _clean_text(
                request.form.get("description")
            )
            template.legal_reference = _clean_text(
                request.form.get(
                    "legal_reference"
                )
            )
            template.active = (
                "active" in request.form
            )
            version.change_note = _clean_text(
                request.form.get("change_note")
            )
            _replace_template_items(version)
            db.session.commit()
        except (ValueError, IntegrityError) as exc:
            db.session.rollback()
            flash(_write_error(exc), "danger")
        else:
            audit(
                "safety_inspection.template_updated",
                "safety_inspection_template",
                template.id,
                {"version_id": version.id},
            )
            db.session.commit()
            flash(
                "Der Prüfdetailplan wurde "
                "gespeichert.",
                "success",
            )
            return redirect(
                url_for(
                    "safety_inspection_admin."
                    "template_edit",
                    template_id=template.id,
                    version_id=version.id,
                )
            )

    item_rows = [
        {
            "stable_key": item.stable_key,
            "title": item.title,
            "description": (
                item.description or ""
            ),
            "help_text": item.help_text or "",
            "required": item.required,
            "allow_not_present": (
                item.allow_not_present
            ),
            "photo_required_no_defect": (
                item.photo_required_no_defect
            ),
            "photo_required_not_present": (
                item.photo_required_not_present
            ),
            "photo_required_defect_info": (
                item.photo_required_defect_info
            ),
            "photo_required_defect_urgent": (
                item.photo_required_defect_urgent
            ),
        }
        for item in sorted(
            version.items,
            key=lambda value: (
                value.position_index
            ),
        )
    ]

    return render_template(
        "admin/safety_inspection_template_form.html",
        item=template,
        version=version,
        versions=sorted(
            template.versions,
            key=lambda value: (
                value.version_number
            ),
            reverse=True,
        ),
        item_rows=item_rows,
        draft_status=(
            SafetyInspectionTemplateVersionStatus
            .DRAFT.value
        ),
        published_status=(
            SafetyInspectionTemplateVersionStatus
            .PUBLISHED.value
        ),
    )


@safety_inspection_admin_bp.post(
    "/admin/safety-inspections/templates/"
    "<int:template_id>/revision"
)
@login_required
@safety_inspection_manage_required
def template_revision(
    template_id: int,
) -> Any:
    template = db.session.get(
        SafetyInspectionTemplate,
        template_id,
    )

    if template is None:
        abort(404)

    try:
        version = create_safety_inspection_revision(
            template,
            current_user,
        )
        db.session.commit()
    except (ValueError, IntegrityError) as exc:
        db.session.rollback()
        flash(_write_error(exc), "danger")
        return redirect(
            url_for(
                "safety_inspection_admin.templates"
            )
        )

    audit(
        "safety_inspection.revision_created",
        "safety_inspection_template",
        template.id,
        {"version_id": version.id},
    )
    db.session.commit()
    flash(
        "Die neue Entwurfsversion wurde "
        "angelegt.",
        "success",
    )

    return redirect(
        url_for(
            "safety_inspection_admin."
            "template_edit",
            template_id=template.id,
            version_id=version.id,
        )
    )


@safety_inspection_admin_bp.post(
    "/admin/safety-inspections/templates/"
    "<int:template_id>/versions/"
    "<int:version_id>/publish"
)
@login_required
@safety_inspection_manage_required
def template_publish(
    template_id: int,
    version_id: int,
) -> Any:
    version = db.session.get(
        SafetyInspectionTemplateVersion,
        version_id,
    )

    if (
        version is None
        or version.template_id != template_id
    ):
        abort(404)

    try:
        publish_safety_inspection_version(
            version,
            current_user,
            request.form.get("change_note"),
        )
        db.session.commit()
    except (ValueError, IntegrityError) as exc:
        db.session.rollback()
        flash(_write_error(exc), "danger")
    else:
        audit(
            "safety_inspection."
            "version_published",
            "safety_inspection_template_version",
            version.id,
        )
        db.session.commit()
        flash(
            "Die Version wurde veröffentlicht.",
            "success",
        )

    return redirect(
        url_for(
            "safety_inspection_admin."
            "template_edit",
            template_id=template_id,
            version_id=version_id,
        )
    )


@safety_inspection_admin_bp.route(
    "/admin/safety-inspections/schedules",
    methods=["GET", "POST"],
)
@login_required
@safety_inspection_manage_required
def schedules() -> Any:
    if request.method == "POST":
        item = SafetyInspectionSchedule(
            created_by=current_user
        )

        try:
            _save_schedule(item)
            db.session.add(item)
            db.session.commit()
        except (ValueError, IntegrityError) as exc:
            db.session.rollback()
            flash(_write_error(exc), "danger")
        else:
            audit(
                "safety_inspection."
                "schedule_created",
                "safety_inspection_schedule",
                item.id,
            )
            db.session.commit()
            flash(
                "Der Prüfzeitplan wurde "
                "angelegt.",
                "success",
            )
            return redirect(
                url_for(
                    "safety_inspection_admin."
                    "schedules"
                )
            )

    return render_template(
        "admin/safety_inspection_schedules.html",
        items=(
            SafetyInspectionSchedule.query
            .order_by(
                SafetyInspectionSchedule
                .active.desc(),
                SafetyInspectionSchedule
                .name.asc(),
            )
            .all()
        ),
        frequency_labels=(
            _frequency_labels()
        ),
        generation_mode_labels=(
            _generation_mode_labels()
        ),
    )


@safety_inspection_admin_bp.post(
    "/admin/safety-inspections/schedules/"
    "<int:schedule_id>"
)
@login_required
@safety_inspection_manage_required
def schedule_update(
    schedule_id: int,
) -> Any:
    item = db.session.get(
        SafetyInspectionSchedule,
        schedule_id,
    )

    if item is None:
        abort(404)

    try:
        _save_schedule(item)
        db.session.commit()
    except (ValueError, IntegrityError) as exc:
        db.session.rollback()
        flash(_write_error(exc), "danger")
    else:
        audit(
            "safety_inspection."
            "schedule_updated",
            "safety_inspection_schedule",
            item.id,
        )
        db.session.commit()
        flash(
            "Der Prüfzeitplan wurde "
            "gespeichert.",
            "success",
        )

    return redirect(
        url_for(
            "safety_inspection_admin."
            "schedules"
        )
    )


@safety_inspection_admin_bp.get(
    "/admin/safety-inspections/groups"
)
@login_required
@safety_inspection_manage_required
def groups() -> Any:
    items = (
        SafetyInspectionGroup.query
        .options(selectinload(SafetyInspectionGroup.schedule))
        .order_by(
            SafetyInspectionGroup.active.desc(),
            SafetyInspectionGroup.name.asc(),
        )
        .all()
    )

    return render_template(
        "admin/safety_inspection_groups.html",
        items=items,
        group_progress=get_safety_inspection_group_progress(items),
        object_level_labels=_object_level_labels(),
        selection_mode_labels=_selection_mode_labels(),
    )


@safety_inspection_admin_bp.route(
    "/admin/safety-inspections/groups/new",
    methods=["GET", "POST"],
)
@login_required
@safety_inspection_manage_required
def group_new() -> Any:
    item = SafetyInspectionGroup(
        created_by=current_user,
        first_period_start=date.today(),
        selection_mode=(
            SafetyInspectionSelectionMode
            .WHITELIST.value
        ),
        include_vacancies=True,
        automatic_archive=True,
        archive_original_photos=False,
        active=True,
    )

    if request.method == "POST":
        try:
            _save_group(item)
            db.session.add(item)
            db.session.flush()
            validate_safety_inspection_group(
                item
            )
            db.session.commit()
        except (ValueError, IntegrityError) as exc:
            db.session.rollback()
            flash(_write_error(exc), "danger")
        else:
            audit(
                "safety_inspection.group_created",
                "safety_inspection_group",
                item.id,
            )
            db.session.commit()
            flash(
                "Die Prüfgruppe wurde angelegt.",
                "success",
            )
            return redirect(
                url_for(
                    "safety_inspection_admin."
                    "group_edit",
                    group_id=item.id,
                )
            )

    return _render_group_form(item)


@safety_inspection_admin_bp.route(
    "/admin/safety-inspections/groups/"
    "<int:group_id>",
    methods=["GET", "POST"],
)
@login_required
@safety_inspection_manage_required
def group_edit(group_id: int) -> Any:
    is_post = request.method == "POST"
    item = db.session.get(
        SafetyInspectionGroup,
        group_id,
        with_for_update=is_post,
        populate_existing=is_post,
    )

    if item is None:
        abort(404)

    if is_post:
        action = request.form.get("action", "save")
        removed_count = 0

        try:
            if action not in {"save", "reset_unprocessed"}:
                raise ValueError("Die Gruppenaktion ist ungültig.")

            _save_group(item)
            db.session.flush()
            validate_safety_inspection_group(item)

            if action == "reset_unprocessed":
                removed_count = reset_unprocessed_safety_inspection_jobs(
                    item,
                    current_user,
                )
                audit(
                    "safety_inspection.group_jobs_reset",
                    "safety_inspection_group",
                    item.id,
                    {"removed_count": removed_count},
                )

            audit(
                "safety_inspection.group_updated",
                "safety_inspection_group",
                item.id,
            )
            db.session.commit()

        except (ValueError, IntegrityError) as exc:
            db.session.rollback()
            flash(_write_error(exc), "danger")

        except SQLAlchemyError:
            db.session.rollback()
            current_app.logger.exception(
                "safety_inspection_group_save_failed group_id=%s",
                group_id,
            )
            flash(
                "Die Aktion konnte nicht gespeichert werden. "
                "Bitte erneut versuchen.",
                "danger",
            )

        else:
            message = "Die Prüfgruppe wurde gespeichert."
            if action == "reset_unprocessed":
                message += (
                    f" {removed_count} unbearbeitete Prüfaufträge "
                    "wurden zurückgesetzt."
                )

            flash(message, "success")
            return redirect(url_for(
                "safety_inspection_admin.group_edit",
                group_id=item.id,
            ))

    return _render_group_form(item)


@safety_inspection_admin_bp.post(
    "/admin/safety-inspections/groups/import-objects"
)
@login_required
@safety_inspection_manage_required
def group_import_objects() -> Any:
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify({"error": "Bitte eine Datei auswählen."}), 400
    try:
        payload = resolve_safety_inspection_import(
            upload.filename,
            upload.stream.read(MAX_IMPORT_BYTES + 1),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(payload)


@safety_inspection_admin_bp.post(
    "/admin/safety-inspections/groups/preview"
)
@login_required
@safety_inspection_manage_required
def group_preview() -> Any:
    try:
        preview_group = (
            _group_preview_from_request()
        )
        contexts = (
            resolve_group_object_contexts(
                preview_group
            )
        )
    except ValueError as exc:
        return jsonify(
            {"error": str(exc)}
        ), 400

    object_level_labels = (
        _object_level_labels()
    )
    items = []

    for context in contexts:
        economic_unit_label = " · ".join(
            value
            for value in (
                context.economic_unit.id_num,
                context.economic_unit.name,
            )
            if value
        )
        building_label = (
            context.building.id_num
            if context.building is not None
            else ""
        )

        items.append(
            {
                "object_type": (
                    object_level_labels.get(
                        context.object_level,
                        context.object_level,
                    )
                ),
                "object_erp_id": (
                    context.object_erp_id
                ),
                "id_num": (
                    context.object_id_num or ""
                ),
                "economic_unit": (
                    economic_unit_label
                ),
                "building": building_label,
                "address": (
                    context.address or ""
                ),
                "location": (
                    context.location or ""
                ),
            }
        )

    return jsonify(
        {
            "count": len(items),
            "items": items,
        }
    )


@safety_inspection_admin_bp.post(
    "/admin/safety-inspections/groups/"
    "<int:group_id>/generate"
)
@login_required
@safety_inspection_manage_required
def group_generate(
    group_id: int,
) -> Any:
    item = db.session.get(
        SafetyInspectionGroup,
        group_id,
    )

    if item is None:
        abort(404)

    try:
        result = (
            generate_safety_inspection_jobs(
                group_id=item.id
            )
        )
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    else:
        audit(
            "safety_inspection."
            "group_generated",
            "safety_inspection_group",
            item.id,
            result,
        )
        db.session.commit()
        flash(
            "Prüfungen erstellt: "
            f"{result['created']}; "
            "bereits vorhanden: "
            f"{result['existing']}; "
            "ohne Zuordnung: "
            f"{result['unassigned']}; "
            f"Fehler: {result['errors']}.",
            (
                "success"
                if not result["errors"]
                else "warning"
            ),
        )

    return redirect(
        url_for(
            "safety_inspection_admin."
            "group_edit",
            group_id=group_id,
        )
    )


@safety_inspection_admin_bp.get(
    "/admin/safety-inspections/"
    "objects/search"
)
@login_required
@safety_inspection_manage_required
def object_search() -> Any:
    search_text = " ".join(
        request.args.get("q", "").split()
    )

    if len(search_text) < 2:
        return jsonify({"items": []})

    pattern = f"%{search_text}%"
    results = []

    economic_units = (
        WowiEconomicUnit.query.filter(
            WowiEconomicUnit.active.is_(True),
            or_(
                WowiEconomicUnit.id_num.ilike(
                    pattern
                ),
                WowiEconomicUnit.name.ilike(
                    pattern
                ),
                WowiEconomicUnit.location.ilike(
                    pattern
                ),
            ),
        )
        .order_by(
            WowiEconomicUnit.id_num.asc()
        )
        .limit(20)
        .all()
    )

    for item in economic_units:
        results.append(
            _object_result(
                SafetyInspectionObjectLevel
                .ECONOMIC_UNIT.value,
                item.erp_id,
                "Wirtschaftseinheit",
                " · ".join(
                    value
                    for value in (
                        item.id_num,
                        item.name,
                        item.location,
                    )
                    if value
                ),
            )
        )

    buildings = (
        WowiBuilding.query.filter(
            WowiBuilding.active.is_(True),
            or_(
                WowiBuilding.id_num.ilike(
                    pattern
                ),
                WowiBuilding.address.ilike(
                    pattern
                ),
            ),
            WowiBuilding.use_units.any(
                WowiUseUnit.active.is_(True)
            ),
        )
        .order_by(
            WowiBuilding.id_num.asc()
        )
        .limit(20)
        .all()
    )

    for item in buildings:
        results.append(
            _object_result(
                SafetyInspectionObjectLevel
                .BUILDING.value,
                item.erp_id,
                "Gebäude",
                " · ".join(
                    value
                    for value in (
                        item.id_num,
                        item.address,
                    )
                    if value
                ),
            )
        )

    use_units = (
        WowiUseUnit.query.filter(
            WowiUseUnit.active.is_(True),
            or_(
                WowiUseUnit.id_num.ilike(
                    pattern
                ),
                WowiUseUnit.address.ilike(
                    pattern
                ),
                WowiUseUnit
                .description_of_position
                .ilike(pattern),
            ),
            WowiUseUnit.building.has(
                WowiBuilding.active.is_(True)
            ),
        )
        .order_by(
            WowiUseUnit.id_num.asc()
        )
        .limit(20)
        .all()
    )

    for item in use_units:
        results.append(
            _object_result(
                SafetyInspectionObjectLevel
                .USE_UNIT.value,
                item.erp_id,
                "Nutzungseinheit",
                " · ".join(
                    value
                    for value in (
                        item.id_num,
                        item.address,
                        item.description_of_position,
                    )
                    if value
                ),
            )
        )

    return jsonify(
        {"items": results[:50]}
    )


def _selected_template_version(
    template: SafetyInspectionTemplate,
) -> SafetyInspectionTemplateVersion | None:
    raw_version_id = request.values.get(
        "version_id",
        "",
    ).strip()

    if raw_version_id.isdigit():
        version = db.session.get(
            SafetyInspectionTemplateVersion,
            int(raw_version_id),
        )

        if (
            version is not None
            and version.template_id
            == template.id
        ):
            return version

    return max(
        template.versions,
        key=lambda value: (
            value.status
            == (
                SafetyInspectionTemplateVersionStatus
                .DRAFT.value
            ),
            value.version_number,
        ),
        default=None,
    )


def _replace_template_items(
    version: SafetyInspectionTemplateVersion,
) -> None:
    try:
        payload = json.loads(
            request.form.get(
                "items_json",
                "[]",
            )
        )
    except json.JSONDecodeError as exc:
        raise ValueError(
            "Die Prüfpunkte konnten nicht "
            "gelesen werden."
        ) from exc

    if not isinstance(payload, list):
        raise ValueError(
            "Die Prüfpunkte sind ungültig."
        )

    version.items.clear()
    db.session.flush()

    for position_index, row in enumerate(
        payload
    ):
        if not isinstance(row, dict):
            raise ValueError(
                "Ein Prüfpunkt ist ungültig."
            )

        title = str(
            row.get("title") or ""
        ).strip()

        if not title:
            raise ValueError(
                "Jeder Prüfpunkt benötigt "
                "eine Bezeichnung."
            )

        stable_key = str(
            row.get("stable_key")
            or uuid4()
        ).strip()

        version.items.append(
            SafetyInspectionTemplateItem(
                stable_key=stable_key,
                position_index=(
                    position_index
                ),
                title=title,
                description=_clean_text(
                    row.get("description")
                ),
                help_text=_clean_text(
                    row.get("help_text")
                ),
                required=bool(
                    row.get("required")
                ),
                allow_not_present=bool(
                    row.get(
                        "allow_not_present"
                    )
                ),
                photo_required_no_defect=bool(
                    row.get(
                        "photo_required_no_defect"
                    )
                ),
                photo_required_not_present=bool(
                    row.get(
                        "photo_required_not_present"
                    )
                ),
                photo_required_defect_info=bool(
                    row.get(
                        "photo_required_defect_info"
                    )
                ),
                photo_required_defect_urgent=bool(
                    row.get(
                        "photo_required_defect_urgent"
                    )
                ),
            )
        )


def _save_schedule(
    item: SafetyInspectionSchedule,
) -> None:
    item.name = request.form.get(
        "name",
        "",
    ).strip()

    if not item.name:
        raise ValueError(
            "Der Prüfzeitplan benötigt "
            "einen Namen."
        )

    item.frequency = request.form.get(
        "frequency",
        SafetyInspectionScheduleFrequency
        .MONTHLY.value,
    )
    item.interval_value = _form_int(
        "interval_value",
        1,
    )
    item.generation_mode = request.form.get(
        "generation_mode",
        SafetyInspectionGenerationMode
        .PREVIOUS_MONTH_DAY.value,
    )
    item.generation_lead_days = _form_int(
        "generation_lead_days",
        7,
    )
    item.generation_day_of_month = (
        _form_optional_int(
            "generation_day_of_month"
        )
    )
    item.due_start_offset_days = _form_int(
        "due_start_offset_days",
        0,
    )
    item.due_end_offset_days = _form_int(
        "due_end_offset_days",
        0,
    )
    item.warning_days = _form_int(
        "warning_days",
        7,
    )
    item.grace_days = _form_int(
        "grace_days",
        0,
    )
    item.active = (
        "active" in request.form
    )

    validate_safety_inspection_schedule(
        item
    )


def _save_group(
    item: SafetyInspectionGroup,
) -> None:
    item.name = request.form.get(
        "name",
        "",
    ).strip()

    if not item.name:
        raise ValueError(
            "Die Prüfgruppe benötigt "
            "einen Namen."
        )

    item.description = _clean_text(
        request.form.get("description")
    )
    item.template = _required_model(
        SafetyInspectionTemplate,
        "template_id",
        "Prüfdetailplan",
    )
    item.schedule = _required_model(
        SafetyInspectionSchedule,
        "schedule_id",
        "Prüfzeitplan",
    )
    department_id = _form_optional_int("department_id")
    item.department = (
        _required_model(WowiDepartment, "department_id", "Abteilung")
        if department_id is not None
        else None
    )

    if request.form.get("corrective_orders_enabled") == "1":
        item.corrective_craft_activity = _required_model(
            CraftActivity,
            "corrective_craft_activity_id",
            "Gewerk",
        )
        item.corrective_task_template = _required_model(
            TimeTaskTemplate,
            "corrective_task_template_id",
            "Standard-Leistungsposition",
        )
        commission_type_erp_id = _required_form_int(
            "corrective_commission_type_erp_id",
            "Auftragsart",
        )
        commission_type = WowiCommissionType.query.filter_by(
            erp_id=commission_type_erp_id,
            active=True,
        ).one_or_none()
        if commission_type is None:
            raise ValueError("Die Auftragsart ist nicht mehr verfügbar.")
        item.corrective_commission_type_erp_id = commission_type.erp_id
    else:
        item.corrective_commission_type_erp_id = None
        item.corrective_craft_activity = None
        item.corrective_task_template = None
    item.object_level = request.form.get(
        "object_level",
        "",
    )
    selection_mode = request.form.get(
        "selection_mode",
        "",
    ).strip()

    valid_selection_modes = {
        mode.value
        for mode in (
            SafetyInspectionSelectionMode
        )
    }

    if (
        selection_mode
        not in valid_selection_modes
    ):
        raise ValueError(
            "Der Auswahlmodus ist ungültig."
        )

    item.selection_mode = selection_mode

    item.id_num_regex = _clean_text(
        request.form.get(
            "id_num_regex"
        )
    )

    if item.id_num_regex is None:
        item.id_num_regex_action = None
    else:
        item.id_num_regex_action = (
            request.form.get(
                "id_num_regex_action",
                "",
            ).strip()
        )

    fallback_user_id = _form_optional_int(
        "fallback_user_id"
    )

    if fallback_user_id is None:
        item.fallback_user = None
    else:
        fallback_user = db.session.get(
            User,
            fallback_user_id,
        )

        if (
            fallback_user is None
            or not fallback_user.is_active
            or fallback_user
            .craftsman_erp_id is None
        ):
            raise ValueError("Der ausgewählte Handwerker ist nicht verfügbar.")

        item.fallback_user = fallback_user

    item.first_period_start = _form_date(
        "first_period_start",
        "Beginn",
    )
    item.ends_on = _form_optional_date(
        "ends_on"
    )
    item.include_vacancies = (
        "include_vacancies"
        in request.form
    )
    item.automatic_archive = (
        "automatic_archive"
        in request.form
    )
    item.archive_original_photos = (
        "archive_original_photos"
        in request.form
    )
    item.active = (
        "active" in request.form
    )

    _replace_group_targets(item)


def _group_preview_from_request(
) -> SafetyInspectionGroup:
    object_level = request.form.get(
        "object_level",
        "",
    ).strip()
    valid_object_levels = {
        level.value
        for level in SafetyInspectionObjectLevel
    }

    if object_level not in valid_object_levels:
        raise ValueError(
            "Die Prüfebene ist ungültig."
        )

    selection_mode = request.form.get(
        "selection_mode",
        "",
    ).strip()
    valid_selection_modes = {
        mode.value
        for mode in (
            SafetyInspectionSelectionMode
        )
    }

    if (
        selection_mode
        not in valid_selection_modes
    ):
        raise ValueError(
            "Der Auswahlmodus ist ungültig."
        )

    regex_pattern = _clean_text(
        request.form.get("id_num_regex")
    )
    regex_action = request.form.get(
        "id_num_regex_action",
        "",
    ).strip()

    if regex_pattern is None:
        regex_action = None
    else:
        valid_regex_actions = {
            action.value
            for action in (
                SafetyInspectionRegexAction
            )
        }

        if (
            regex_action
            not in valid_regex_actions
        ):
            raise ValueError(
                "Die RegEx-Aktion ist ungültig."
            )

        if len(regex_pattern) > 500:
            raise ValueError(
                "Der reguläre Ausdruck darf "
                "höchstens 500 Zeichen lang sein."
            )

        try:
            re.compile(
                regex_pattern,
                re.IGNORECASE,
            )
        except re.error as exc:
            raise ValueError(
                "Der reguläre Ausdruck ist "
                f"ungültig: {exc}."
            ) from exc

    preview_group = SafetyInspectionGroup(
        object_level=object_level,
        selection_mode=selection_mode,
        id_num_regex=regex_pattern,
        id_num_regex_action=regex_action,
        include_vacancies=(
            "include_vacancies"
            in request.form
        ),
    )
    preview_group.targets.extend(
        _parse_group_targets()
    )

    return preview_group


def _replace_group_targets(
    group: SafetyInspectionGroup,
) -> None:
    targets = _parse_group_targets()

    group.targets.clear()

    if group.id is not None:
        db.session.flush()

    group.targets.extend(targets)


def _parse_group_targets(
) -> list[SafetyInspectionGroupTarget]:
    try:
        payload = json.loads(
            request.form.get(
                "targets_json",
                "[]",
            )
        )
    except json.JSONDecodeError as exc:
        raise ValueError(
            "Die Objektauswahl konnte nicht "
            "gelesen werden."
        ) from exc

    if not isinstance(payload, list):
        raise ValueError(
            "Die Objektauswahl ist ungültig."
        )

    targets = []
    seen = set()

    for row in payload:
        if not isinstance(row, dict):
            raise ValueError(
                "Ein ausgewähltes Objekt ist "
                "ungültig."
            )

        object_type = str(
            row.get("object_type") or ""
        ).strip()
        object_erp_id = _as_positive_int(
            row.get("object_erp_id"),
            "Objekt-ID",
        )
        key = (
            object_type,
            object_erp_id,
        )

        if key in seen:
            continue

        label = _validate_target_object(
            object_type,
            object_erp_id,
        )
        seen.add(key)

        targets.append(
            SafetyInspectionGroupTarget(
                object_type=object_type,
                object_erp_id=object_erp_id,
                object_label=label,
                included=bool(
                    row.get("included", True)
                ),
            )
        )

    return targets


def _validate_target_object(
    object_type: str,
    object_erp_id: int,
) -> str:
    if object_type == (
        SafetyInspectionObjectLevel
        .ECONOMIC_UNIT.value
    ):
        item = (
            WowiEconomicUnit.query
            .filter_by(
                erp_id=object_erp_id,
                active=True,
            )
            .one_or_none()
        )
        type_label = "Wirtschaftseinheit"
        values = (
            item.id_num if item else None,
            item.name if item else None,
            item.location if item else None,
        )
    elif object_type == (
        SafetyInspectionObjectLevel
        .BUILDING.value
    ):
        item = (
            WowiBuilding.query.filter_by(
                erp_id=object_erp_id,
                active=True,
            ).one_or_none()
        )
        type_label = "Gebäude"
        values = (
            item.id_num if item else None,
            item.address if item else None,
        )
    elif object_type == (
        SafetyInspectionObjectLevel
        .USE_UNIT.value
    ):
        item = (
            WowiUseUnit.query.filter_by(
                erp_id=object_erp_id,
                active=True,
            ).one_or_none()
        )
        type_label = "Nutzungseinheit"
        values = (
            item.id_num if item else None,
            item.address if item else None,
            (
                item.description_of_position
                if item
                else None
            ),
        )
    else:
        raise ValueError(
            "Die ausgewählte Objektart ist "
            "ungültig."
        )

    if item is None:
        raise ValueError(
            "Ein ausgewähltes Objekt ist "
            "nicht mehr verfügbar."
        )

    return (
        f"{type_label}: "
        + " · ".join(
            value
            for value in values
            if value
        )
    )


def _render_group_form(
    item: SafetyInspectionGroup,
) -> Any:
    target_rows = [
        {
            "object_type": (
                target.object_type
            ),
            "object_erp_id": (
                target.object_erp_id
            ),
            "object_label": (
                target.object_label or ""
            ),
            "included": target.included,
        }
        for target in item.targets
    ]

    templates = (
        SafetyInspectionTemplate.query.filter(
            SafetyInspectionTemplate
            .active.is_(True),
            SafetyInspectionTemplate
            .versions.any(
                SafetyInspectionTemplateVersion
                .status
                == (
                    SafetyInspectionTemplateVersionStatus
                    .PUBLISHED.value
                )
            ),
        )
        .order_by(
            SafetyInspectionTemplate.name.asc()
        )
        .all()
    )
    schedules = (
        SafetyInspectionSchedule.query
        .filter_by(active=True)
        .order_by(
            SafetyInspectionSchedule.name.asc()
        )
        .all()
    )
    departments = (
        WowiDepartment.query
        .filter_by(active=True)
        .order_by(
            WowiDepartment.name.asc()
        )
        .all()
    )
    fallback_users = (
        User.query.filter(
            User.is_active_flag.is_(True),
            User.craftsman_erp_id.isnot(None),
        )
        .order_by(User.display_name.asc())
        .all()
    )
    commission_types = (
        WowiCommissionType.query
        .filter_by(active=True)
        .order_by(
            WowiCommissionType.code.asc()
        )
        .all()
    )
    craft_activities = (
        CraftActivity.query
        .filter_by(
            active=True,
            app_enabled=True,
        )
        .order_by(CraftActivity.name.asc())
        .all()
    )
    task_templates = (
        TimeTaskTemplate.query.filter(
            TimeTaskTemplate.active.is_(True),
            TimeTaskTemplate
            .erp_service_catalogue_id
            .isnot(None),
        )
        .order_by(
            TimeTaskTemplate.title.asc()
        )
        .all()
    )

    return render_template(
        "admin/safety_inspection_group_form.html",
        item=item,
        target_rows=target_rows,
        templates=templates,
        schedules=schedules,
        departments=departments,
        fallback_users=fallback_users,
        commission_types=commission_types,
        craft_activities=craft_activities,
        task_templates=task_templates,
        object_level_labels=(
            _object_level_labels()
        ),
        selection_mode_labels=(
            _selection_mode_labels()
        ),
        regex_action_labels=(
            _regex_action_labels()
        ),
    )


def _required_model(
    model: Any,
    field_name: str,
    label: str,
) -> Any:
    model_id = _required_form_int(
        field_name,
        label,
    )
    item = db.session.get(
        model,
        model_id,
    )

    if item is None:
        raise ValueError(
            f"{label} ist nicht mehr "
            "verfügbar."
        )

    return item


def _required_form_int(
    field_name: str,
    label: str,
) -> int:
    value = request.form.get(
        field_name,
        "",
    ).strip()

    if (
        not value.isdigit()
        or int(value) < 1
    ):
        raise ValueError(
            f"{label} muss ausgewählt werden."
        )

    return int(value)


def _form_int(
    field_name: str,
    default: int,
) -> int:
    value = request.form.get(
        field_name,
        "",
    ).strip()

    if not value:
        return default

    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(
            "Ein Zahlenfeld enthält einen "
            "ungültigen Wert."
        ) from exc


def _form_optional_int(
    field_name: str,
) -> int | None:
    value = request.form.get(
        field_name,
        "",
    ).strip()

    if not value:
        return None

    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(
            "Ein Zahlenfeld enthält einen "
            "ungültigen Wert."
        ) from exc


def _form_date(
    field_name: str,
    label: str,
) -> date:
    value = request.form.get(
        field_name,
        "",
    ).strip()

    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"{label} enthält kein "
            "gültiges Datum."
        ) from exc


def _form_optional_date(
    field_name: str,
) -> date | None:
    value = request.form.get(
        field_name,
        "",
    ).strip()

    if not value:
        return None

    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            "Das Enddatum ist ungültig."
        ) from exc


def _as_positive_int(
    value: Any,
    label: str,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{label} ist ungültig."
        ) from exc

    if parsed < 1:
        raise ValueError(
            f"{label} ist ungültig."
        )

    return parsed


def _object_result(
    object_type: str,
    object_erp_id: int,
    type_label: str,
    label: str,
) -> dict[str, Any]:
    return {
        "object_type": object_type,
        "object_erp_id": object_erp_id,
        "type_label": type_label,
        "label": label,
        "display_label": (
            f"{type_label}: {label}"
        ),
    }


def _clean_text(
    value: Any,
) -> str | None:
    normalized = str(
        value or ""
    ).strip()
    return normalized or None


def _write_error(
    exc: Exception,
) -> str:
    if isinstance(exc, IntegrityError):
        return (
            "Der Name oder eine Zuordnung "
            "wird bereits verwendet."
        )

    return str(exc)


def _frequency_labels() -> dict[str, str]:
    return {
        SafetyInspectionScheduleFrequency
        .DAILY.value: "Täglich",
        SafetyInspectionScheduleFrequency
        .WEEKLY.value: "Wöchentlich",
        SafetyInspectionScheduleFrequency
        .MONTHLY.value: "Monatlich",
        SafetyInspectionScheduleFrequency
        .YEARLY.value: "Jährlich",
    }


def _generation_mode_labels() -> dict[str, str]:
    return {
        SafetyInspectionGenerationMode
        .LEAD_DAYS.value: (
            "Vorlauf in Tagen"
        ),
        SafetyInspectionGenerationMode
        .PREVIOUS_MONTH_DAY.value: (
            "Fester Tag des Vormonats"
        ),
    }


def _object_level_labels() -> dict[str, str]:
    return {
        SafetyInspectionObjectLevel
        .ECONOMIC_UNIT.value: (
            "Wirtschaftseinheit"
        ),
        SafetyInspectionObjectLevel
        .BUILDING.value: "Gebäude",
        SafetyInspectionObjectLevel
        .USE_UNIT.value: (
            "Nutzungseinheit"
        ),
    }


def _selection_mode_labels() -> dict[str, str]:
    return {
        SafetyInspectionSelectionMode
        .WHITELIST.value: "Whitelist",
        SafetyInspectionSelectionMode
        .BLACKLIST.value: "Blacklist",
    }


def _regex_action_labels() -> dict[str, str]:
    return {
        SafetyInspectionRegexAction
        .INCLUDE.value: (
            "Passende IdNums einschließen"
        ),
        SafetyInspectionRegexAction
        .EXCLUDE.value: (
            "Passende IdNums ausschließen"
        ),
    }
