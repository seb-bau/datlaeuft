from __future__ import annotations

from contextlib import closing
from pathlib import Path

from openpyxl import load_workbook
from sqlalchemy.orm import selectinload
from xlrd import XL_CELL_BOOLEAN, XL_CELL_DATE, XL_CELL_ERROR, open_workbook

from .audit import audit
from .models import CraftActivity, ExternalCraftsman


def _is_blank(value) -> bool:
    return value is None or isinstance(value, str) and not value.strip()


def _local_id(value, line_number: int, column: str) -> int:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    if not text.isascii() or not text.isdecimal() or int(text) <= 0:
        raise ValueError(
            f"Zeile {line_number}, Spalte {column}: "
            "Eine positive lokale ID als ganze Zahl ist erforderlich."
        )
    return int(text)


def _excel_rows(filename: Path):
    extension = filename.suffix.lower()
    if extension == ".xlsx":
        workbook = load_workbook(
            filename, read_only=True, data_only=False, keep_links=False
        )
        try:
            sheet = workbook.worksheets[0]
            sheet.reset_dimensions()
            for line_number, cells in enumerate(sheet.iter_rows(), start=1):
                if any(cell.data_type == "f" for cell in cells):
                    raise ValueError(
                        f"Zeile {line_number}: Bitte feste IDs statt Formeln verwenden."
                    )
                yield line_number, [cell.value for cell in cells]
        finally:
            workbook.close()
    elif extension == ".xls":
        workbook = open_workbook(str(filename), on_demand=True)
        try:
            sheet = workbook.sheet_by_index(0)
            for index in range(sheet.nrows):
                cells = sheet.row(index)
                if any(
                    cell.ctype in (XL_CELL_BOOLEAN, XL_CELL_DATE, XL_CELL_ERROR)
                    for cell in cells
                ):
                    raise ValueError(
                        f"Zeile {index + 1}: Bitte IDs als ganze Zahlen oder Text angeben."
                    )
                yield index + 1, [cell.value for cell in cells]
        finally:
            workbook.release_resources()
    else:
        raise ValueError("Bitte eine XLSX- oder XLS-Datei angeben.")


def import_external_craftsman_crafts(
    filename: Path,
    *,
    skip_header: bool = False,
    replace: bool = False,
) -> tuple[int, int]:
    assignments: dict[int, set[int]] = {}
    with closing(_excel_rows(filename)) as rows:
        for line_number, values in rows:
            if skip_header and line_number == 1:
                continue
            if all(_is_blank(value) for value in values):
                continue
            if len(values) < 2 or any(
                not _is_blank(value) for value in values[2:]
            ):
                raise ValueError(
                    f"Zeile {line_number}: Genau zwei Spalten A und B sind erforderlich."
                )
            craftsman_id = _local_id(values[0], line_number, "A")
            activity_id = _local_id(values[1], line_number, "B")
            assignments.setdefault(craftsman_id, set()).add(activity_id)

    if not assignments:
        raise ValueError("Die Datei enthält keine Gewerkezuordnungen.")

    craftsmen = {
        item.id: item
        for item in ExternalCraftsman.query
        .options(selectinload(ExternalCraftsman.craft_activities))
        .filter(ExternalCraftsman.id.in_(sorted(assignments)))
        .all()
    }
    activity_ids = set().union(*assignments.values())
    activities = {
        item.id: item
        for item in CraftActivity.query
        .filter(CraftActivity.id.in_(sorted(activity_ids)))
        .all()
    }
    missing_craftsman_ids = assignments.keys() - craftsmen.keys()
    missing_activity_ids = activity_ids - activities.keys()
    if missing_craftsman_ids:
        raise ValueError(
            "Lokale Handwerker-IDs nicht verfügbar: "
            + ", ".join(map(str, sorted(missing_craftsman_ids)))
        )
    if missing_activity_ids:
        raise ValueError(
            "Lokale Gewerke-IDs nicht verfügbar: "
            + ", ".join(map(str, sorted(missing_activity_ids)))
        )

    changed = 0
    for craftsman_id, imported_ids in assignments.items():
        craftsman = craftsmen[craftsman_id]
        existing_activities = {
            activity.id: activity for activity in craftsman.craft_activities
        }
        previous_ids = set(existing_activities)
        selected_ids = imported_ids if replace else previous_ids | imported_ids
        if selected_ids == previous_ids:
            continue
        craftsman.craft_activities = [
            activities[activity_id]
            if activity_id in imported_ids
            else existing_activities[activity_id]
            for activity_id in sorted(selected_ids)
        ]
        audit(
            "external_craftsman.craft_activities.update",
            "external_craftsman",
            craftsman.id,
            metadata={
                "previous_craft_activity_ids": sorted(previous_ids),
                "craft_activity_ids": sorted(selected_ids),
                "source": "excel",
            },
        )
        changed += 1

    return len(assignments), changed