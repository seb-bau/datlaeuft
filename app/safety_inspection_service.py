from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable
from uuid import uuid4
import re

from .commission_service import (
    create_pending_commission,
)
from .extensions import db
from .models import (
    Commission,
    CommissionLocalStatus,
    CommissionPhoto,
    CommissionSource,
    CommissionSyncState,
    CraftActivityDepartmentMapping,
    SafetyInspectionAssignmentSource,
    SafetyInspectionDefect,
    SafetyInspectionDefectCommission,
    SafetyInspectionDefectStatus,
    SafetyInspectionGenerationMode,
    SafetyInspectionGroup,
    SafetyInspectionGroupTarget,
    SafetyInspectionJob,
    SafetyInspectionJobStatus,
    SafetyInspectionObjectLevel,
    SafetyInspectionResult,
    SafetyInspectionResultState,
    SafetyInspectionSchedule,
    SafetyInspectionScheduleFrequency,
    SafetyInspectionTemplate,
    SafetyInspectionTemplateItem,
    SafetyInspectionTemplateVersion,
    SafetyInspectionTemplateVersionStatus,
    User,
    WowiBuilding,
    WowiEconomicUnit,
    WowiDepartment,
    WowiUseUnit,
    utc_now,
    PushEventType,
    SafetyInspectionSelectionMode,
    SafetyInspectionRegexAction,
)
from .object_directory import (
    resolve_department_assignment,
)
from .photo_service import (
    commission_photo_path,
    store_commission_photo_bytes,
)
from .push_service import (
    enqueue_safety_inspection_push,
    enqueue_urgent_safety_defect_push,
)


@dataclass(frozen=True)
class SafetyInspectionObjectContext:
    object_level: str
    object_erp_id: int
    economic_unit: WowiEconomicUnit
    building: WowiBuilding | None
    use_unit: WowiUseUnit | None
    address: str | None
    location: str | None
    latitude: float | None
    longitude: float | None
    object_id_num: str | None


def create_safety_inspection_template(
        name: str,
        description: str | None,
        legal_reference: str | None,
        user: User,
) -> tuple[
    SafetyInspectionTemplate,
    SafetyInspectionTemplateVersion,
]:
    normalized_name = name.strip()

    if not normalized_name:
        raise ValueError(
            "Der Prüfdetailplan benötigt "
            "einen Namen."
        )

    template = SafetyInspectionTemplate(
        name=normalized_name,
        description=clean_text(description),
        legal_reference=clean_text(
            legal_reference
        ),
        created_by=user,
    )
    version = (
        SafetyInspectionTemplateVersion(
            version_number=1,
            status=(
                SafetyInspectionTemplateVersionStatus
                .DRAFT.value
            ),
            created_by=user,
        )
    )
    template.versions.append(version)
    db.session.add(template)
    db.session.flush()

    return template, version


def create_safety_inspection_revision(
        template: SafetyInspectionTemplate,
        user: User,
) -> SafetyInspectionTemplateVersion:
    existing_draft = next(
        (
            version
            for version in template.versions
            if version.status
               == (
                   SafetyInspectionTemplateVersionStatus
                   .DRAFT.value
               )
        ),
        None,
    )

    if existing_draft is not None:
        return existing_draft

    latest_version = max(
        template.versions,
        key=lambda item: item.version_number,
        default=None,
    )
    next_version_number = (
        latest_version.version_number + 1
        if latest_version is not None
        else 1
    )
    version = (
        SafetyInspectionTemplateVersion(
            template=template,
            version_number=(
                next_version_number
            ),
            status=(
                SafetyInspectionTemplateVersionStatus
                .DRAFT.value
            ),
            created_by=user,
        )
    )

    if latest_version is not None:
        for item in latest_version.items:
            version.items.append(
                SafetyInspectionTemplateItem(
                    stable_key=item.stable_key,
                    position_index=(
                        item.position_index
                    ),
                    title=item.title,
                    description=item.description,
                    help_text=item.help_text,
                    required=item.required,
                    allow_not_present=(
                        item.allow_not_present
                    ),
                    photo_required_no_defect=(
                        item
                        .photo_required_no_defect
                    ),
                    photo_required_not_present=(
                        item
                        .photo_required_not_present
                    ),
                    photo_required_defect_info=(
                        item
                        .photo_required_defect_info
                    ),
                    photo_required_defect_urgent=(
                        item
                        .photo_required_defect_urgent
                    ),
                )
            )

    db.session.add(version)
    db.session.flush()
    return version


def publish_safety_inspection_version(
        version: SafetyInspectionTemplateVersion,
        user: User,
        change_note: str | None = None,
) -> SafetyInspectionTemplateVersion:
    if version.status != (
            SafetyInspectionTemplateVersionStatus
                    .DRAFT.value
    ):
        raise ValueError(
            "Nur ein Entwurf kann "
            "veröffentlicht werden."
        )

    if not version.items:
        raise ValueError(
            "Der Prüfdetailplan benötigt "
            "mindestens einen Prüfpunkt."
        )

    positions = [
        item.position_index
        for item in version.items
    ]

    if len(positions) != len(set(positions)):
        raise ValueError(
            "Die Reihenfolge der Prüfpunkte "
            "ist nicht eindeutig."
        )

    if any(
            not item.title.strip()
            for item in version.items
    ):
        raise ValueError(
            "Jeder Prüfpunkt benötigt "
            "eine Bezeichnung."
        )

    for previous_version in (
            version.template.versions
    ):
        if (
                previous_version.id != version.id
                and previous_version.status
                == (
                SafetyInspectionTemplateVersionStatus
                        .PUBLISHED.value
        )
        ):
            previous_version.status = (
                SafetyInspectionTemplateVersionStatus
                .ARCHIVED.value
            )

    version.status = (
        SafetyInspectionTemplateVersionStatus
        .PUBLISHED.value
    )
    version.change_note = clean_text(
        change_note
    )
    version.published_by = user
    version.published_at = utc_now()
    return version


def validate_safety_inspection_schedule(
        schedule: SafetyInspectionSchedule,
) -> None:
    frequencies = {
        item.value
        for item in (
            SafetyInspectionScheduleFrequency
        )
    }
    generation_modes = {
        item.value
        for item in (
            SafetyInspectionGenerationMode
        )
    }

    if schedule.frequency not in frequencies:
        raise ValueError(
            "Der Turnus ist ungültig."
        )

    if schedule.interval_value < 1:
        raise ValueError(
            "Das Turnusintervall muss "
            "mindestens 1 betragen."
        )

    if (
            schedule.generation_mode
            not in generation_modes
    ):
        raise ValueError(
            "Der Erzeugungsmodus ist ungültig."
        )

    if schedule.generation_lead_days < 0:
        raise ValueError(
            "Der Vorlauf darf nicht "
            "negativ sein."
        )

    if schedule.warning_days < 0:
        raise ValueError(
            "Die Vorwarnzeit darf nicht "
            "negativ sein."
        )

    if schedule.grace_days < 0:
        raise ValueError(
            "Die Nachfrist darf nicht "
            "negativ sein."
        )

    if (
            schedule.generation_mode
            == (
            SafetyInspectionGenerationMode
                    .PREVIOUS_MONTH_DAY.value
    )
    ):
        if schedule.frequency != (
                SafetyInspectionScheduleFrequency
                        .MONTHLY.value
        ):
            raise ValueError(
                "Die Erzeugung an einem Tag "
                "des Vormonats ist nur bei "
                "monatlichen Zeitplänen möglich."
            )

        if (
                schedule.generation_day_of_month
                is None
                or not 1
                       <= schedule.generation_day_of_month
                       <= 31
        ):
            raise ValueError(
                "Der Erzeugungstag muss "
                "zwischen 1 und 31 liegen."
            )


def validate_safety_inspection_group(
        group: SafetyInspectionGroup,
) -> None:
    validate_safety_inspection_schedule(
        group.schedule
    )
    if not group.schedule.active:
        raise ValueError(
            "Der Prüfzeitplan ist deaktiviert."
        )

    if not group.template.active:
        raise ValueError(
            "Der Prüfdetailplan ist deaktiviert."
        )

    if (
            latest_published_version(
                group.template
            )
            is None
    ):
        raise ValueError(
            "Der Prüfdetailplan besitzt keine "
            "veröffentlichte Version."
        )

    levels = {
        item.value
        for item in SafetyInspectionObjectLevel
    }

    if group.object_level not in levels:
        raise ValueError(
            "Die Objektebene ist ungültig."
        )

    if (
            group.ends_on is not None
            and group.ends_on
            < group.first_period_start
    ):
        raise ValueError(
            "Das Enddatum darf nicht vor "
            "dem Beginn liegen."
        )

    if group.department is None:
        _resolve_safety_inspection_assignment(
            None,
            None,
            group.fallback_user,
        )
    elif not group.department.active:
        raise ValueError("Die ausgewählte Abteilung ist nicht aktiv.")

    if group.fallback_user is not None and (
            not group.fallback_user.is_active
            or group.fallback_user.craftsman_erp_id is None
    ):
        raise ValueError("Der ausgewählte Handwerker ist nicht verfügbar.")

    corrective_values = (
        group.corrective_commission_type_erp_id,
        group.corrective_craft_activity_id,
        group.corrective_task_template_id,
    )
    if any(value is not None for value in corrective_values):
        if not group.corrective_orders_enabled:
            raise ValueError(
                "Bitte die Vorgabe für Mängelaufträge vollständig ausfüllen."
            )

        task_template = group.corrective_task_template
        craft_activity = group.corrective_craft_activity

        if not craft_activity.active or not craft_activity.app_enabled:
            raise ValueError(
                "Das ausgewählte Gewerk ist global deaktiviert."
            )

        if (
                not task_template.active
                or task_template.erp_service_catalogue_id is None
        ):
            raise ValueError(
                "Die Standard-Leistungsposition ist nicht verfügbar."
            )

        if task_template.craft_activity_id != craft_activity.id:
            raise ValueError(
                "Leistungsposition und Gewerk gehören nicht zusammen."
            )

        mapping = CraftActivityDepartmentMapping.query.filter_by(
            craft_activity_id=craft_activity.id,
        ).one_or_none()

        if (
                mapping is None
                or not mapping.department.active
                or (
                group.department is not None
                and mapping.department_id != group.department_id
        )
        ):
            raise ValueError(
                "Das Gewerk benötigt eine aktive Abteilungszuordnung, "
                "die zu einer gewählten Prüfgruppen-Abteilung passen muss."
            )

    selection_modes = {
        mode.value
        for mode in (
            SafetyInspectionSelectionMode
        )
    }

    if (
            group.selection_mode
            not in selection_modes
    ):
        raise ValueError(
            "Der Auswahlmodus der Prüfgruppe "
            "ist ungültig."
        )

    regex_pattern = clean_text(
        group.id_num_regex
    )
    regex_actions = {
        action.value
        for action in SafetyInspectionRegexAction
    }

    if regex_pattern is None:
        if group.id_num_regex_action is not None:
            raise ValueError(
                "Für die RegEx-Aktion fehlt "
                "ein regulärer Ausdruck."
            )
    else:
        if len(regex_pattern) > 500:
            raise ValueError(
                "Der reguläre Ausdruck darf "
                "höchstens 500 Zeichen lang sein."
            )

        if (
                group.id_num_regex_action
                not in regex_actions
        ):
            raise ValueError(
                "Die RegEx-Aktion ist ungültig."
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

    has_regex_include = (
            regex_pattern is not None
            and group.id_num_regex_action
            == (
                SafetyInspectionRegexAction
                .INCLUDE.value
            )
    )

    if (
            group.selection_mode
            == (
            SafetyInspectionSelectionMode
                    .WHITELIST.value
            )
            and not has_regex_include
            and not any(
                target.included
                for target in group.targets
            )
    ):
        raise ValueError(
            "Eine Prüfgruppe im "
            "Whitelist-Modus benötigt "
            "mindestens ein eingeschlossenes "
            "Objekt oder eine einschließende "
            "RegEx."
        )


def generate_safety_inspection_jobs(
    run_date: date | None = None,
    group_id: int | None = None,
) -> dict[str, int]:
    current_date = run_date or date.today()
    result = {
        "groups": 0,
        "created": 0,
        "existing": 0,
        "unassigned": 0,
        "missed": mark_missed_inspections(
            current_date
        ),
        "errors": 0,
    }

    query = SafetyInspectionGroup.query.filter(
        SafetyInspectionGroup.active.is_(
            True
        )
    )

    if group_id is not None:
        query = query.filter(
            SafetyInspectionGroup.id
            == group_id
        )

    groups = query.order_by(
        SafetyInspectionGroup.id.asc()
    ).all()

    for group in groups:
        result["groups"] += 1
        group_result = {
            "created": 0,
            "existing": 0,
            "unassigned": 0,
        }

        try:
            with db.session.begin_nested():
                validate_safety_inspection_group(
                    group
                )
                template_version = (
                    latest_published_version(
                        group.template
                    )
                )
                contexts = (
                    resolve_group_object_contexts(
                        group
                    )
                )

                if not contexts:
                    raise ValueError(
                        "Für die Prüfgruppe wurden "
                        "keine aktiven Objekte gefunden."
                    )

                period_start = (
                    group.first_period_start
                )
                iteration_count = 0

                while iteration_count < 10000:
                    iteration_count += 1

                    if (
                        group.ends_on is not None
                        and period_start
                        > group.ends_on
                    ):
                        break

                    period_end = (
                        next_period_start(
                            period_start,
                            group.schedule,
                        )
                        - timedelta(days=1)
                    )
                    generation_date = (
                        inspection_generation_date(
                            period_start,
                            group.schedule,
                        )
                    )

                    if (
                        generation_date
                        > current_date
                    ):
                        break

                    due_from = (
                        period_start
                        + timedelta(
                            days=(
                                group.schedule
                                .due_start_offset_days
                            )
                        )
                    )
                    due_to = (
                        period_end
                        + timedelta(
                            days=(
                                group.schedule
                                .due_end_offset_days
                            )
                        )
                    )
                    missed_on = (
                        due_to
                        + timedelta(
                            days=(
                                group.schedule
                                .grace_days
                                + 1
                            )
                        )
                    )

                    if missed_on >= current_date:
                        for context in contexts:
                            job, created = (
                                create_safety_inspection_job(
                                    group=group,
                                    template_version=(
                                        template_version
                                    ),
                                    context=context,
                                    period_start=(
                                        period_start
                                    ),
                                    period_end=(
                                        period_end
                                    ),
                                    due_from=due_from,
                                    due_to=due_to,
                                    missed_on=missed_on,
                                )
                            )

                            if created:
                                group_result[
                                    "created"
                                ] += 1

                                if (
                                    job.assigned_user_id
                                    is None
                                ):
                                    group_result[
                                        "unassigned"
                                    ] += 1
                            else:
                                group_result[
                                    "existing"
                                ] += 1

                    period_start = (
                        next_period_start(
                            period_start,
                            group.schedule,
                        )
                    )

                if iteration_count >= 10000:
                    raise ValueError(
                        "Der Zeitplan erzeugt zu "
                        "viele Prüfperioden."
                    )

                group.last_generation_at = (
                    utc_now()
                )
                group.last_generation_error = None
                db.session.flush()

        except Exception as exc:
            group.last_generation_at = (
                utc_now()
            )
            group.last_generation_error = (
                str(exc)
            )
            result["errors"] += 1
            db.session.flush()

        else:
            result["created"] += (
                group_result["created"]
            )
            result["existing"] += (
                group_result["existing"]
            )
            result["unassigned"] += (
                group_result["unassigned"]
            )

    return result


def create_safety_inspection_job(
        group: SafetyInspectionGroup,
        template_version: (
                SafetyInspectionTemplateVersion
        ),
        context: SafetyInspectionObjectContext,
        period_start: date,
        period_end: date,
        due_from: date,
        due_to: date,
        missed_on: date,
) -> tuple[SafetyInspectionJob, bool]:
    template = db.session.get(
        SafetyInspectionTemplate,
        template_version.template_id,
        with_for_update=True,
        populate_existing=True,
    )
    if template is None:
        raise ValueError("Der Prüfdetailplan existiert nicht mehr.")
    existing = (
        SafetyInspectionJob.query.filter_by(
            group_id=group.id,
            object_level=(
                context.object_level
            ),
            object_erp_id=(
                context.object_erp_id
            ),
            period_start=period_start,
        ).one_or_none()
    )

    if existing is not None:
        return existing, False

    template_items = (
        SafetyInspectionTemplateItem.query
        .filter(
            SafetyInspectionTemplateItem
            .template_version_id
            == template_version.id
        )
        .order_by(
            SafetyInspectionTemplateItem
            .position_index.asc(),
            SafetyInspectionTemplateItem.id.asc(),
        )
        .all()
    )

    if not template_items:
        raise ValueError(
            "Der veröffentlichte "
            "Prüfdetailplan enthält "
            "keine Prüfpunkte."
        )

    assigned_user = None
    jurisdiction = None
    responsible_official_erp_id = None
    assignment_error = None
    assignment_source = (
        SafetyInspectionAssignmentSource
        .UNASSIGNED.value
    )

    try:
        (
            assigned_user,
            jurisdiction,
            responsible_official_erp_id,
            assignment_source,
        ) = _resolve_safety_inspection_assignment(
            context.economic_unit,
            group.department,
            group.fallback_user,
        )
    except ValueError as exc:
        assignment_error = str(exc)

    building = context.building
    use_unit = context.use_unit
    job = SafetyInspectionJob(
        group=group,
        template_version=template_version,
        status=(
            SafetyInspectionJobStatus
            .PLANNED.value
        ),
        period_key=inspection_period_key(
            period_start,
            group.schedule,
        ),
        period_start=period_start,
        period_end=period_end,
        due_from=due_from,
        due_to=due_to,
        warning_on=(
                due_to
                - timedelta(
            days=(
                group.schedule
                .warning_days
            )
        )
        ),
        missed_on=missed_on,
        object_level=context.object_level,
        object_erp_id=context.object_erp_id,
        company_code_erp_id=(
            context.economic_unit
            .company_code_erp_id
        ),
        company_code_code=(
            context.economic_unit
            .company_code_code
        ),
        economic_unit_erp_id=(
            context.economic_unit.erp_id
        ),
        economic_unit_idnum=(
            context.economic_unit.id_num
        ),
        economic_unit_name=(
            context.economic_unit.name
        ),
        building_erp_id=(
            building.erp_id
            if building is not None
            else None
        ),
        building_idnum=(
            building.id_num
            if building is not None
            else None
        ),
        use_unit_erp_id=(
            use_unit.erp_id
            if use_unit is not None
            else None
        ),
        use_unit_idnum=(
            use_unit.id_num
            if use_unit is not None
            else None
        ),
        object_address=context.address,
        object_location=context.location,
        latitude=context.latitude,
        longitude=context.longitude,
        department=group.department,
        jurisdiction=jurisdiction,
        assigned_user=assigned_user,
        responsible_official_erp_id=(
            responsible_official_erp_id
        ),
        assignment_source=(
            assignment_source
        ),
        assignment_error=assignment_error,
    )

    db.session.add(job)

    for item in template_items:
        job.results.append(
            SafetyInspectionResult(
                template_item=item,
                item_key=item.stable_key,
                position_index=(
                    item.position_index
                ),
                item_title=item.title,
                item_description=(
                    item.description
                ),
                item_help_text=(
                    item.help_text
                ),
                required=item.required,
                allow_not_present=(
                    item.allow_not_present
                ),
                photo_required_no_defect=(
                    item.photo_required_no_defect
                ),
                photo_required_not_present=(
                    item.photo_required_not_present
                ),
                photo_required_defect_info=(
                    item
                    .photo_required_defect_info
                ),
                photo_required_defect_urgent=(
                    item
                    .photo_required_defect_urgent
                ),
            )
        )

    template.used_for_jobs = True
    db.session.flush()

    if job.assigned_user is not None:
        enqueue_safety_inspection_push(
            job,
            (
                PushEventType
                .NEW_SAFETY_INSPECTION
            ),
        )

    return job, True


def reconcile_safety_inspection_assignments(
) -> dict[str, int]:
    result = {
        "seen": 0,
        "assigned": 0,
        "unresolved": 0,
    }

    jobs = (
        SafetyInspectionJob.query
        .filter(
            SafetyInspectionJob.status
            == (
                SafetyInspectionJobStatus
                .PLANNED.value
            ),
            SafetyInspectionJob.assigned_user_id
            .is_(None),
        )
        .order_by(
            SafetyInspectionJob.id.asc()
        )
        .all()
    )

    for job in jobs:
        result["seen"] += 1

        economic_unit = (
            WowiEconomicUnit.query
            .filter_by(
                erp_id=(
                    job.economic_unit_erp_id
                ),
                active=True,
            )
            .one_or_none()
        )

        if economic_unit is None and job.department is not None:
            job.assignment_source = (
                SafetyInspectionAssignmentSource
                .UNASSIGNED.value
            )
            job.assignment_error = (
                "Die Wirtschaftseinheit wurde "
                "im Objektverzeichnis nicht "
                "gefunden."
            )
            result["unresolved"] += 1
            continue

        try:
            (
                assigned_user,
                jurisdiction,
                responsible_official_erp_id,
                assignment_source,
            ) = _resolve_safety_inspection_assignment(
                economic_unit,
                job.department,
                job.group.fallback_user,
            )
        except ValueError as exc:
            job.assignment_source = (
                SafetyInspectionAssignmentSource.UNASSIGNED.value
            )
            job.assignment_error = str(exc)
            result["unresolved"] += 1
            continue

        job.assigned_user = assigned_user
        job.jurisdiction = jurisdiction
        job.responsible_official_erp_id = responsible_official_erp_id
        job.assignment_source = assignment_source

        job.assignment_error = None
        result["assigned"] += 1

        enqueue_safety_inspection_push(
            job,
            (
                PushEventType
                .CHANGED_SAFETY_INSPECTION
            ),
        )

    return result


def update_safety_inspection_result(
        result: SafetyInspectionResult,
        state: str,
        note: str | None,
        immediate_action: str | None,
        user: User,
        expected_version: int | None = None,
) -> SafetyInspectionResult:
    ensure_job_editable(result.job, user)

    valid_states = {
        item.value
        for item in SafetyInspectionResultState
    }

    if state not in valid_states:
        raise ValueError(
            "Das Prüfergebnis ist ungültig."
        )

    if (
            state
            == (
            SafetyInspectionResultState
                    .NOT_PRESENT.value
    )
            and not result.allow_not_present
    ):
        raise ValueError(
            "Für diesen Prüfpunkt ist "
            "„nicht vorhanden“ nicht zulässig."
        )

    if (
            expected_version is not None
            and result.version
            != expected_version
    ):
        raise ValueError(
            "Der Prüfpunkt wurde "
            "zwischenzeitlich geändert."
        )

    result.state = state
    result.note = clean_text(note)
    result.immediate_action = clean_text(
        immediate_action
    )
    result.version += 1

    if state == (
            SafetyInspectionResultState
                    .NOT_CHECKED.value
    ):
        result.checked_by = None
        result.checked_at = None
    else:
        result.checked_by = user
        result.checked_at = utc_now()

    if result.job.status == (
            SafetyInspectionJobStatus
                    .PLANNED.value
    ):
        result.job.status = (
            SafetyInspectionJobStatus
            .IN_PROGRESS.value
        )
        result.job.started_at = utc_now()

    return result


def complete_safety_inspection_job(
        job: SafetyInspectionJob,
        user: User,
        overall_note: str | None = None,
) -> list[SafetyInspectionDefect]:
    ensure_job_editable(job, user)
    validate_safety_inspection_results(
        job.results
    )

    defects = []

    for result in job.results:
        if result.state not in {
            SafetyInspectionResultState
                    .DEFECT_INFO.value,
            SafetyInspectionResultState
                    .DEFECT_URGENT.value,
        }:
            continue

        defect = result.defect

        if defect is None:
            defect = SafetyInspectionDefect(
                job=job,
                result=result,
                severity=result.state,
                status=(
                    SafetyInspectionDefectStatus
                    .OPEN.value
                ),
                description=(
                        result.note or result.item_title
                ),
                immediate_action=(
                    result.immediate_action
                ),
            )
            db.session.add(defect)

        defects.append(defect)

    job.overall_note = clean_text(
        overall_note
    )
    job.completed_by = user
    job.completed_at = utc_now()
    job.status = (
        SafetyInspectionJobStatus
        .COMPLETED_WITH_DEFECTS.value
        if defects
        else (
            SafetyInspectionJobStatus
            .COMPLETED.value
        )
    )
    db.session.flush()
    from .safety_inspection_document import (
        create_safety_inspection_protocol,
    )

    create_safety_inspection_protocol(
        job,
        user,
    )

    if any(
            result.state
            == (
                    SafetyInspectionResultState
                            .DEFECT_URGENT.value
            )
            for result in job.results
    ):
        enqueue_urgent_safety_defect_push(
            job
        )

    return defects


def _resolve_safety_inspection_assignment(
    economic_unit: WowiEconomicUnit | None,
    department: WowiDepartment | None,
    fallback_user: User | None,
):
    user_available = (
        fallback_user is not None
        and fallback_user.is_active
        and fallback_user.craftsman_erp_id is not None
    )
    if department is None:
        if not user_available:
            raise ValueError(
                "Bitte einen verfügbaren Handwerker fest auswählen."
            )
        return (
            fallback_user,
            None,
            None,
            SafetyInspectionAssignmentSource.DIRECT.value,
        )
    try:
        assignment = resolve_department_assignment(
            economic_unit,
            department,
        )
    except ValueError:
        if not user_available:
            raise
        return (
            fallback_user,
            None,
            None,
            SafetyInspectionAssignmentSource.FALLBACK.value,
        )
    return (
        assignment.user,
        assignment.jurisdiction,
        assignment.responsible_official_erp_id,
        SafetyInspectionAssignmentSource.JURISDICTION.value,
    )


def validate_safety_inspection_results(
        results: Iterable[
            SafetyInspectionResult
        ],
) -> None:
    for result in results:
        if (
                result.required
                and result.state
                == (
                SafetyInspectionResultState
                        .NOT_CHECKED.value
        )
        ):
            raise ValueError(
                f"Der Prüfpunkt "
                f"„{result.item_title}“ "
                "wurde noch nicht bearbeitet."
            )

        if (
                result.state
                == (
                SafetyInspectionResultState
                        .NOT_PRESENT.value
        )
                and not result.note
        ):
            raise ValueError(
                f"Für „{result.item_title}“ "
                "muss begründet werden, warum "
                "der Prüfpunkt nicht vorhanden ist."
            )

        if (
                result.state
                in {
            SafetyInspectionResultState
                    .DEFECT_INFO.value,
            SafetyInspectionResultState
                    .DEFECT_URGENT.value,
        }
                and not result.note
        ):
            raise ValueError(
                f"Der Mangel bei "
                f"„{result.item_title}“ "
                "benötigt eine Beschreibung."
            )

        active_photo_count = sum(
            1
            for photo in result.photos
            if photo.active
        )
        photo_required = (
            result.photo_required_no_defect
            if result.state
               == (
                   SafetyInspectionResultState
                   .NO_DEFECT.value
               )
            else (
                result
                .photo_required_not_present
                if result.state
                   == (
                       SafetyInspectionResultState
                       .NOT_PRESENT.value
                   )
                else (
                    result
                    .photo_required_defect_info
                    if result.state
                       == (
                           SafetyInspectionResultState
                           .DEFECT_INFO.value
                       )
                    else (
                        result
                        .photo_required_defect_urgent
                        if result.state
                           == (
                               SafetyInspectionResultState
                               .DEFECT_URGENT.value
                           )
                        else False
                    )
                )
            )
        )

        if photo_required and active_photo_count == 0:
            raise ValueError(
                f"Der Prüfpunkt "
                f"„{result.item_title}“ "
                "benötigt mindestens ein Foto."
            )


def create_commission_from_safety_defect(
        defect: SafetyInspectionDefect,
        user: User,
        building_id: int | None = None,
) -> Commission:
    if defect.status in {
        SafetyInspectionDefectStatus
                .RESOLVED.value,
        SafetyInspectionDefectStatus
                .DISMISSED.value,
    }:
        raise ValueError(
            "Für diesen Mangel kann kein Auftrag "
            "mehr erstellt werden."
        )

    job = defect.job
    group = job.group
    if not group.corrective_orders_enabled:
        raise ValueError(
            "Für diese Prüfgruppe sind Mängelaufträge deaktiviert."
        )
    object_type = None
    object_id = None

    if job.use_unit_erp_id is not None:
        use_unit = (
            WowiUseUnit.query.filter_by(
                erp_id=job.use_unit_erp_id,
                active=True,
            ).one_or_none()
        )

        if use_unit is not None:
            object_type = "use_unit"
            object_id = use_unit.id

    elif job.building_erp_id is not None:
        building = (
            WowiBuilding.query.filter_by(
                erp_id=job.building_erp_id,
                active=True,
            ).one_or_none()
        )

        if building is not None:
            object_type = "building"
            object_id = building.id

    elif building_id is not None:
        building = db.session.get(
            WowiBuilding,
            building_id,
        )

        if (
                building is None
                or not building.active
                or building.economic_unit is None
                or building.economic_unit.erp_id
                != job.economic_unit_erp_id
        ):
            raise ValueError(
                "Das ausgewählte Gebäude gehört "
                "nicht zur Wirtschaftseinheit "
                "des Prüfauftrags."
            )

        object_type = "building"
        object_id = building.id

    if object_type is None or object_id is None:
        raise ValueError(
            "Bitte für den Mangel ein Gebäude "
            "auswählen."
        )

    task_template = (
        group.corrective_task_template
    )
    result = defect.result
    urgency_text = (
        "Dringender Mangel"
        if defect.severity
           == (
               SafetyInspectionResultState
               .DEFECT_URGENT.value
           )
        else "Mangel"
    )
    description_parts = [
        urgency_text,
        f"Prüfgruppe: {group.name}",
        f"Prüfpunkt: {result.item_title}",
        f"Prüfzeitraum: "
        f"{job.period_start:%d.%m.%Y} bis "
        f"{job.period_end:%d.%m.%Y}",
        f"Feststellung: {defect.description}",
    ]

    if defect.immediate_action:
        description_parts.append(
            "Sofortmaßnahme: "
            f"{defect.immediate_action}"
        )

    commission_text = "\n".join(
        description_parts
    )
    commission = create_pending_commission(
        {
            "commission_type_id": (
                group
                .corrective_commission_type_erp_id
            ),
            "object_type": object_type,
            "object_id": object_id,
            "title": (
                f"{urgency_text}: "
                f"{result.item_title}"
            )[:255],
            "damage_location": (
                result.item_title
            )[:255],
            "positions": [
                {
                    "service_catalog_id": (
                        task_template
                        .erp_service_catalogue_id
                    ),
                    "code": task_template.code,
                    "commission_text": (
                        commission_text
                    ),
                    "description": (
                        commission_text
                    ),
                    "units": 1,
                }
            ],
        },
        created_by_user=user,
        source=CommissionSource.WEB,
    )
    link = (
        SafetyInspectionDefectCommission(
            defect=defect,
            commission=commission,
            primary_link=(
                not defect.commission_links
            ),
            created_by=user,
        )
    )
    db.session.add(link)
    copy_defect_photos_to_commission(
        defect,
        commission,
        user,
    )
    defect.status = (
        SafetyInspectionDefectStatus
        .COMMISSION_CREATED.value
    )
    db.session.flush()
    return commission


def copy_defect_photos_to_commission(
        defect: SafetyInspectionDefect,
        commission: Commission,
        user: User,
) -> int:
    created_count = 0

    for photo in defect.result.photos:
        if not photo.active:
            continue

        duplicate = (
            CommissionPhoto.query.filter(
                CommissionPhoto.commission
                == commission,
                CommissionPhoto.content_sha256
                == photo.content_sha256,
                CommissionPhoto.active.is_(
                    True
                ),
            ).one_or_none()
        )

        if duplicate is not None:
            continue

        source_path = commission_photo_path(
            photo.storage_name
        )
        stored_photo = (
            store_commission_photo_bytes(
                commission=commission,
                raw_data=source_path.read_bytes(),
                original_filename=(
                    photo.original_filename
                ),
            )
        )
        commission_photo = CommissionPhoto(
            commission=commission,
            uploaded_by=user,
            source="safety_inspection",
            context="order",
            external_id=(
                f"safety-inspection:"
                f"{photo.public_id}"
            ),
            external_document_id=(
                "datlaeuft-photo-"
                f"{uuid4().hex}"
            ),
            external_entity_name="Commission",
            external_entity_id=(
                commission.erp_id
            ),
            original_filename=(
                stored_photo.original_filename
            ),
            storage_name=(
                stored_photo.storage_name
            ),
            thumbnail_storage_name=(
                stored_photo
                .thumbnail_storage_name
            ),
            mime_type=stored_photo.mime_type,
            size_bytes=stored_photo.size_bytes,
            content_sha256=(
                stored_photo.content_sha256
            ),
            caption=(
                    photo.caption
                    or defect.result.item_title
            ),
            active=True,
            erp_sync_state=(
                CommissionSyncState
                .PENDING.value
            ),
        )
        db.session.add(commission_photo)
        created_count += 1

    return created_count


def reconcile_safety_inspection_defects(
) -> dict[str, int]:
    result = {
        "seen": 0,
        "changed": 0,
        "unchanged": 0,
    }
    defects = (
        SafetyInspectionDefect.query.filter(
            SafetyInspectionDefect.status.in_(
                {
                    SafetyInspectionDefectStatus
                    .COMMISSION_CREATED.value,
                    SafetyInspectionDefectStatus
                    .IN_PROGRESS.value,
                    SafetyInspectionDefectStatus
                    .REVIEW_REQUIRED.value,
                }
            )
        ).all()
    )

    for defect in defects:
        result["seen"] += 1
        commissions = [
            link.commission
            for link in defect.commission_links
        ]

        if not commissions:
            result["unchanged"] += 1
            continue

        previous_status = defect.status

        if any(
                commission.local_status
                == CommissionLocalStatus.DONE.value
                and commission.completion_successful
                is not False
                for commission in commissions
        ):
            defect.status = (
                SafetyInspectionDefectStatus
                .RESOLVED.value
            )
            defect.resolved_at = utc_now()

        elif all(
                commission.local_status
                in {
                    CommissionLocalStatus
                            .DONE.value,
                    CommissionLocalStatus
                            .DELETED.value,
                }
                for commission in commissions
        ):
            defect.status = (
                SafetyInspectionDefectStatus
                .REVIEW_REQUIRED.value
            )

        elif any(
                commission.local_status
                == (
                        CommissionLocalStatus
                                .IN_PROGRESS.value
                )
                for commission in commissions
        ):
            defect.status = (
                SafetyInspectionDefectStatus
                .IN_PROGRESS.value
            )

        else:
            defect.status = (
                SafetyInspectionDefectStatus
                .COMMISSION_CREATED.value
            )

        if defect.status == previous_status:
            result["unchanged"] += 1
        else:
            result["changed"] += 1

    return result


def mark_missed_inspections(
        current_date: date | None = None,
) -> int:
    run_date = current_date or date.today()
    jobs = (
        SafetyInspectionJob.query.filter(
            SafetyInspectionJob.status.in_(
                {
                    SafetyInspectionJobStatus
                    .PLANNED.value,
                    SafetyInspectionJobStatus
                    .IN_PROGRESS.value,
                }
            ),
            SafetyInspectionJob.missed_on
            <= run_date,
        ).all()
    )

    for job in jobs:
        job.status = (
            SafetyInspectionJobStatus
            .MISSED.value
        )
        job.missed_at = utc_now()

    return len(jobs)


def ensure_job_editable(
        job: SafetyInspectionJob,
        user: User,
) -> None:
    if job.is_terminal:
        raise ValueError(
            "Diese Prüfung ist bereits "
            "abgeschlossen."
        )

    if (
            job.assigned_user_id != user.id
            and not user.is_admin
            and not (
            user
                    .can_manage_safety_inspections
    )
            and not (
            user
                    .can_review_safety_inspections
    )
    ):
        raise ValueError(
            "Die Prüfung ist einem anderen "
            "Benutzer zugewiesen."
        )


def latest_published_version(
        template: SafetyInspectionTemplate,
) -> SafetyInspectionTemplateVersion:
    versions = [
        version
        for version in template.versions
        if version.status
           == (
               SafetyInspectionTemplateVersionStatus
               .PUBLISHED.value
           )
    ]

    if not versions:
        raise ValueError(
            f"Der Prüfdetailplan "
            f"„{template.name}“ besitzt keine "
            "veröffentlichte Version."
        )

    return max(
        versions,
        key=lambda item: item.version_number,
    )


def next_period_start(
        period_start: date,
        schedule: SafetyInspectionSchedule,
) -> date:
    interval_value = (
        schedule.interval_value
    )

    if schedule.frequency == (
            SafetyInspectionScheduleFrequency
                    .DAILY.value
    ):
        return (
                period_start
                + timedelta(days=interval_value)
        )

    if schedule.frequency == (
            SafetyInspectionScheduleFrequency
                    .WEEKLY.value
    ):
        return (
                period_start
                + timedelta(
            days=7 * interval_value
        )
        )

    if schedule.frequency == (
            SafetyInspectionScheduleFrequency
                    .MONTHLY.value
    ):
        return add_months(
            period_start,
            interval_value,
        )

    if schedule.frequency == (
            SafetyInspectionScheduleFrequency
                    .YEARLY.value
    ):
        return add_years(
            period_start,
            interval_value,
        )

    raise ValueError(
        "Der Turnus ist ungültig."
    )


def inspection_generation_date(
        period_start: date,
        schedule: SafetyInspectionSchedule,
) -> date:
    if schedule.generation_mode == (
            SafetyInspectionGenerationMode
                    .PREVIOUS_MONTH_DAY.value
    ):
        previous_month = add_months(
            period_start.replace(day=1),
            -1,
        )
        day_of_month = min(
            (
                    schedule
                    .generation_day_of_month
                    or 1
            ),
            calendar.monthrange(
                previous_month.year,
                previous_month.month,
            )[1],
        )
        return previous_month.replace(
            day=day_of_month
        )

    return (
            period_start
            - timedelta(
        days=(
            schedule
            .generation_lead_days
        )
    )
    )


def inspection_period_key(
        period_start: date,
        schedule: SafetyInspectionSchedule,
) -> str:
    if schedule.frequency == (
            SafetyInspectionScheduleFrequency
                    .MONTHLY.value
    ):
        return period_start.strftime(
            "%Y-%m"
        )

    if schedule.frequency == (
            SafetyInspectionScheduleFrequency
                    .YEARLY.value
    ):
        return period_start.strftime(
            "%Y"
        )

    return period_start.isoformat()


def add_months(
        value: date,
        month_count: int,
) -> date:
    month_index = (
            value.year * 12
            + value.month
            - 1
            + month_count
    )
    year, month_offset = divmod(
        month_index,
        12,
    )
    month = month_offset + 1
    day = min(
        value.day,
        calendar.monthrange(
            year,
            month,
        )[1],
    )
    return value.replace(
        year=year,
        month=month,
        day=day,
    )


def add_years(
        value: date,
        year_count: int,
) -> date:
    year = value.year + year_count
    day = min(
        value.day,
        calendar.monthrange(
            year,
            value.month,
        )[1],
    )
    return value.replace(
        year=year,
        day=day,
    )


def resolve_group_object_contexts(
        group: SafetyInspectionGroup,
) -> list[SafetyInspectionObjectContext]:
    included_targets = [
        target
        for target in group.targets
        if target.included
    ]
    excluded_targets = [
        target
        for target in group.targets
        if not target.included
    ]
    available_contexts = {
        (
            context.object_level,
            context.object_erp_id,
        ): context
        for context in (
            all_group_object_contexts(group)
        )
    }

    if group.selection_mode == (
            SafetyInspectionSelectionMode
                    .BLACKLIST.value
    ):
        contexts = dict(available_contexts)
    else:
        contexts = {}

    regex_pattern = (
        compile_group_id_num_regex(group)
    )

    if regex_pattern is not None:
        matching_contexts = {
            key: context
            for key, context
            in available_contexts.items()
            if regex_pattern.search(
                context.object_id_num or ""
            )
        }

        if group.id_num_regex_action == (
                SafetyInspectionRegexAction
                        .INCLUDE.value
        ):
            contexts.update(
                matching_contexts
            )
        else:
            for key in matching_contexts:
                contexts.pop(key, None)

    if group.selection_mode == (
            SafetyInspectionSelectionMode
                    .BLACKLIST.value
    ):
        remove_group_targets(
            contexts,
            excluded_targets,
        )
        add_group_targets(
            contexts,
            group,
            included_targets,
        )
    else:
        add_group_targets(
            contexts,
            group,
            included_targets,
        )
        remove_group_targets(
            contexts,
            excluded_targets,
        )

    return sorted(
        contexts.values(),
        key=lambda item: (
            item.address or "",
            item.location or "",
            item.object_erp_id,
        ),
    )


def compile_group_id_num_regex(
        group: SafetyInspectionGroup,
) -> re.Pattern[str] | None:
    regex_pattern = clean_text(
        group.id_num_regex
    )

    if regex_pattern is None:
        return None

    return re.compile(
        regex_pattern,
        re.IGNORECASE,
    )


def add_group_targets(
        contexts: dict[
            tuple[str, int],
            SafetyInspectionObjectContext,
        ],
        group: SafetyInspectionGroup,
        targets: Iterable[
            SafetyInspectionGroupTarget
        ],
) -> None:
    for target in targets:
        for context in expand_group_target(
                group,
                target,
        ):
            contexts[
                (
                    context.object_level,
                    context.object_erp_id,
                )
            ] = context


def remove_group_targets(
        contexts: dict[
            tuple[str, int],
            SafetyInspectionObjectContext,
        ],
        targets: Iterable[
            SafetyInspectionGroupTarget
        ],
) -> None:
    for target in targets:
        for key, context in list(
                contexts.items()
        ):
            if target_matches_context(
                    target,
                    context,
            ):
                contexts.pop(key, None)


def building_is_available(
        building: WowiBuilding,
) -> bool:
    return (
            building.active
            and building.economic_unit is not None
            and building.economic_unit.active
            and any(
        use_unit.active
        for use_unit in building.use_units
    )
    )


def use_unit_is_available(
        use_unit: WowiUseUnit,
) -> bool:
    return (
            use_unit.active
            and use_unit.economic_unit is not None
            and use_unit.economic_unit.active
            and use_unit.building is not None
            and building_is_available(
        use_unit.building
    )
    )


def all_group_object_contexts(
        group: SafetyInspectionGroup,
) -> list[SafetyInspectionObjectContext]:
    if group.object_level == (
            SafetyInspectionObjectLevel
                    .ECONOMIC_UNIT.value
    ):
        economic_units = (
            WowiEconomicUnit.query.filter(
                WowiEconomicUnit.active.is_(
                    True
                )
            )
            .order_by(
                WowiEconomicUnit.erp_id.asc()
            )
            .all()
        )

        return [
            context_from_economic_unit(
                economic_unit
            )
            for economic_unit in economic_units
        ]

    if group.object_level == (
            SafetyInspectionObjectLevel
                    .BUILDING.value
    ):
        buildings = (
            WowiBuilding.query.filter(
                WowiBuilding.active.is_(True)
            )
            .order_by(
                WowiBuilding.erp_id.asc()
            )
            .all()
        )

        return [
            context_from_building(building)
            for building in buildings
            if building_is_available(
                building
            )
        ]

    use_units = (
        WowiUseUnit.query.filter(
            WowiUseUnit.active.is_(True)
        )
        .order_by(
            WowiUseUnit.erp_id.asc()
        )
        .all()
    )

    return [
        context_from_use_unit(use_unit)
        for use_unit in use_units
        if (
                use_unit_is_available(use_unit)
                and (
                        group.include_vacancies
                        or use_unit_is_occupied(
                    use_unit
                )
                )
        )
    ]


def expand_group_target(
        group: SafetyInspectionGroup,
        target: SafetyInspectionGroupTarget,
) -> list[SafetyInspectionObjectContext]:
    economic_units: list[
        WowiEconomicUnit
    ] = []
    buildings: list[WowiBuilding] = []
    use_units: list[WowiUseUnit] = []

    if target.object_type == (
            SafetyInspectionObjectLevel
                    .ECONOMIC_UNIT.value
    ):
        economic_unit = (
            WowiEconomicUnit.query.filter_by(
                erp_id=target.object_erp_id,
                active=True,
            ).one_or_none()
        )

        if economic_unit is None:
            return []

        economic_units = [economic_unit]
        buildings = [
            item
            for item in economic_unit.buildings
            if building_is_available(item)
        ]
        use_units = [
            item
            for item in economic_unit.use_units
            if use_unit_is_available(item)
        ]

    elif target.object_type == (
            SafetyInspectionObjectLevel
                    .BUILDING.value
    ):
        building = (
            WowiBuilding.query.filter_by(
                erp_id=target.object_erp_id,
                active=True,
            ).one_or_none()
        )

        if (
            building is None
            or not building_is_available(
                building
            )
        ):
            return []

        buildings = [building]
        use_units = [
            item
            for item in building.use_units
            if use_unit_is_available(item)
        ]

        if building.economic_unit is not None:
            economic_units = [
                building.economic_unit
            ]

    elif target.object_type == (
            SafetyInspectionObjectLevel
                    .USE_UNIT.value
    ):
        use_unit = (
            WowiUseUnit.query.filter_by(
                erp_id=target.object_erp_id,
                active=True,
            ).one_or_none()
        )

        if (
                use_unit is None
                or not use_unit_is_available(
            use_unit
        )
        ):
            return []

        use_units = [use_unit]

        if use_unit.building is not None:
            buildings = [use_unit.building]

        if use_unit.economic_unit is not None:
            economic_units = [
                use_unit.economic_unit
            ]

    else:
        return []

    if group.object_level == (
            SafetyInspectionObjectLevel
                    .ECONOMIC_UNIT.value
    ):
        return [
            context_from_economic_unit(item)
            for item in economic_units
            if item.active
        ]

    if group.object_level == (
        SafetyInspectionObjectLevel
        .BUILDING.value
    ):
        return [
            context_from_building(item)
            for item in buildings
            if building_is_available(item)
        ]

    return [
        context_from_use_unit(item)
        for item in use_units
        if (
            use_unit_is_available(item)
            and (
                group.include_vacancies
                or use_unit_is_occupied(item)
            )
        )
    ]


def context_from_economic_unit(
        economic_unit: WowiEconomicUnit,
) -> SafetyInspectionObjectContext:
    return SafetyInspectionObjectContext(
        object_level=(
            SafetyInspectionObjectLevel
            .ECONOMIC_UNIT.value
        ),
        object_erp_id=economic_unit.erp_id,
        economic_unit=economic_unit,
        building=None,
        use_unit=None,
        address=economic_unit.location,
        location=economic_unit.name,
        latitude=None,
        longitude=None,
        object_id_num=economic_unit.id_num,
    )


def context_from_building(
        building: WowiBuilding,
) -> SafetyInspectionObjectContext:
    if building.economic_unit is None:
        raise ValueError(
            "Dem Gebäude ist keine "
            "Wirtschaftseinheit zugeordnet."
        )

    return SafetyInspectionObjectContext(
        object_level=(
            SafetyInspectionObjectLevel
            .BUILDING.value
        ),
        object_erp_id=building.erp_id,
        economic_unit=building.economic_unit,
        building=building,
        use_unit=None,
        address=building.address,
        location=None,
        latitude=building.latitude,
        longitude=building.longitude,
        object_id_num=building.id_num,
    )


def context_from_use_unit(
        use_unit: WowiUseUnit,
) -> SafetyInspectionObjectContext:
    if use_unit.economic_unit is None:
        raise ValueError(
            "Der Nutzungseinheit ist keine "
            "Wirtschaftseinheit zugeordnet."
        )

    location_parts = [
        use_unit.floor_name,
        use_unit.position_name,
        use_unit.description_of_position,
    ]
    building = use_unit.building

    return SafetyInspectionObjectContext(
        object_level=(
            SafetyInspectionObjectLevel
            .USE_UNIT.value
        ),
        object_erp_id=use_unit.erp_id,
        economic_unit=use_unit.economic_unit,
        building=building,
        use_unit=use_unit,
        address=use_unit.address,
        location=(
                " · ".join(
                    item
                    for item in location_parts
                    if item
                )
                or None
        ),
        latitude=(
            building.latitude
            if building is not None
            else None
        ),
        longitude=(
            building.longitude
            if building is not None
            else None
        ),
        object_id_num=use_unit.id_num,
    )


def target_matches_context(
        target: SafetyInspectionGroupTarget,
        context: SafetyInspectionObjectContext,
) -> bool:
    if target.object_type == (
            SafetyInspectionObjectLevel
                    .ECONOMIC_UNIT.value
    ):
        return (
                context.economic_unit.erp_id
                == target.object_erp_id
        )

    if target.object_type == (
            SafetyInspectionObjectLevel
                    .BUILDING.value
    ):
        return (
                context.building is not None
                and context.building.erp_id
                == target.object_erp_id
        )

    if target.object_type == (
            SafetyInspectionObjectLevel
                    .USE_UNIT.value
    ):
        return (
                context.use_unit is not None
                and context.use_unit.erp_id
                == target.object_erp_id
        )

    return False


def use_unit_is_occupied(
        use_unit: WowiUseUnit,
) -> bool:
    return any(
        agreement.active
        and not agreement.is_vacancy
        for agreement in (
            use_unit.license_agreements
        )
    )


def clean_text(
        value: str | None,
) -> str | None:
    if value is None:
        return None

    normalized = str(value).strip()
    return normalized or None
