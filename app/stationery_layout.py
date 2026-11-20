from decimal import Decimal, InvalidOperation

from .models import SettingValueType
from .settings import SettingDefinition, settings


LAYOUT_FIELDS = {
    "first_page_top_mm": "Seitenbeginn auf der ersten Seite",
    "following_page_top_mm": "Seitenbeginn auf Folgeseiten",
    "bottom_mm": "Freiraum am unteren Seitenrand",
}
STATIONERY_SLOTS = ("stationery", "cover_letter_stationery")


def layout_definition(company):
    return SettingDefinition(
        f"company_code.{company.id}.stationery_layout", SettingValueType.JSON, {},
        f"Seitenabstände für Buchwerk {company.code}", "Rechnungen und Angebote",
    )


def stored_layouts(company):
    if company is None or getattr(company, "id", None) is None:
        return {slot: {} for slot in STATIONERY_SLOTS}
    values = settings.get(layout_definition(company))
    if not isinstance(values, dict):
        values = {}
    return {slot: dict(values.get(slot) or {}) for slot in STATIONERY_SLOTS}


def normalize_layout(values):
    result = {}
    for field, label in LAYOUT_FIELDS.items():
        raw_value = values.get(field)
        if raw_value is None or not str(raw_value).strip():
            continue
        try:
            number = Decimal(str(raw_value).strip().replace(",", "."))
            if not number.is_finite() or not 0 <= number <= 150 or number != number.quantize(Decimal("0.1")):
                raise ValueError()
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"{label}: Bitte einen Wert von 0 bis 150 mm mit höchstens einer Nachkommastelle angeben.") from exc
        result[field] = format(number.normalize(), "f") if number else "0"
    bottom = Decimal(result.get("bottom_mm", "0"))
    for field in ("first_page_top_mm", "following_page_top_mm"):
        if Decimal(result.get(field, "0")) + bottom > 250:
            raise ValueError("Oberer und unterer Freiraum dürfen zusammen höchstens 250 mm betragen.")
    return result


def form_layouts(company, form):
    values = stored_layouts(company)
    return {
        slot: normalize_layout({field: form.get(f"{slot}_{field}", values[slot].get(field)) for field in LAYOUT_FIELDS})
        for slot in STATIONERY_SLOTS
    }


def form_layout_values(company, form=None):
    values = stored_layouts(company)
    if form is None:
        return values
    return {
        slot: {field: form.get(f"{slot}_{field}", values[slot].get(field, "")) for field in LAYOUT_FIELDS}
        for slot in STATIONERY_SLOTS
    }


def save_layouts(company, form, *, has_own_cover_stationery=None):
    if any(f"{slot}_{field}" in form for slot in STATIONERY_SLOTS for field in LAYOUT_FIELDS):
        layouts = form_layouts(company, form)
        company_page_layout(company, "cover_letter_stationery", layouts=layouts, has_own_cover_stationery=has_own_cover_stationery)
        settings.set(layout_definition(company), layouts)


def company_page_layout(company, slot="stationery", *, layouts=None, has_own_cover_stationery=None):
    layouts = stored_layouts(company) if layouts is None else layouts
    own_cover = bool(getattr(company, "cover_letter_stationery_filename", None)) if has_own_cover_stationery is None else has_own_cover_stationery
    values = dict(layouts.get("stationery") or {}) if slot == "cover_letter_stationery" and not own_cover else {}
    values.update(layouts.get(slot) or {})
    return normalize_layout(values)


def page_stylesheets(layout):
    values = normalize_layout(layout or {})
    rules = []
    if "bottom_mm" in values:
        footer_rules = " ".join(f"@{box} {{ content: none !important; }}" for box in (
            "bottom-left-corner", "bottom-left", "bottom-center", "bottom-right", "bottom-right-corner",
        ))
        rules.append(f"@page {{ margin-bottom: {values['bottom_mm']}mm !important; {footer_rules} }}")
    if "first_page_top_mm" in values:
        rules.append(f"@page:first {{ margin-top: {values['first_page_top_mm']}mm !important; }}")
    if "following_page_top_mm" in values:
        rules.append(f"@page:nth(n+2) {{ margin-top: {values['following_page_top_mm']}mm !important; }}")
    if not rules:
        return []
    from weasyprint import CSS

    return [CSS(string="\n".join(rules))]
