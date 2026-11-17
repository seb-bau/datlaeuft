from __future__ import annotations

import gettext
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any
from urllib.parse import urlsplit

import pycountry
from flask import request, url_for


def commission_list_return_url() -> str:
    fallback = url_for("admin.commissions")
    value = request.values.get("return_to", "")
    if "\\" in value or any(ord(character) < 32 or ord(character) == 127 for character in value):
        return fallback
    try:
        parsed = urlsplit(value)
    except ValueError:
        return fallback
    if parsed.scheme or parsed.netloc or parsed.path != fallback:
        return fallback
    return fallback + ("?" + parsed.query if parsed.query else "")


def commission_detail_url(commission_id: int, anchor: str | None = None) -> str:
    return_url = commission_list_return_url()
    values = {"commission_id": commission_id}
    if return_url != url_for("admin.commissions"):
        values["return_to"] = return_url
    if anchor:
        values["_anchor"] = anchor
    return url_for("admin.commission_detail", **values)


def country_lookup_key(value: Any) -> str:
    return " ".join(
        str(value).strip().casefold().split()
    )


def build_country_code_lookup() -> dict[str, str]:
    translation = gettext.translation(
        "iso3166-1",
        pycountry.LOCALES_DIR,
        languages=["de"],
        fallback=True,
    )
    lookup: dict[str, str] = {}

    for country in pycountry.countries:
        country_code = country.alpha_2

        for attribute_name in (
            "alpha_2",
            "alpha_3",
            "numeric",
            "name",
            "official_name",
            "common_name",
        ):
            country_name = getattr(
                country,
                attribute_name,
                None,
            )

            if not country_name:
                continue

            lookup[
                country_lookup_key(country_name)
            ] = country_code

            translated_name = translation.gettext(
                str(country_name)
            )
            lookup[
                country_lookup_key(translated_name)
            ] = country_code

    lookup["kosovo"] = "XK"
    lookup["xk"] = "XK"

    return lookup


COUNTRY_CODE_LOOKUP = build_country_code_lookup()


def normalize_country_code(
    value: Any,
    default: str = "DE",
) -> str:
    text = str(value or "").strip()

    if not text:
        return default

    return COUNTRY_CODE_LOOKUP.get(
        country_lookup_key(text),
        text.upper(),
    )


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)

    return value.astimezone(timezone.utc)


def nested_value(data: dict[str, Any] | None, *keys: str, default: Any = None) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    return default if current is None else current


def parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None

    if isinstance(value, datetime):
        return ensure_utc(value)

    text = str(value).replace("Z", "+00:00")

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None

    return ensure_utc(parsed)


def parse_local_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Europe/Berlin"))
    return ensure_utc(parsed)


def format_local_datetime(value: datetime | None, pattern: str = "%d.%m.%Y %H:%M") -> str:
    return ensure_utc(value).astimezone(ZoneInfo("Europe/Berlin")).strftime(pattern) if value else "–"


def iso_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()
