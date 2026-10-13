from __future__ import annotations

from collections import defaultdict

from sqlalchemy import or_
from sqlalchemy.orm.attributes import set_committed_value

from .audit import audit
from .extensions import db
from .models import (
    PushDelivery,
    PushDeliveryState,
    SafetyInspectionDefect,
    SafetyInspectionGroup,
    SafetyInspectionJob,
    SafetyInspectionJobStatus,
    SafetyInspectionPhoto,
    SafetyInspectionProtocol,
    SafetyInspectionResult,
    SafetyInspectionResultState,
    User,
)


def _locked_rows(model, *conditions):
    return (
        model.query.filter(*conditions)
        .order_by(model.id.asc())
        .populate_existing()
        .with_for_update()
        .all()
    )


def _job_is_unprocessed(job, results, photographed_result_ids) -> bool:
    if job.status != SafetyInspectionJobStatus.PLANNED.value:
        return False

    if any(value is not None for value in (
        job.started_at,
        job.completed_at,
        job.completed_by_user_id,
        job.missed_at,
        job.canceled_at,
        job.canceled_by_user_id,
        job.overall_note,
        job.cancellation_reason,
    )):
        return False

    return all(
        result.state == SafetyInspectionResultState.NOT_CHECKED.value
        and result.version == 1
        and result.checked_at is None
        and result.checked_by_user_id is None
        and result.note is None
        and result.immediate_action is None
        and result.id not in photographed_result_ids
        for result in results
    )


def reset_unprocessed_safety_inspection_jobs(
    group: SafetyInspectionGroup,
    user: User,
) -> int:
    removed_count = 0

    with db.session.no_autoflush:
        jobs = _locked_rows(
            SafetyInspectionJob,
            SafetyInspectionJob.group_id == group.id,
            SafetyInspectionJob.status == SafetyInspectionJobStatus.PLANNED.value,
        )

        for offset in range(0, len(jobs), 500):
            batch = jobs[offset:offset + 500]
            job_ids = [job.id for job in batch]

            results = _locked_rows(
                SafetyInspectionResult,
                SafetyInspectionResult.job_id.in_(job_ids),
            )
            results_by_job = defaultdict(list)
            results_by_id = {}
            for result in results:
                results_by_job[result.job_id].append(result)
                results_by_id[result.id] = result

            photos = _locked_rows(
                SafetyInspectionPhoto,
                SafetyInspectionPhoto.result.has(
                    SafetyInspectionResult.job_id.in_(job_ids)
                ),
            )
            photographed_result_ids = {
                photo.result_id for photo in photos
            }

            defects = _locked_rows(
                SafetyInspectionDefect,
                or_(
                    SafetyInspectionDefect.job_id.in_(job_ids),
                    SafetyInspectionDefect.result.has(
                        SafetyInspectionResult.job_id.in_(job_ids)
                    ),
                ),
            )
            protocols = _locked_rows(
                SafetyInspectionProtocol,
                SafetyInspectionProtocol.job_id.in_(job_ids),
            )

            protected_job_ids = {
                defect.job_id for defect in defects
            }
            protected_job_ids.update(
                protocol.job_id for protocol in protocols
            )
            protected_job_ids.update(
                results_by_id[defect.result_id].job_id
                for defect in defects
                if defect.result_id in results_by_id
            )

            removable_jobs = [
                job for job in batch
                if job.id not in protected_job_ids
                and _job_is_unprocessed(
                    job,
                    results_by_job[job.id],
                    photographed_result_ids,
                )
            ]
            if not removable_jobs:
                continue

            deliveries = _locked_rows(
                PushDelivery,
                PushDelivery.safety_inspection_job_id.in_(
                    [job.id for job in removable_jobs]
                ),
            )
            deliveries_by_job = defaultdict(list)
            for delivery in deliveries:
                deliveries_by_job[
                    delivery.safety_inspection_job_id
                ].append(delivery)

            for job in removable_jobs:
                job_results = results_by_job[job.id]

                set_committed_value(job, "results", job_results)
                set_committed_value(job, "defects", [])
                set_committed_value(job, "protocols", [])
                set_committed_value(
                    job,
                    "push_deliveries",
                    deliveries_by_job[job.id],
                )

                for result in job_results:
                    set_committed_value(result, "photos", [])
                    set_committed_value(result, "defect", None)

                for delivery in list(deliveries_by_job[job.id]):
                    delivery.safety_inspection_job = None

                    if delivery.state not in {
                        PushDeliveryState.SUCCEEDED.value,
                        PushDeliveryState.DISCARDED.value,
                    }:
                        delivery.state = PushDeliveryState.DISCARDED.value
                        delivery.locked_at = None
                        delivery.last_error = (
                            "Der Prüfauftrag wurde zurückgesetzt."
                        )

                audit(
                    "safety_inspection.job_reset",
                    "safety_inspection_job",
                    job.id,
                    {
                        "group_id": group.id,
                        "public_id": job.public_id,
                        "assigned_user_id": job.assigned_user_id,
                        "object_level": job.object_level,
                        "object_erp_id": job.object_erp_id,
                        "period_start": job.period_start.isoformat(),
                    },
                    actor_user_id=user.id,
                )
                db.session.delete(job)
                removed_count += 1

    db.session.flush()
    return removed_count