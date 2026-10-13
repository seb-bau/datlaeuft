from __future__ import annotations

from datetime import date

from sqlalchemy import and_, case, func, or_

from .models import SafetyInspectionJob, SafetyInspectionJobStatus, User
from .safety_inspection_progress import _current_group_period


STATUS_DEFINITIONS = (
    (SafetyInspectionJobStatus.PLANNED.value, "Geplant", "#6c757d"),
    (SafetyInspectionJobStatus.IN_PROGRESS.value, "In Bearbeitung", "#0d6efd"),
    (SafetyInspectionJobStatus.COMPLETED.value, "Erledigt ohne Mängel", "#198754"),
    (
        SafetyInspectionJobStatus.COMPLETED_WITH_DEFECTS.value,
        "Erledigt mit Mängeln",
        "#6f42c1",
    ),
    (SafetyInspectionJobStatus.MISSED.value, "Abgelaufen", "#dc3545"),
    ("other", "Sonstige", "#343a40"),
)


def _empty_statistics():
    return {
        "counts": {status: 0 for status, _, _ in STATUS_DEFINITIONS},
        "canceled": 0,
        "overdue": 0,
    }


def _finish_statistics(statistics):
    statistics["total"] = sum(statistics["counts"].values())
    statistics["completed"] = (
        statistics["counts"][SafetyInspectionJobStatus.COMPLETED.value]
        + statistics["counts"][
            SafetyInspectionJobStatus.COMPLETED_WITH_DEFECTS.value
        ]
    )
    statistics["percentage"] = (
        statistics["completed"] * 100 / statistics["total"]
        if statistics["total"]
        else None
    )


def build_safety_inspection_report(groups, values, run_date=None):
    current_date = run_date or date.today()
    filters = {
        "period_mode": values.get("period_mode", "current"),
        "group_scope": values.get("group_scope", "all"),
        "start_date": values.get("start_date", current_date.replace(day=1).isoformat()),
        "end_date": values.get("end_date", current_date.isoformat()),
        "selected_group_ids": [],
    }
    report = {
        "filters": filters,
        "summary": _empty_statistics(),
        "groups": [],
        "craftsmen": [],
        "statuses": [],
        "status_gradient": "#e9ecef",
        "unassigned": 0,
        "error": None,
        "warnings": [],
        "current_date": current_date,
    }
    _finish_statistics(report["summary"])
    group_by_id = {group.id: group for group in groups}

    try:
        if filters["group_scope"] == "all":
            filters["selected_group_ids"] = list(group_by_id)
        elif filters["group_scope"] == "selected":
            try:
                selected_ids = {int(value) for value in values.getlist("group_id")}
            except (TypeError, ValueError) as exc:
                raise ValueError("Die Auswahl der Prüfgruppen ist ungültig.") from exc
            filters["selected_group_ids"] = sorted(selected_ids.intersection(group_by_id))
            if not selected_ids:
                raise ValueError("Bitte mindestens eine Prüfgruppe auswählen.")
            if not selected_ids.issubset(group_by_id):
                raise ValueError("Eine ausgewählte Prüfgruppe existiert nicht mehr.")
            filters["selected_group_ids"] = sorted(selected_ids)
        else:
            raise ValueError("Die Auswahl der Prüfgruppen ist ungültig.")

        if filters["period_mode"] == "range":
            try:
                start_date = date.fromisoformat(filters["start_date"])
                end_date = date.fromisoformat(filters["end_date"])
            except (TypeError, ValueError) as exc:
                raise ValueError("Bitte ein gültiges Von- und Bis-Datum angeben.") from exc
            if end_date < start_date:
                raise ValueError("Das Bis-Datum darf nicht vor dem Von-Datum liegen.")
            filters["start_date"] = start_date.isoformat()
            filters["end_date"] = end_date.isoformat()
            report["range_start"] = start_date
            report["range_end"] = end_date
        elif filters["period_mode"] != "current":
            raise ValueError("Der ausgewählte Zeitraum ist ungültig.")
    except ValueError as exc:
        report["error"] = str(exc)
        return report

    conditions = []
    statistics_by_group = {}
    selected_group_ids = set(filters["selected_group_ids"])
    for group in groups:
        if group.id not in selected_group_ids:
            continue
        statistics = _empty_statistics()
        statistics.update({
            "name": group.name,
            "active": group.active,
            "period_start": None,
            "period_end": None,
            "error": None,
        })
        statistics_by_group[group.id] = statistics
        report["groups"].append(statistics)
        if filters["period_mode"] == "current":
            try:
                period = _current_group_period(group, current_date)
            except (ValueError, OverflowError):
                statistics["error"] = "Prüfzeitraum nicht berechenbar."
                report["warnings"].append(f"{group.name}: Prüfzeitraum nicht berechenbar.")
                continue
            if period is None:
                continue
            statistics["period_start"], statistics["period_end"] = period
            conditions.append(and_(
                SafetyInspectionJob.group_id == group.id,
                SafetyInspectionJob.period_start == period[0],
            ))
        else:
            conditions.append(and_(
                SafetyInspectionJob.group_id == group.id,
                SafetyInspectionJob.period_start <= end_date,
                SafetyInspectionJob.period_end >= start_date,
            ))

    open_statuses = (
        SafetyInspectionJobStatus.PLANNED.value,
        SafetyInspectionJobStatus.IN_PROGRESS.value,
    )
    overdue_condition = or_(
        SafetyInspectionJob.status == SafetyInspectionJobStatus.MISSED.value,
        and_(
            SafetyInspectionJob.status.in_(open_statuses),
            SafetyInspectionJob.due_to < current_date,
        ),
    )
    statistics_by_user = {}
    for offset in range(0, len(conditions), 250):
        rows = (
            SafetyInspectionJob.query.with_entities(
                SafetyInspectionJob.group_id,
                SafetyInspectionJob.assigned_user_id,
                User.display_name.label("user_name"),
                SafetyInspectionJob.status,
                func.count(SafetyInspectionJob.id).label("job_count"),
                func.sum(case((overdue_condition, 1), else_=0)).label("overdue_count"),
                func.min(SafetyInspectionJob.period_start).label("first_period_start"),
                func.max(SafetyInspectionJob.period_end).label("last_period_end"),
            )
            .outerjoin(User, User.id == SafetyInspectionJob.assigned_user_id)
            .filter(or_(*conditions[offset:offset + 250]))
            .group_by(
                SafetyInspectionJob.group_id,
                SafetyInspectionJob.assigned_user_id,
                User.display_name,
                SafetyInspectionJob.status,
            )
            .all()
        )
        for row in rows:
            group_statistics = statistics_by_group[row.group_id]
            if filters["period_mode"] == "range":
                group_statistics["period_start"] = min(
                    group_statistics["period_start"] or row.first_period_start,
                    row.first_period_start,
                )
                group_statistics["period_end"] = max(
                    group_statistics["period_end"] or row.last_period_end,
                    row.last_period_end,
                )
            count = int(row.job_count)
            if row.status == SafetyInspectionJobStatus.CANCELED.value:
                report["summary"]["canceled"] += count
                group_statistics["canceled"] += count
                continue
            user_statistics = statistics_by_user.setdefault(row.assigned_user_id, {
                **_empty_statistics(),
                "name": (
                    "Nicht zugeordnet" if row.assigned_user_id is None
                    else row.user_name or "Unbekannter Benutzer"
                ),
                "unassigned": row.assigned_user_id is None,
            })
            status = row.status if row.status in report["summary"]["counts"] else "other"
            for statistics in (report["summary"], group_statistics, user_statistics):
                statistics["counts"][status] += count
                statistics["overdue"] += int(row.overdue_count or 0)
            if row.assigned_user_id is None:
                report["unassigned"] += count

    for statistics in (
        report["summary"], *report["groups"], *statistics_by_user.values()
    ):
        _finish_statistics(statistics)
    report["craftsmen"] = sorted(
        statistics_by_user.values(),
        key=lambda statistics: (
            statistics["unassigned"], -statistics["total"], statistics["name"].casefold()
        ),
    )
    maximum_total = max((row["total"] for row in report["craftsmen"]), default=1)
    report["maximum_total"] = maximum_total
    for statistics in report["craftsmen"]:
        statistics["volume_percentage"] = statistics["total"] * 100 / maximum_total

    segments = []
    position = 0
    for status, label, color in STATUS_DEFINITIONS:
        count = report["summary"]["counts"][status]
        if status == "other" and not count:
            continue
        percentage = count * 100 / report["summary"]["total"] if report["summary"]["total"] else 0
        report["statuses"].append({
            "key": status, "label": label, "color": color,
            "count": count, "percentage": percentage,
        })
        if count:
            segments.append(f"{color} {position:.6f}% {position + percentage:.6f}%")
            position += percentage
    if segments:
        report["status_gradient"] = f"conic-gradient({', '.join(segments)})"
    return report