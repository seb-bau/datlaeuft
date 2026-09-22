from __future__ import annotations

from dataclasses import dataclass
from datetime import (
    date,
    datetime,
    time,
    timedelta,
    timezone,
)
import re
from typing import Any, Mapping

from sqlalchemy import case, distinct, func
from sqlalchemy.orm import joinedload

from .extensions import db
from .models import (
    Commission,
    CommissionLocalStatus,
    CraftActivity,
    InventoryItem,
    InventoryItemGroup,
    InventoryItemType,
    InventoryMovement,
    InventoryMovementType,
    MaterialUsageStatus,
    TimeEntry,
    TimeTaskTemplate,
    User,
)


GERMAN_MONTH_NAMES = (
    "Januar",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
)


@dataclass(frozen=True)
class ReportPeriod:
    mode: str
    start_date: date
    end_date_exclusive: date
    label: str
    month_value: str
    start_month_value: str
    end_month_value: str
    year_value: str

    @property
    def end_date(self) -> date:
        return (
            self.end_date_exclusive
            - timedelta(days=1)
        )

    @property
    def start_datetime(self) -> datetime:
        return datetime.combine(
            self.start_date,
            time.min,
            timezone.utc,
        )

    @property
    def end_datetime_exclusive(
        self,
    ) -> datetime:
        return datetime.combine(
            self.end_date_exclusive,
            time.min,
            timezone.utc,
        )


def add_months(
    value: date,
    amount: int,
) -> date:
    month_index = (
        value.year * 12
        + value.month
        - 1
        + amount
    )
    year, zero_based_month = divmod(
        month_index,
        12,
    )

    return date(
        year,
        zero_based_month + 1,
        1,
    )


def parse_month(
    value: str,
    field_label: str,
) -> date:
    try:
        return date.fromisoformat(
            f"{value}-01"
        )
    except ValueError as exc:
        raise ValueError(
            f"{field_label} ist ungültig."
        ) from exc


def month_label(value: date) -> str:
    return (
        f"{GERMAN_MONTH_NAMES[value.month - 1]} "
        f"{value.year}"
    )


def resolve_reporting_period(
    values: Mapping[str, str],
) -> ReportPeriod:
    today = date.today()
    current_month = today.replace(day=1)
    default_start_month = add_months(
        current_month,
        -2,
    )
    mode = str(
        values.get("period_mode") or "month"
    ).strip()
    month_value = str(
        values.get("month")
        or current_month.strftime("%Y-%m")
    ).strip()
    start_month_value = str(
        values.get("start_month")
        or default_start_month.strftime("%Y-%m")
    ).strip()
    end_month_value = str(
        values.get("end_month")
        or current_month.strftime("%Y-%m")
    ).strip()
    year_value = str(
        values.get("year")
        or today.year
    ).strip()

    if mode == "month":
        start_date = parse_month(
            month_value,
            "Der Monat",
        )
        end_date_exclusive = add_months(
            start_date,
            1,
        )
        month_value = start_date.strftime(
            "%Y-%m"
        )
        label = month_label(start_date)

    elif mode == "months":
        start_date = parse_month(
            start_month_value,
            "Der Startmonat",
        )
        end_month = parse_month(
            end_month_value,
            "Der Endmonat",
        )

        if end_month < start_date:
            raise ValueError(
                "Der Endmonat darf nicht vor dem "
                "Startmonat liegen."
            )

        end_date_exclusive = add_months(
            end_month,
            1,
        )
        start_month_value = start_date.strftime(
            "%Y-%m"
        )
        end_month_value = end_month.strftime(
            "%Y-%m"
        )
        label = (
            f"{month_label(start_date)} bis "
            f"{month_label(end_month)}"
        )

    elif mode == "year":
        try:
            selected_year = int(year_value)
        except ValueError as exc:
            raise ValueError(
                "Das Jahr ist ungültig."
            ) from exc

        if not 1900 <= selected_year <= 9998:
            raise ValueError(
                "Das Jahr ist ungültig."
            )

        start_date = date(
            selected_year,
            1,
            1,
        )
        end_date_exclusive = date(
            selected_year + 1,
            1,
            1,
        )
        year_value = str(selected_year)
        label = str(selected_year)

    else:
        raise ValueError(
            "Der ausgewählte Zeitraum ist ungültig."
        )

    return ReportPeriod(
        mode=mode,
        start_date=start_date,
        end_date_exclusive=(
            end_date_exclusive
        ),
        label=label,
        month_value=month_value,
        start_month_value=start_month_value,
        end_month_value=end_month_value,
        year_value=year_value,
    )


def build_service_position_report(
    period: ReportPeriod,
) -> dict[str, Any]:
    filters = (
        TimeEntry.service_date
        >= period.start_date,
        TimeEntry.service_date
        < period.end_date_exclusive,
    )

    raw_rows = (
        db.session.query(
            TimeTaskTemplate.id.label(
                "task_template_id"
            ),
            TimeTaskTemplate.code.label(
                "task_code"
            ),
            TimeTaskTemplate.title.label(
                "task_title"
            ),
            TimeTaskTemplate.suggested_minutes,
            TimeTaskTemplate.free_time_enabled,
            CraftActivity.name.label(
                "craft_name"
            ),
            func.count(
                TimeEntry.id
            ).label("booking_count"),
            func.count(
                distinct(
                    TimeEntry.commission_id
                )
            ).label("commission_count"),
            func.count(
                distinct(TimeEntry.user_id)
            ).label("user_count"),
            func.coalesce(
                func.sum(TimeEntry.minutes),
                0,
            ).label("total_minutes"),
            func.avg(
                TimeEntry.minutes
            ).label("average_minutes"),
        )
        .select_from(TimeEntry)
        .outerjoin(
            TimeTaskTemplate,
            TimeEntry.task_template_id
            == TimeTaskTemplate.id,
        )
        .outerjoin(
            CraftActivity,
            TimeTaskTemplate.craft_activity_id
            == CraftActivity.id,
        )
        .filter(*filters)
        .group_by(
            TimeTaskTemplate.id,
            TimeTaskTemplate.code,
            TimeTaskTemplate.title,
            TimeTaskTemplate.suggested_minutes,
            TimeTaskTemplate.free_time_enabled,
            CraftActivity.name,
        )
        .all()
    )

    rows = []

    for raw_row in raw_rows:
        booking_count = int(
            raw_row.booking_count or 0
        )
        total_minutes = int(
            raw_row.total_minutes or 0
        )

        rows.append({
            "task_template_id": (
                raw_row.task_template_id
            ),
            "code": (
                raw_row.task_code or "–"
            ),
            "title": (
                raw_row.task_title
                or "Ohne Leistungsposition"
            ),
            "craft_name": (
                raw_row.craft_name
                or "Ohne Gewerk"
            ),
            "free_time_enabled": bool(
                raw_row.free_time_enabled
            ),
            "suggested_minutes": int(
                raw_row.suggested_minutes or 0
            ),
            "booking_count": booking_count,
            "commission_count": int(
                raw_row.commission_count or 0
            ),
            "user_count": int(
                raw_row.user_count or 0
            ),
            "total_minutes": total_minutes,
            "average_minutes": int(
                round(
                    float(
                        raw_row.average_minutes
                        or 0
                    )
                )
            ),
        })

    rows.sort(
        key=lambda row: (
            -row["booking_count"],
            row["title"].lower(),
        )
    )

    raw_summary = (
        db.session.query(
            func.count(
                TimeEntry.id
            ).label("booking_count"),
            func.count(
                distinct(
                    TimeEntry.commission_id
                )
            ).label("commission_count"),
            func.count(
                distinct(TimeEntry.user_id)
            ).label("user_count"),
            func.coalesce(
                func.sum(TimeEntry.minutes),
                0,
            ).label("total_minutes"),
        )
        .filter(*filters)
        .one()
    )

    return {
        "rows": rows,
        "summary": {
            "position_count": len(rows),
            "booking_count": int(
                raw_summary.booking_count or 0
            ),
            "commission_count": int(
                raw_summary.commission_count or 0
            ),
            "user_count": int(
                raw_summary.user_count or 0
            ),
            "total_minutes": int(
                raw_summary.total_minutes or 0
            ),
        },
    }


def build_craftsman_commission_report(
    period: ReportPeriod,
) -> dict[str, Any]:
    report_date = func.coalesce(
        Commission.execution_from,
        Commission.release_date,
        Commission.recording_date,
        Commission.synced_at,
    )
    active_statuses = (
        CommissionLocalStatus
        .WAITING_FOR_ACCEPTANCE
        .value,
        CommissionLocalStatus.OPEN.value,
        CommissionLocalStatus
        .IN_PROGRESS
        .value,
    )
    review_statuses = (
        CommissionLocalStatus
        .COMPLETION_REVIEW
        .value,
        CommissionLocalStatus
        .TENANT_CHARGE_REVIEW
        .value,
    )

    raw_rows = (
        db.session.query(
            User.id.label("user_id"),
            User.display_name.label(
                "user_name"
            ),
            func.count(
                Commission.id
            ).label("commission_count"),
            func.sum(
                case(
                    (
                        Commission.local_status.in_(
                            active_statuses
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("active_count"),
            func.sum(
                case(
                    (
                        Commission.local_status.in_(
                            review_statuses
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("review_count"),
            func.sum(
                case(
                    (
                        Commission.local_status
                        == CommissionLocalStatus
                        .DONE
                        .value,
                        1,
                    ),
                    else_=0,
                )
            ).label("completed_count"),
            func.sum(
                case(
                    (
                        Commission
                        .completion_successful
                        .is_(True),
                        1,
                    ),
                    else_=0,
                )
            ).label("successful_count"),
            func.sum(
                case(
                    (
                        Commission
                        .completion_successful
                        .is_(False),
                        1,
                    ),
                    else_=0,
                )
            ).label("unsuccessful_count"),
            func.sum(
                case(
                    (
                        Commission
                        .is_high_priority
                        .is_(True),
                        1,
                    ),
                    else_=0,
                )
            ).label("priority_count"),
        )
        .select_from(Commission)
        .outerjoin(
            User,
            Commission.user_id == User.id,
        )
        .filter(
            Commission.local_status
            != CommissionLocalStatus
            .DELETED
            .value,
            report_date
            >= period.start_datetime,
            report_date
            < period.end_datetime_exclusive,
        )
        .group_by(
            User.id,
            User.display_name,
        )
        .all()
    )

    rows = [
        {
            "user_id": raw_row.user_id,
            "user_name": (
                raw_row.user_name
                or "Nicht zugewiesen"
            ),
            "commission_count": int(
                raw_row.commission_count or 0
            ),
            "active_count": int(
                raw_row.active_count or 0
            ),
            "review_count": int(
                raw_row.review_count or 0
            ),
            "completed_count": int(
                raw_row.completed_count or 0
            ),
            "successful_count": int(
                raw_row.successful_count or 0
            ),
            "unsuccessful_count": int(
                raw_row.unsuccessful_count or 0
            ),
            "priority_count": int(
                raw_row.priority_count or 0
            ),
        }
        for raw_row in raw_rows
    ]

    rows.sort(
        key=lambda row: (
            -row["commission_count"],
            row["user_name"].lower(),
        )
    )

    return {
        "rows": rows,
        "summary": {
            "commission_count": sum(
                row["commission_count"]
                for row in rows
            ),
            "craftsman_count": sum(
                1
                for row in rows
                if row["user_id"] is not None
            ),
            "active_count": sum(
                row["active_count"]
                for row in rows
            ),
            "review_count": sum(
                row["review_count"]
                for row in rows
            ),
            "completed_count": sum(
                row["completed_count"]
                for row in rows
            ),
            "successful_count": sum(
                row["successful_count"]
                for row in rows
            ),
            "unsuccessful_count": sum(
                row["unsuccessful_count"]
                for row in rows
            ),
            "priority_count": sum(
                row["priority_count"]
                for row in rows
            ),
        },
    }


def cancelled_inventory_movement_ids() -> set[int]:
    cancelled_ids = set()
    notes = (
        db.session.query(
            InventoryMovement.note
        )
        .filter(
            InventoryMovement.movement_type
            == InventoryMovementType
            .CANCELLATION
            .value,
            InventoryMovement.note.isnot(None),
        )
        .all()
    )

    for note, in notes:
        match = re.match(
            r"Storno zu Buchung #(\d+)",
            note or "",
        )

        if match:
            cancelled_ids.add(
                int(match.group(1))
            )

    return cancelled_ids


def build_installed_material_report(
    period: ReportPeriod,
) -> dict[str, Any]:
    cancelled_ids = (
        cancelled_inventory_movement_ids()
    )
    movements = (
        InventoryMovement.query
        .options(
            joinedload(
                InventoryMovement.item
            )
            .joinedload(
                InventoryItem.item_type
            )
            .joinedload(
                InventoryItemType.group
            ),
            joinedload(
                InventoryMovement
                .item_type_snapshot
            )
            .joinedload(
                InventoryItemType.group
            ),
            joinedload(
                InventoryMovement
                .item_group_snapshot
            ),
        )
        .filter(
            InventoryMovement.movement_type
            == InventoryMovementType
            .WITHDRAWAL
            .value,
            InventoryMovement.usage_status
            == MaterialUsageStatus
            .INSTALLED
            .value,
            InventoryMovement.occurred_at
            >= period.start_datetime,
            InventoryMovement.occurred_at
            < period.end_datetime_exclusive,
        )
        .order_by(
            InventoryMovement.occurred_at.asc(),
            InventoryMovement.id.asc(),
        )
        .all()
    )

    grouped_rows = {}
    commission_ids = set()
    user_ids = set()
    item_ids = set()
    booking_count = 0

    for movement in movements:
        if movement.id in cancelled_ids:
            continue

        item = movement.item
        item_type = (
            movement.item_type_snapshot
            or item.item_type
        )
        item_group = (
            movement.item_group_snapshot
            or (
                item_type.group
                if item_type is not None
                else None
            )
        )
        key = (
            item_group.id
            if item_group is not None
            else None,
            item_type.id
            if item_type is not None
            else None,
            item.id,
            item.unit,
        )
        row = grouped_rows.setdefault(
            key,
            {
                "group_name": (
                    item_group.name
                    if item_group is not None
                    else "Ohne Artikelgruppe"
                ),
                "type_name": (
                    item_type.name
                    if item_type is not None
                    else "Ohne Artikelart"
                ),
                "item_number": item.item_number,
                "item_name": item.name,
                "unit": item.unit,
                "quantity": 0.0,
                "booking_count": 0,
                "commission_ids": set(),
                "user_ids": set(),
                "serial_number_count": 0,
            },
        )

        row["quantity"] += float(
            movement.quantity
        )
        row["booking_count"] += 1

        if movement.commission_id is not None:
            row["commission_ids"].add(
                movement.commission_id
            )
            commission_ids.add(
                movement.commission_id
            )

        if movement.user_id is not None:
            row["user_ids"].add(
                movement.user_id
            )
            user_ids.add(movement.user_id)

        if movement.serial_number:
            row["serial_number_count"] += 1

        item_ids.add(item.id)
        booking_count += 1

    rows = []

    for row in grouped_rows.values():
        rows.append({
            "group_name": row["group_name"],
            "type_name": row["type_name"],
            "item_number": row["item_number"],
            "item_name": row["item_name"],
            "unit": row["unit"],
            "quantity": row["quantity"],
            "booking_count": (
                row["booking_count"]
            ),
            "commission_count": len(
                row["commission_ids"]
            ),
            "user_count": len(
                row["user_ids"]
            ),
            "serial_number_count": (
                row["serial_number_count"]
            ),
        })

    rows.sort(
        key=lambda row: (
            row["group_name"].lower(),
            row["type_name"].lower(),
            row["item_name"].lower(),
        )
    )

    return {
        "rows": rows,
        "summary": {
            "booking_count": booking_count,
            "commission_count": len(
                commission_ids
            ),
            "user_count": len(user_ids),
            "item_count": len(item_ids),
        },
    }