from __future__ import annotations

import csv
from collections import defaultdict
from io import BytesIO, StringIO
from pathlib import Path
from zipfile import ZipFile

from openpyxl import load_workbook
from xlrd import open_workbook

from .models import WowiBuilding, WowiEconomicUnit, WowiUseUnit


MAX_IMPORT_BYTES = 5 * 1024 * 1024
MAX_IMPORT_ROWS = 5000
OBJECT_TYPES = {
    "wirtschaftseinheit": "economic_unit",
    "gebäude": "building",
    "gebaeude": "building",
    "nutzungseinheit": "use_unit",
}


def _read_import_rows(filename: str, content: bytes):
    extension = Path(filename).suffix.lower()
    if extension == ".csv":
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("cp1252")
        try:
            dialect = csv.Sniffer().sniff(
                text[:8192],
                delimiters=";,\t",
            )
        except csv.Error:
            dialect = csv.excel()
            dialect.delimiter = ";"
        yield from csv.reader(
            StringIO(text, newline=""),
            dialect,
            strict=True,
        )
    elif extension == ".xlsx":
        with ZipFile(BytesIO(content)) as archive:
            if (
                sum(item.file_size for item in archive.infolist())
                > 64 * 1024 * 1024
            ):
                raise ValueError("Die entpackte Excel-Datei ist zu groß.")
        workbook = load_workbook(
            BytesIO(content),
            read_only=True,
            keep_links=False,
        )
        try:
            sheet = workbook.worksheets[0]
            sheet.reset_dimensions()
            for cells in sheet.iter_rows(max_col=3):
                if any(cell.data_type == "f" for cell in cells):
                    raise ValueError(
                        "Bitte feste Werte statt Excel-Formeln verwenden."
                    )
                yield [cell.value for cell in cells]
        finally:
            workbook.close()
    elif extension == ".xls":
        workbook = open_workbook(
            file_contents=content,
            on_demand=True,
        )
        try:
            sheet = workbook.sheet_by_index(0)
            for index in range(sheet.nrows):
                yield sheet.row_values(index)
        finally:
            workbook.release_resources()
    else:
        raise ValueError(
            "Bitte eine CSV-, XLSX- oder XLS-Datei auswählen."
        )


def _import_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not value.is_integer():
            raise ValueError(
                "Bitte IdNum als Text oder ganze Zahl angeben."
            )
        return str(int(value))
    raise ValueError(
        "Bitte Textwerte für Objekttyp und IdNum verwenden."
    )


def parse_safety_inspection_import(filename: str, content: bytes):
    if not content or len(content) > MAX_IMPORT_BYTES:
        raise ValueError("Die Datei ist leer oder größer als 5 MB.")

    rows = _read_import_rows(filename, content)
    records = []
    try:
        for line_number, values in enumerate(rows, start=1):
            if line_number > MAX_IMPORT_ROWS + 1:
                raise ValueError(
                    "Es dürfen höchstens 5000 Zeilen importiert werden."
                )

            values = [_import_text(value) for value in values]
            while values and not values[-1]:
                values.pop()

            if line_number == 1:
                if [value.casefold() for value in values] != [
                    "objekttyp",
                    "idnum",
                ]:
                    raise ValueError(
                        "Die Kopfzeile muss Objekttyp und IdNum enthalten."
                    )
                continue

            if not values:
                continue
            if len(values) != 2 or not values[1]:
                raise ValueError(
                    f"Zeile {line_number}: Objekttyp oder IdNum fehlt."
                )

            object_type = OBJECT_TYPES.get(values[0].casefold())
            if object_type is None:
                raise ValueError(
                    f"Zeile {line_number}: Unbekannter Objekttyp."
                )
            records.append((line_number, object_type, values[1]))
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(
            "Die Datei konnte nicht gelesen werden."
        ) from exc
    finally:
        rows.close()

    if not records:
        raise ValueError("Die Datei enthält keine Objekte.")
    return records


def resolve_safety_inspection_import(filename: str, content: bytes):
    records = parse_safety_inspection_import(filename, content)
    models = {
        "economic_unit": WowiEconomicUnit,
        "building": WowiBuilding,
        "use_unit": WowiUseUnit,
    }
    matches = defaultdict(list)

    for object_type, model in models.items():
        numbers = sorted({
            number
            for _, kind, number in records
            if kind == object_type
        })
        for offset in range(0, len(numbers), 500):
            items = model.query.filter(
                model.active.is_(True),
                model.id_num.in_(numbers[offset:offset + 500]),
            ).all()
            for item in items:
                matches[(object_type, item.id_num)].append(item)

    targets = []
    seen = set()
    for line_number, object_type, id_num in records:
        items = matches[(object_type, id_num)]
        if len(items) != 1:
            reason = (
                "nicht gefunden oder inaktiv"
                if not items
                else "nicht eindeutig"
            )
            raise ValueError(
                f"Zeile {line_number}: IdNum {id_num} ist {reason}."
            )

        item = items[0]
        key = (object_type, item.erp_id)
        if key in seen:
            continue
        seen.add(key)

        type_label = {
            "economic_unit": "Wirtschaftseinheit",
            "building": "Gebäude",
            "use_unit": "Nutzungseinheit",
        }[object_type]
        details = (
            id_num,
            getattr(item, "name", None),
            getattr(item, "address", None),
            getattr(item, "description_of_position", None),
        )
        targets.append({
            "object_type": object_type,
            "object_erp_id": item.erp_id,
            "object_label": (
                type_label
                + ": "
                + " · ".join(value for value in details if value)
            ),
        })

    return {
        "items": targets,
        "duplicates": len(records) - len(targets),
    }
