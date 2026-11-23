from __future__ import annotations

from calendar import monthrange
from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any

from sqlalchemy.orm import joinedload
from .models import (
    SafetyInspectionJob,
    TimeEntry,
    User,
    UserWorkTimeProfileAssignment,
    WorkTimeTargetOverride,
)


def work_time_period_bounds(
    period: str,
    anchor_date: date,
) -> tuple[date, date]:
    if period == "day":
        return anchor_date, anchor_date

    if period == "week":
        start_date = anchor_date - timedelta(
            days=anchor_date.weekday()
        )
        return start_date, start_date + timedelta(days=6)

    if period == "month":
        start_date = anchor_date.replace(day=1)
        end_date = anchor_date.replace(
            day=monthrange(
                anchor_date.year,
                anchor_date.month,
            )[1]
        )
        return start_date, end_date

    raise ValueError(
        "Der Auswertungszeitraum ist ungültig."
    )


def current_work_time_assignment(
    user_id: int,
    target_date: date,
) -> UserWorkTimeProfileAssignment | None:
    return (
        UserWorkTimeProfileAssignment.query
        .options(
            joinedload(
                UserWorkTimeProfileAssignment.profile
            )
        )
        .filter(
            UserWorkTimeProfileAssignment.user_id
            == user_id,
            UserWorkTimeProfileAssignment.effective_from
            <= target_date,
        )
        .order_by(
            UserWorkTimeProfileAssignment
            .effective_from
            .desc(),
            UserWorkTimeProfileAssignment.id.desc(),
        )
        .first()
    )


def work_time_day_row(
    user: User,
    target_date: date,
) -> dict[str, Any]:
    return work_time_rows(
        [user],
        target_date,
        target_date,
    )[0]["days"][0]


def work_time_rows(
    users: Sequence[User],
    start_date: date,
    end_date: date,
) -> list[dict[str, Any]]:
    user_items = list(users)
    user_ids = [item.id for item in user_items]

    recorded_by_day = {}
    entries_by_user = {
        user_id: []
        for user_id in user_ids
    }
    assignments_by_user = {
        user_id: []
        for user_id in user_ids
    }
    overrides_by_day = {}

    if user_ids and start_date <= end_date:
        time_entries = (
            TimeEntry.query
            .options(
                joinedload(
                    TimeEntry.commission
                ),
                joinedload(
                    TimeEntry.task_template
                ),
                joinedload(TimeEntry.safety_inspection_job).joinedload(SafetyInspectionJob.group),
            )
            .filter(
                TimeEntry.user_id.in_(user_ids),
                TimeEntry.service_date >= start_date,
                TimeEntry.service_date <= end_date,
            )
            .order_by(
                TimeEntry.user_id.asc(),
                TimeEntry.service_date.desc(),
                TimeEntry.created_at.desc(),
                TimeEntry.id.desc(),
            )
            .all()
        )

        for entry in time_entries:
            entries_by_user[
                entry.user_id
            ].append(entry)
            day_key = (
                entry.user_id,
                entry.service_date,
            )
            recorded_by_day[day_key] = (
                recorded_by_day.get(day_key, 0)
                + int(entry.minutes or 0)
            )

        assignments = (
            UserWorkTimeProfileAssignment.query
            .options(
                joinedload(
                    UserWorkTimeProfileAssignment.profile
                )
            )
            .filter(
                UserWorkTimeProfileAssignment.user_id.in_(
                    user_ids
                ),
                UserWorkTimeProfileAssignment.effective_from
                <= end_date,
            )
            .order_by(
                UserWorkTimeProfileAssignment.user_id.asc(),
                UserWorkTimeProfileAssignment
                .effective_from
                .asc(),
                UserWorkTimeProfileAssignment.id.asc(),
            )
            .all()
        )

        for assignment in assignments:
            assignments_by_user[
                assignment.user_id
            ].append(assignment)

        overrides = (
            WorkTimeTargetOverride.query
            .filter(
                WorkTimeTargetOverride.user_id.in_(
                    user_ids
                ),
                WorkTimeTargetOverride.work_date
                >= start_date,
                WorkTimeTargetOverride.work_date
                <= end_date,
            )
            .all()
        )

        overrides_by_day = {
            (item.user_id, item.work_date): item
            for item in overrides
        }

    result = []

    for user in user_items:
        user_assignments = assignments_by_user.get(
            user.id,
            [],
        )
        assignment_index = 0
        active_assignment = None
        day_rows = []
        expected_total = 0
        recorded_total = 0
        missing_profile_days = 0
        current_date = start_date

        while current_date <= end_date:
            while (
                assignment_index
                < len(user_assignments)
                and user_assignments[
                    assignment_index
                ].effective_from
                <= current_date
            ):
                active_assignment = user_assignments[
                    assignment_index
                ]
                assignment_index += 1

            profile = (
                active_assignment.profile
                if active_assignment is not None
                else None
            )
            target_override = overrides_by_day.get(
                (user.id, current_date)
            )

            if target_override is not None:
                expected_minutes = (
                    target_override.target_minutes
                )
            elif profile is not None:
                expected_minutes = (
                    profile.minutes_for_weekday(
                        current_date.weekday()
                    )
                )
            else:
                expected_minutes = None

            recorded_minutes = recorded_by_day.get(
                (user.id, current_date),
                0,
            )

            if expected_minutes is None:
                status = "missing_profile"
                missing_profile_days += 1
                difference_minutes = None
            else:
                difference_minutes = (
                    recorded_minutes
                    - expected_minutes
                )
                status = (
                    "complete"
                    if difference_minutes >= 0
                    else "missing"
                )
                expected_total += expected_minutes

            recorded_total += recorded_minutes

            day_rows.append({
                "date": current_date,
                "profile": profile,
                "assignment": active_assignment,
                "override": target_override,
                "expected_minutes": expected_minutes,
                "recorded_minutes": recorded_minutes,
                "difference_minutes": difference_minutes,
                "status": status,
            })

            current_date += timedelta(days=1)

        difference_total = (
            None
            if missing_profile_days or not day_rows
            else recorded_total - expected_total
        )

        if not day_rows:
            status = "not_evaluated"
        elif missing_profile_days:
            status = "missing_profile"
        elif (
            difference_total is not None
            and difference_total >= 0
        ):
            status = "complete"
        else:
            status = "missing"

        result.append({
            "user": user,
            "days": day_rows,
            "entries": entries_by_user.get(
                user.id,
                [],
            ),
            "expected_minutes": expected_total,
            "recorded_minutes": recorded_total,
            "difference_minutes": difference_total,
            "missing_profile_days": (
                missing_profile_days
            ),
            "status": status,
        })

    return result
