from __future__ import annotations

import time
from typing import Any

import requests
from flask import current_app

from .settings import SettingKeys, settings


last_request_time = 0.0


def geocode_address(address: str) -> tuple[float | None, float | None]:
    global last_request_time

    api_key = settings.get(SettingKeys.GEOAPIFY_API_KEY).strip()
    if not api_key or not settings.get(SettingKeys.GEOCODING_ENABLED):
        return None, None

    interval = float(settings.get(SettingKeys.GEOCODING_REQUEST_INTERVAL_SECONDS))
    now = time.time()
    elapsed = now - last_request_time

    if elapsed < interval:
        time.sleep(interval - elapsed)

    last_request_time = time.time()

    response = requests.get(
        "https://api.geoapify.com/v1/geocode/search",
        params={
            "text": address,
            "format": "json",
            "apiKey": api_key,
            "lang": "de",
            "limit": 1,
            "filter": "countrycode:de",
        },
        timeout=10,
    )
    response.raise_for_status()

    data: dict[str, Any] = response.json()
    results = data.get("results") or []

    if not results:
        current_app.logger.warning("geocoding_no_result address=%s", address)
        return None, None

    return float(results[0]["lat"]), float(results[0]["lon"])
