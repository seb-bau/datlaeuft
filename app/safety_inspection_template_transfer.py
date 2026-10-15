from __future__ import annotations

import csv
import json
from io import BytesIO, StringIO
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font


MAX_IMPORT_BYTES = 5 * 1024 * 1024
MAX_ROWS = 2000
FLAG_FIELDS = (
    ("required", "Pflichtfeld", True),
    ("allow_not_present", "Nicht vorhanden zulassen", True),
    ("photo_required_no_defect", "Foto ohne Mangel", False),
    ("photo_required_not_present", "Foto nicht vorhanden", False),
    ("photo_required_defect_info", "Foto Mangel Info", False),
    ("photo_required_defect_urgent", "Foto Mangel dringend", True),
)
COLUMNS = (
    "Typ", "Bezeichnung", "Beschreibung", "Hinweis", "Rechtsgrundlage", "Aktiv",
    *(label for _, label, _ in FLAG_FIELDS),
    "Katalogposition",
)


class TemplateTransferError(ValueError):
    pass


def _text(value, limit, label):
    value = str(value if value is not None else "").strip()
    if len(value) > limit or any(ord(char) < 32 and char not in "\t\n\r" for char in value):
        raise TemplateTransferError(f"{label}: Text zu lang oder ungültige Steuerzeichen.")
    return value


def _boolean(value, default, label):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    normalized = str(value).strip().casefold()
    if normalized in {"ja", "true", "1"}:
        return True
    if normalized in {"nein", "false", "0"}:
        return False
    raise TemplateTransferError(f"{label}: Bitte Ja oder Nein verwenden.")


def normalize_template_rows(payload):
    if not isinstance(payload, list) or len(payload) > MAX_ROWS:
        raise TemplateTransferError(f"Ein Plan darf höchstens {MAX_ROWS} Zeilen enthalten.")
    rows = []
    keys = set()
    for number, value in enumerate(payload, 1):
        if not isinstance(value, dict):
            raise TemplateTransferError(f"Zeile {number}: Ungültiger Eintrag.")
        row_type = value.get("row_type", "check")
        if not isinstance(row_type, str) or row_type not in {"check", "heading"}:
            raise TemplateTransferError(f"Zeile {number}: Ungültiger Zeilentyp.")
        title = _text(value.get("title"), 255, f"Zeile {number}")
        if not title:
            raise TemplateTransferError(f"Zeile {number}: Eine Bezeichnung ist erforderlich.")
        stable_key = str(value.get("stable_key") or uuid4()).strip()
        if not stable_key or len(stable_key) > 36 or stable_key in keys:
            raise TemplateTransferError(f"Zeile {number}: Ungültiger oder doppelter Zeilenschlüssel.")
        keys.add(stable_key)
        row = {
            "stable_key": stable_key, "row_type": row_type, "title": title,
            "description": _text(value.get("description"), 10000, "Beschreibung"),
            "help_text": _text(value.get("help_text"), 10000, "Hinweis"),
            "catalog_position": _text(value.get("catalog_position"), 16,
                                      "Katalogposition") if row_type == "check" else "",
        }
        for key, label, default in FLAG_FIELDS:
            row[key] = _boolean(value.get(key), default, label) if row_type == "check" else False
        rows.append(row)
    if len(json.dumps(rows, ensure_ascii=False).encode("utf-8")) > 1024 * 1024:
        raise TemplateTransferError("Der Planinhalt ist zu umfangreich; maximal 1 MB ist möglich.")
    return rows


def template_item_rows(version):
    return [
        {
            "stable_key": item.stable_key, "row_type": item.row_type,
            "title": item.title, "description": item.description or "",
            "help_text": item.help_text or "",
            "catalog_position": item.catalog_position or "",
            **{key: getattr(item, key) for key, _, _ in FLAG_FIELDS},
        }
        for item in sorted(version.items, key=lambda item: item.position_index)
    ]


def normalize_plan_data(value):
    if not isinstance(value, dict):
        raise TemplateTransferError("Die Planangaben sind ungültig.")
    name = _text(value.get("name"), 255, "Planbezeichnung")
    if not name:
        raise TemplateTransferError("Der Plan benötigt eine Bezeichnung.")
    return {
        "name": name,
        "description": _text(value.get("description"), 10000, "Planbeschreibung"),
        "legal_reference": _text(value.get("legal_reference"), 10000, "Rechtsgrundlage"),
        "change_note": _text(value.get("change_note"), 10000, "Änderungshinweis"),
        "active": _boolean(value.get("active"), True, "Aktiv"),
    }


def _needs_csv_escape(value):
    return bool(value) and (value[0] in "'=+-@" or value[0].isspace())


def _csv_encode(value):
    value = str(value)
    return "'" + value if _needs_csv_escape(value) else value


def _csv_decode(value):
    if isinstance(value, str) and value.startswith("'") and _needs_csv_escape(value[1:]):
        return value[1:]
    return value


def export_template_file(file_format, plan, payload):
    plan = normalize_plan_data(plan)
    rows = normalize_template_rows(payload)
    table = [list(COLUMNS), [
        "Plan", plan["name"], plan["description"], plan["change_note"],
        plan["legal_reference"], "Ja" if plan["active"] else "Nein", *([""] * 6), "",
    ]]
    for row in rows:
        table.append([
            "Überschrift" if row["row_type"] == "heading" else "Prüfpunkt",
            row["title"], row["description"], row["help_text"], "", "",
            *(
                ("Ja" if row[key] else "Nein") if row["row_type"] == "check" else ""
                for key, _, _ in FLAG_FIELDS
            ),
            row["catalog_position"],
        ])
    if file_format == "csv":
        stream = StringIO(newline="")
        csv.writer(stream, delimiter=";", lineterminator="\r\n").writerows(
            [[_csv_encode(value) for value in row] for row in table]
        )
        return stream.getvalue().encode("utf-8-sig"), "text/csv"
    if file_format != "xlsx":
        raise TemplateTransferError("Bitte CSV oder XLSX wählen.")
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Prüfdetailplan"
    for row_number, row in enumerate(table, 1):
        for column_number, value in enumerate(row, 1):
            cell = sheet.cell(row_number, column_number, str(value))
            cell.data_type = "s"
            if column_number == len(COLUMNS):
                cell.number_format = "@"
            if row_number == 1 or row[0] == "Überschrift":
                cell.font = Font(bold=True)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for column in "ABCDEFGHIJKLM":
        sheet.column_dimensions[column].width = 25
    sheet.column_dimensions["B"].width = 45
    sheet.column_dimensions["C"].width = 55
    sheet.column_dimensions["M"].width = 18
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _parse_table(table, csv_values=False):
    try:
        header = next(table)
    except StopIteration as exc:
        raise TemplateTransferError("Die Datei ist leer.") from exc
    names = [str(value or "").strip().casefold() for value in header]
    populated_names = [name for name in names if name]
    if (
        len(populated_names) != len(set(populated_names))
        or not {"typ", "bezeichnung"}.issubset(populated_names)
        or any(name not in {column.casefold() for column in COLUMNS} for name in populated_names)
    ):
        raise TemplateTransferError("Ungültige Spalten. Mindestens Typ und Bezeichnung werden benötigt.")
    items = []
    plan = None
    for row_number, values in enumerate(table, 2):
        if row_number > MAX_ROWS + 2:
            raise TemplateTransferError(f"Die Datei darf höchstens {MAX_ROWS} Inhaltszeilen enthalten.")
        if not any(value is not None and value != "" for value in values):
            continue
        if any(value is not None and value != "" for value in values[len(names):]):
            raise TemplateTransferError(f"Zeile {row_number}: Mehr Werte als Spaltenüberschriften.")
        if any(index < len(values) and values[index] not in (None, "") for index, name in enumerate(names) if not name):
            raise TemplateTransferError(f"Zeile {row_number}: Ein Wert steht unter einer leeren Spaltenüberschrift.")
        cells = {name: values[index] if index < len(values) else None for index, name in enumerate(names) if name}
        if csv_values:
            cells = {key: _csv_decode(value) for key, value in cells.items()}
        row_type = str(cells.get("typ") or "").strip().casefold()
        if row_type == "plan":
            if plan is not None:
                raise TemplateTransferError("Die Datei darf nur eine Planzeile enthalten.")
            plan = normalize_plan_data({
                "name": cells.get("bezeichnung"), "description": cells.get("beschreibung"),
                "change_note": cells.get("hinweis"), "legal_reference": cells.get("rechtsgrundlage"),
                "active": cells.get("aktiv"),
            })
            continue
        kinds = {"überschrift": "heading", "ueberschrift": "heading", "prüfpunkt": "check", "pruefpunkt": "check"}
        if row_type not in kinds:
            raise TemplateTransferError(f"Zeile {row_number}: Typ muss Plan, Überschrift oder Prüfpunkt sein.")
        items.append({
            "row_type": kinds[row_type], "title": cells.get("bezeichnung"),
            "description": cells.get("beschreibung"), "help_text": cells.get("hinweis"),
            "catalog_position": cells.get("katalogposition"),
            **{key: cells.get(label.casefold()) for key, label, _ in FLAG_FIELDS},
        })
    if not items and plan is None:
        raise TemplateTransferError("Die Datei enthält keinen Planinhalt.")
    return {"plan": plan, "items": normalize_template_rows(items)}


def import_template_file(filename, content):
    if not content or len(content) > MAX_IMPORT_BYTES:
        raise TemplateTransferError("Die Datei ist leer oder größer als 5 MB.")
    suffix = Path(filename).suffix.casefold()
    if suffix == ".csv":
        try:
            decoded = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                decoded = content.decode("cp1252")
            except UnicodeDecodeError as exc:
                raise TemplateTransferError("Die CSV-Datei verwendet eine ungültige Zeichenkodierung.") from exc
        try:
            delimiter = csv.Sniffer().sniff(decoded[:8192], delimiters=";,\t").delimiter
        except csv.Error:
            delimiter = ";"
        try:
            return _parse_table(iter(csv.reader(StringIO(decoded, newline=""), delimiter=delimiter)), True)
        except csv.Error as exc:
            raise TemplateTransferError("Die CSV-Datei ist ungültig.") from exc
    if suffix != ".xlsx":
        raise TemplateTransferError("Bitte eine CSV- oder XLSX-Datei wählen.")
    try:
        with ZipFile(BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > 1000 or sum(entry.file_size for entry in entries) > 50 * 1024 * 1024:
                raise TemplateTransferError("Die entpackte Excel-Datei ist zu groß.")
        workbook = load_workbook(BytesIO(content), read_only=True, data_only=False, keep_links=False)
        try:
            sheet = workbook["Prüfdetailplan"] if "Prüfdetailplan" in workbook.sheetnames else workbook.worksheets[0]
            sheet.reset_dimensions()

            def rows():
                for cells in sheet.iter_rows(max_col=len(COLUMNS) + 1):
                    if any(cell.data_type == "f" for cell in cells):
                        raise TemplateTransferError("Excel-Formeln werden beim Import nicht unterstützt.")
                    yield [cell.value for cell in cells]

            return _parse_table(iter(rows()))
        finally:
            workbook.close()
    except TemplateTransferError:
        raise
    except Exception as exc:
        raise TemplateTransferError("Die XLSX-Datei ist ungültig.") from exc