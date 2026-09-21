from __future__ import annotations

from hashlib import sha256
import json
import re

from sqlalchemy import or_

from .extensions import db
from .models import (
    Commission,
    CommissionLocalStatus,
    CommissionPerformanceReview,
    InventoryMovement,
    InventoryMovementType,
    MaterialUsageStatus,
    TimeEntry,
    TimeReviewStatus,
    TimeTaskTemplate,
    User,
    utc_now,
)
from .settings import SettingKeys, settings

from .inventory_service import (
    movement_item_type_id,
)


FINAL_STATUSES = {
    CommissionLocalStatus.COMPLETION_REVIEW.value,
    CommissionLocalStatus.TENANT_CHARGE_REVIEW.value,
    CommissionLocalStatus.DONE.value,
}


def commission_tasks(
    commission: Commission,
) -> list[TimeTaskTemplate]:
    task_ids = {
        entry.task_template_id
        for entry in commission.time_entries
        if entry.task_template_id
    }
    task_ids.update(
        movement.task_template_id
        for movement in commission.inventory_movements
        if movement.task_template_id
    )

    catalogue_ids = {
        position.service_catalog_erp_id
        for position in commission.positions
        if position.service_catalog_erp_id
    }
    codes = {
        position.code.strip()
        for position in commission.positions
        if position.code
        and position.code.strip()
    }
    conditions = []

    if task_ids:
        conditions.append(
            TimeTaskTemplate.id.in_(task_ids)
        )

    if catalogue_ids:
        conditions.append(
            TimeTaskTemplate
            .erp_service_catalogue_id
            .in_(catalogue_ids)
        )

    if codes:
        conditions.extend([
            TimeTaskTemplate.code.in_(codes),
            TimeTaskTemplate.erp_idnum.in_(codes),
        ])

    if not conditions:
        return []

    return (
        TimeTaskTemplate.query
        .filter(or_(*conditions))
        .order_by(
            TimeTaskTemplate.title.asc(),
            TimeTaskTemplate.code.asc(),
        )
        .all()
    )


def active_withdrawals(
    commission: Commission,
) -> list[InventoryMovement]:
    cancelled_ids = set()

    for movement in commission.inventory_movements:
        if (
            movement.movement_type
            != InventoryMovementType.CANCELLATION.value
        ):
            continue

        match = re.match(
            r"Storno zu Buchung #(\d+)",
            movement.note or "",
        )

        if match:
            cancelled_ids.add(
                int(match.group(1))
            )

    return [
        movement
        for movement
        in commission.inventory_movements
        if (
            movement.movement_type
            == InventoryMovementType.WITHDRAWAL.value
            and movement.id not in cancelled_ids
        )
    ]


def active_installed_material_movements(
    commission: Commission,
) -> list[InventoryMovement]:
    return [
        movement
        for movement in active_withdrawals(
            commission
        )
        if (
            movement.usage_status
            == MaterialUsageStatus.INSTALLED.value
        )
    ]


def commission_requires_material_confirmation(
    commission: Commission,
) -> bool:
    has_required_material = any(
        expected.required
        for task in commission_tasks(commission)
        for expected in task.expected_materials
    )

    return (
        has_required_material
        and not active_withdrawals(commission)
    )


def review_row(
    commission: Commission,
    task: TimeTaskTemplate | None,
    scope_key: str,
    entries: list[TimeEntry],
    movements: list[InventoryMovement],
) -> dict:
    actual_minutes = sum(
        entry.minutes
        for entry in entries
    )
    expected_minutes = (
        int(task.suggested_minutes or 0)
        if task
        else 0
    )
    deviation_minutes = (
        actual_minutes - expected_minutes
    )
    deviation_percent = (
        abs(deviation_minutes)
        / expected_minutes
        * 100
        if expected_minutes > 0
        else 0.0
    )

    has_time_context = bool(entries) or (
        commission.local_status
        in FINAL_STATUSES
    )
    shorter_is_ignored = (
        task is not None
        and deviation_minutes < 0
        and not task.review_when_shorter
    )
    time_deviation = (
        task is None
        and bool(entries)
    ) or (
        task is not None
        and expected_minutes > 0
        and has_time_context
        and not shorter_is_ignored
        and (
            abs(deviation_minutes)
            > settings.get(
                SettingKeys.TIME_DEVIATION_MINUTES
            )
            or deviation_percent
            > settings.get(
                SettingKeys.TIME_DEVIATION_PERCENT
            )
        )
    )

    installed_movements = [
        movement
        for movement in movements
        if (
                movement.usage_status
                == MaterialUsageStatus.INSTALLED.value
        )
    ]
    actual_by_item = {}
    actual_by_type = {}

    for movement in installed_movements:
        actual_by_item[
            movement.item_id
        ] = (
                actual_by_item.get(
                    movement.item_id,
                    0.0,
                )
                + float(movement.quantity)
        )

        item_type_id = movement_item_type_id(
            movement
        )

        if item_type_id is not None:
            actual_by_type[
                item_type_id
            ] = (
                    actual_by_type.get(
                        item_type_id,
                        0.0,
                    )
                    + float(movement.quantity)
            )

    material_lines = []
    expected_item_ids = set()
    expected_type_ids = set()

    if task:
        for expected in task.expected_materials:
            if expected.item_id is not None:
                expected_item_ids.add(
                    expected.item_id
                )
                actual_quantity = (
                    actual_by_item.get(
                        expected.item_id,
                        0.0,
                    )
                )
                label = expected.item.name
                unit = expected.item.unit
            else:
                expected_type_ids.add(
                    expected.item_type_id
                )
                actual_quantity = (
                    actual_by_type.get(
                        expected.item_type_id,
                        0.0,
                    )
                )
                label = (
                    f"{expected.item_type.group.name} "
                    f"· {expected.item_type.name}"
                )
                unit = expected.item_type.unit

            quantity_differs = (
                    abs(
                        actual_quantity
                        - float(expected.quantity)
                    )
                    > 0.0001
            )
            deviates = (
                quantity_differs
                if expected.required
                else (
                        actual_quantity > 0.0001
                        and quantity_differs
                )
            )

            material_lines.append({
                "label": label,
                "unit": unit,
                "item": expected.item,
                "item_type": expected.item_type,
                "expected_quantity": float(
                    expected.quantity
                ),
                "actual_quantity": actual_quantity,
                "required": expected.required,
                "deviates": deviates,
            })

    for item_id, actual_quantity in (
            actual_by_item.items()
    ):
        if item_id in expected_item_ids:
            continue

        movement = next(
            movement
            for movement in installed_movements
            if movement.item_id == item_id
        )
        item_type_id = movement_item_type_id(
            movement
        )

        if item_type_id in expected_type_ids:
            continue

        material_lines.append({
            "label": movement.item.name,
            "unit": movement.item.unit,
            "item": movement.item,
            "item_type": movement.item.item_type,
            "expected_quantity": 0.0,
            "actual_quantity": actual_quantity,
            "required": False,
            "deviates": True,
        })

    material_deviation = (
        task is None
        and bool(movements)
    ) or any(
        line["deviates"]
        for line in material_lines
    ) or any(
        movement.usage_status
        != MaterialUsageStatus.INSTALLED.value
        for movement in movements
    )
    needs_review = (
        time_deviation
        or material_deviation
    )

    payload = {
        "scope_key": scope_key,
        "task": (
            {
                "id": task.id,
                "suggested_minutes": (
                    task.suggested_minutes
                ),
                "review_when_shorter": (
                    task.review_when_shorter
                ),
                "expected_materials": [
                    {
                        "item_id": expected.item_id,
                        "item_type_id": expected.item_type_id,
                        "quantity": expected.quantity,
                        "required": expected.required,
                    }
                    for expected
                    in task.expected_materials
                ],
            }
            if task
            else None
        ),
        "entries": [
            {
                "id": entry.id,
                "minutes": entry.minutes,
            }
            for entry in sorted(
                entries,
                key=lambda item: item.id,
            )
        ],
        "movements": [
            {
                "id": movement.id,
                "item_id": movement.item_id,
                "item_type_id": movement_item_type_id(
                    movement
                ),
                "quantity": movement.quantity,
                "usage_status": (
                    movement.usage_status
                ),
            }
            for movement in sorted(
                movements,
                key=lambda item: item.id,
            )
        ],
    }
    fingerprint = sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    decision = (
        CommissionPerformanceReview.query
        .filter_by(
            commission_id=commission.id,
            scope_key=scope_key,
        )
        .one_or_none()
    )
    status = TimeReviewStatus.OK.value

    if needs_review:
        if (
            decision
            and decision.fingerprint
            == fingerprint
        ):
            status = decision.status
        else:
            status = (
                TimeReviewStatus
                .NEEDS_REVIEW.value
            )

    dates = [
        entry.created_at
        for entry in entries
    ]
    dates.extend(
        movement.occurred_at
        for movement in movements
    )

    return {
        "commission": commission,
        "task_template": task,
        "scope_key": scope_key,
        "entries": entries,
        "movements": movements,
        "material_lines": material_lines,
        "actual_minutes": actual_minutes,
        "expected_minutes": expected_minutes,
        "deviation_minutes": deviation_minutes,
        "deviation_percent": deviation_percent,
        "time_deviation": time_deviation,
        "material_deviation": material_deviation,
        "needs_review": needs_review,
        "fingerprint": fingerprint,
        "status": status,
        "decision": decision,
        "latest_at": (
            max(
                dates,
                key=lambda value: value.timestamp(),
            )
            if dates
            else (
                commission.completed_at
                or commission.synced_at
            )
        ),
    }


def performance_review_rows() -> list[dict]:
    commission_ids = {
        value
        for value, in (
            db.session.query(
                TimeEntry.commission_id
            )
            .distinct()
            .all()
        )
    }
    commission_ids.update(
        value
        for value, in (
            db.session.query(
                InventoryMovement.commission_id
            )
            .filter(
                InventoryMovement
                .commission_id.isnot(None)
            )
            .distinct()
            .all()
        )
    )

    if not commission_ids:
        return []

    commissions = (
        Commission.query
        .filter(
            Commission.id.in_(commission_ids),
            Commission.deleted_at.is_(None),
        )
        .all()
    )
    rows = []

    for commission in commissions:
        tasks = commission_tasks(commission)
        withdrawals = active_withdrawals(
            commission
        )

        for task in tasks:
            entries = [
                entry
                for entry in commission.time_entries
                if entry.task_template_id == task.id
            ]
            movements = [
                movement
                for movement in withdrawals
                if movement.task_template_id == task.id
            ]
            include_empty_task = (
                commission.local_status
                in FINAL_STATUSES
                and bool(task.expected_materials)
            )

            if (
                not entries
                and not movements
                and not include_empty_task
            ):
                continue

            rows.append(
                review_row(
                    commission,
                    task,
                    f"task-{task.id}",
                    entries,
                    movements,
                )
            )

        unassigned_entries = [
            entry
            for entry in commission.time_entries
            if entry.task_template_id is None
        ]
        unassigned_movements = [
            movement
            for movement in withdrawals
            if movement.task_template_id is None
        ]

        if (
            unassigned_entries
            or unassigned_movements
        ):
            rows.append(
                review_row(
                    commission,
                    None,
                    "unassigned",
                    unassigned_entries,
                    unassigned_movements,
                )
            )

    return sorted(
        rows,
        key=lambda row: (
            row["latest_at"].timestamp()
            if row["latest_at"]
            else 0
        ),
        reverse=True,
    )


def review_performance_scope(
    commission_id: int,
    scope_key: str,
    decision_name: str,
    reviewer: User,
) -> CommissionPerformanceReview:
    if decision_name not in {
        "approve",
        "reject",
    }:
        raise ValueError(
            "Ungültige Prüfentscheidung."
        )

    commission = db.session.get(
        Commission,
        commission_id,
    )

    if commission is None:
        raise ValueError(
            "Der Auftrag wurde nicht gefunden."
        )

    tasks = {
        f"task-{task.id}": task
        for task in commission_tasks(
            commission
        )
    }
    task = tasks.get(scope_key)

    if (
        task is None
        and scope_key != "unassigned"
    ):
        raise ValueError(
            "Die Leistungsposition wurde nicht gefunden."
        )

    withdrawals = active_withdrawals(
        commission
    )
    entries = [
        entry
        for entry in commission.time_entries
        if (
            entry.task_template_id
            == (task.id if task else None)
        )
    ]
    movements = [
        movement
        for movement in withdrawals
        if (
            movement.task_template_id
            == (task.id if task else None)
        )
    ]
    current = review_row(
        commission,
        task,
        scope_key,
        entries,
        movements,
    )

    if not current["needs_review"]:
        raise ValueError(
            "Für diese Position ist keine Prüfung erforderlich."
        )

    item = (
        CommissionPerformanceReview.query
        .filter_by(
            commission_id=commission.id,
            scope_key=scope_key,
        )
        .one_or_none()
    )

    if item is None:
        item = CommissionPerformanceReview(
            commission=commission,
            task_template=task,
            scope_key=scope_key,
        )
        db.session.add(item)

    item.fingerprint = current["fingerprint"]
    item.status = (
        TimeReviewStatus.APPROVED.value
        if decision_name == "approve"
        else TimeReviewStatus.REJECTED.value
    )
    item.reviewed_by = reviewer
    item.reviewed_at = utc_now()

    return item