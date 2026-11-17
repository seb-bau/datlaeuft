from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation
from io import StringIO
import re

from .models import LocalCatalogItem


MAX_IMPORT_BYTES = 2 * 1024 * 1024
MAX_IMPORT_ROWS = 5000
CATALOG_FIELDS = (
    ("item_number", "Artikelnummer", 100, True),
    ("name", "Bezeichnung", 255, True),
    ("business_area", "Geschäftsbereich", 100, False),
    ("product_group", "Warengruppe", 100, False),
    ("unit", "Einheit", 40, True),
    ("tax_label", "Steuersatz", 80, True),
)
IMPORT_FIELDS = ("item_number", "name", "business_area", "product_group", "unit", "price_net", "active", "tax_label", "vat_rate")


def article_decimal(value, label: str, maximum: str, places=4) -> Decimal:
    text = str("" if value is None else value).strip()
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        result = Decimal(text)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{label} ist ungültig.") from exc
    if not result.is_finite() or not Decimal("0") <= result <= Decimal(maximum):
        raise ValueError(f"{label} muss zwischen 0 und {maximum} liegen.")
    rounded = result.quantize(Decimal("1").scaleb(-places))
    if result != rounded:
        raise ValueError(f"{label} darf höchstens {places} Nachkommastellen enthalten.")
    return rounded


def catalog_values(data) -> dict:
    values = {}
    for field, label, length, required in CATALOG_FIELDS:
        value = str(data.get(field, "")).strip()
        if required and not value:
            raise ValueError(f"{label} fehlt.")
        if len(value) > length:
            raise ValueError(f"{label} darf höchstens {length} Zeichen enthalten.")
        values[field] = value
    values["price_net"] = article_decimal(data.get("price_net"), "Nettopreis", "9999999999.9999")
    values["vat_rate"] = article_decimal(data.get("vat_rate"), "MwSt.", "100", 2)
    values["active"] = data.get("active") in (True, "on", "true", "1")
    return values


def catalog_identity(values) -> tuple:
    def value(field):
        return values.get(field) if isinstance(values, dict) else getattr(values, field)
    return tuple(" ".join(str(value(field) or "").split()).casefold() for field in (
        "item_number", "name", "business_area", "tax_label",
    )) + (str(Decimal(str(value("vat_rate"))).normalize()),)


def parse_catalog_import(raw_data: bytes, default_unit="Stk") -> dict:
    if not raw_data or len(raw_data) > MAX_IMPORT_BYTES:
        raise ValueError("Bitte eine nicht leere Importdatei mit höchstens 2 MB auswählen.")
    try:
        text = raw_data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw_data.decode("cp1252")
        except UnicodeDecodeError as exc:
            raise ValueError("Die Datei muss als UTF-8 oder Windows-1252 gespeichert sein.") from exc
    if "\x00" in text:
        raise ValueError("Die Datei ist keine gültige Textdatei.")
    default_unit = default_unit.strip()
    if not default_unit or len(default_unit) > 40:
        raise ValueError("Bitte eine Ersatzeinheit mit höchstens 40 Zeichen angeben.")
    result = {"rows": [], "errors": [], "warnings": [], "duplicates": 0}
    seen = {}
    reader = csv.reader(StringIO(text, newline=""), delimiter=";", strict=True)
    try:
        count = 0
        for columns in reader:
            line = reader.line_num
            if not columns or not any(value.strip() for value in columns):
                continue
            if count == 0 and columns[0].strip().casefold() in {"artikelnummer", "artikel-nr.", "item_number"}:
                if len(columns) != 9:
                    raise ValueError("Die Kopfzeile muss neun Spalten enthalten.")
                count += 1
                continue
            count += 1
            if count > MAX_IMPORT_ROWS:
                raise ValueError("Die Importdatei darf höchstens 5.000 Zeilen enthalten.")
            try:
                if len(columns) != 9:
                    raise ValueError(f"Es werden neun Spalten erwartet; gefunden: {len(columns)}.")
                data = dict(zip(IMPORT_FIELDS, (value.strip() for value in columns)))
                for field, label in (("price_net", "Nettopreis"), ("vat_rate", "MwSt.")):
                    if not re.fullmatch(r"(?:\d+|\d{1,3}(?:\.\d{3})+)(?:,\d+)?", data[field]):
                        raise ValueError(f"{label} ist ungültig. Bitte deutsche Zahlen verwenden, z. B. 1.234,56.")
                active = data["active"].casefold()
                if active not in {"true", "false", "1", "0", "ja", "nein"}:
                    raise ValueError("Aktiv muss true oder false sein.")
                data["active"] = active in {"true", "1", "ja"}
                if not data["unit"]:
                    data["unit"] = default_unit
                    result["warnings"].append({"line": line, "message": f"Fehlende Einheit wird mit {default_unit} ergänzt."})
                values = catalog_values(data)
                values["price_net"] = str(values["price_net"])
                values["vat_rate"] = str(values["vat_rate"])
                key = catalog_identity(values)
                if key in seen:
                    if seen[key] != values:
                        raise ValueError("Diese Artikelvariante ist mehrfach mit unterschiedlichen Angaben enthalten.")
                    result["duplicates"] += 1
                    result["warnings"].append({"line": line, "message": "Vollständig identische Artikelvariante wird nur einmal übernommen."})
                    continue
                seen[key] = values
                result["rows"].append({"line": line, "values": values})
            except ValueError as exc:
                result["errors"].append({"line": line, "message": str(exc)})
    except csv.Error as exc:
        result["errors"].append({"line": reader.line_num, "message": f"Die CSV-Zeile ist ungültig: {exc}"})
    if not result["rows"] and not result["errors"]:
        raise ValueError("Die Datei enthält keine Artikel.")
    return result


def plan_catalog_import(rows: list[dict]) -> dict:
    existing = {}
    for item in LocalCatalogItem.query.all():
        existing.setdefault(catalog_identity(item), []).append(item)
    result = {"rows": [], "created": 0, "updated": 0, "unchanged": 0}
    for row in rows:
        values = catalog_values(row["values"])
        matches = existing.get(catalog_identity(values), [])
        if len(matches) > 1:
            raise ValueError(f"Zeile {row['line']}: Mehrere bestehende Artikel passen zu dieser Variante. Bitte zuerst den Katalog bereinigen.")
        item = matches[0] if matches else None
        action = "created" if item is None else "updated" if any(getattr(item, field) != value for field, value in values.items()) else "unchanged"
        result[action] += 1
        result["rows"].append({"line": row["line"], "values": values, "item": item, "action": action})
    return result
