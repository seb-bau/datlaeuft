from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import and_, case, func, or_

from .models import (
    SafetyInspectionGroup,
    SafetyInspectionJob,
    SafetyInspectionJobStatus,
    SafetyInspectionScheduleFrequency,
)
from .safety_inspection_service import next_period_start


def _current_group_period(group, current_date: date):
    target_date = (
        min(current_date, group.ends_on)
        if group.ends_on
        else current_date
    )
    period_start = group.first_period_start

    if period_start > target_date:
        return None

    schedule = group.schedule
    if schedule.interval_value < 1:
        raise ValueError(
            "Das Turnusintervall muss mindestens 1 betragen."
        )

    day_factors = {
        SafetyInspectionScheduleFrequency.DAILY.value: 1,
        SafetyInspectionScheduleFrequency.WEEKLY.value: 7,
    }
    factor = day_factors.get(schedule.frequency)

    if factor is not None:
        interval_days = factor * schedule.interval_value
        elapsed_periods = (
            (target_date - period_start).days // interval_days
        )
        period_start += timedelta(
            days=elapsed_periods * interval_days
        )

    next_start = next_period_start(period_start, schedule)
    while next_start <= target_date:
        period_start = next_start
        next_start = next_period_start(period_start, schedule)

    period_end = next_start - timedelta(days=1)
    if not period_start <= current_date <= period_end:
        return None

    return period_start, period_end


def get_safety_inspection_group_progress(
    groups: list[SafetyInspectionGroup],
    run_date: date | None = None,
) -> dict[int, dict]:
    current_date = run_date or date.today()
    progress_by_group = {}
    period_conditions = []

    for group in groups:
        progress = {
            "period_start": None,
            "period_end": None,
            "completed": 0,
            "total": 0,
            "canceled": 0,
            "percentage": None,
            "error": None,
        }
        progress_by_group[group.id] = progress

        try:
            period = _current_group_period(group, current_date)
        except (ValueError, OverflowError):
            progress["error"] = "Prüfzeitraum nicht berechenbar."
            continue

        if period is None:
            continue

        progress["period_start"], progress["period_end"] = period
        period_conditions.append(and_(
            SafetyInspectionJob.group_id == group.id,
            SafetyInspectionJob.period_start == period[0],
        ))

    completed_statuses = {
        SafetyInspectionJobStatus.COMPLETED.value,
        SafetyInspectionJobStatus.COMPLETED_WITH_DEFECTS.value,
    }

    for offset in range(0, len(period_conditions), 500):
        rows = (
            SafetyInspectionJob.query.with_entities(
                SafetyInspectionJob.group_id,
                func.count(SafetyInspectionJob.id).label("generated"),
                func.sum(case(
                    (
                        SafetyInspectionJob.status.in_(
                            completed_statuses
                        ),
                        1,
                    ),
                    else_=0,
                )).label("completed"),
                func.sum(case(
                    (
                        SafetyInspectionJob.status
                        == SafetyInspectionJobStatus.CANCELED.value,
                        1,
                    ),
                    else_=0,
                )).label("canceled"),
            )
            .filter(or_(*period_conditions[offset:offset + 500]))
            .group_by(SafetyInspectionJob.group_id)
            .all()
        )

        for row in rows:
            progress = progress_by_group[row.group_id]
            progress["completed"] = int(row.completed or 0)
            progress["canceled"] = int(row.canceled or 0)
            progress["total"] = (
                int(row.generated) - progress["canceled"]
            )

            if progress["total"]:
                progress["percentage"] = round(
                    progress["completed"] * 100 / progress["total"],
                    1,
                )

    return progress_by_group