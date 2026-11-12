from __future__ import annotations

from contextlib import nullcontext

import requests
import requests_cache
from wowipy.exceptions import WowiPyException
from wowipy.models import Result
from wowipy.rest_adapter import RestAdapter
from wowipy.wowipy import WowiPy


REQUEST_TIMEOUT = (10, 60)


class WowiportRestAdapter(RestAdapter):
    def _validation_error_details(self, response):
        if response.status_code not in (400, 409, 422):
            return ""
        try:
            payload = response.json()
        except ValueError:
            content_type = response.headers.get("Content-Type", "").lower()
            payload = response.text if content_type.startswith("text/plain") else None

        messages = []

        def collect(value, field="", depth=0):
            if depth > 3:
                return
            if isinstance(value, str):
                message = " ".join(value.split())
                if message:
                    entry = f"{field}: {message}" if field else message
                    if entry not in messages:
                        messages.append(entry)
            elif isinstance(value, list):
                for item in value[:10]:
                    collect(item, field, depth + 1)
            elif isinstance(value, dict):
                for key, item in value.items():
                    normalized_key = key.casefold()
                    if normalized_key in {"message", "detail", "title", "description"}:
                        collect(item, field, depth + 1)
                    elif normalized_key in {"errors", "modelstate"}:
                        collect(item, field, depth + 1)
                    elif isinstance(item, list) and all(isinstance(entry, str) for entry in item):
                        collect(item, str(key), depth + 1)

        collect(payload)
        details = "; ".join(messages)
        for attribute in ("api_key", "access_token", "refresh_token", "password", "user"):
            value = getattr(self, attribute, None)
            if isinstance(value, str) and value:
                details = details.replace(value, "[entfernt]")
        return details[:2000]

    def _request(self, method, endpoint, *, token=False, **kwargs):
        url = (
            f"https://{self.host_base}/oauth2/token"
            if token else self.url + endpoint
        )
        try:
            return requests.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
        except requests.Timeout as exc:
            raise WowiPyException(
                f"OPENWOWI-Zeitlimit überschritten ({method} {endpoint})."
            ) from exc
        except requests.RequestException as exc:
            raise WowiPyException(
                f"OPENWOWI-Anfrage fehlgeschlagen ({method} {endpoint})."
            ) from exc

    def _create_token(self, refresh_token=None):
        payload = (
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
            if refresh_token else {
                "grant_type": "password",
                "username": self.user,
                "password": self.password,
            }
        )
        response = self._request(
            "POST", "oauth2/token", token=True,
            data=payload,
            headers={"User-Agent": self.user_agent, "Accept": "text/plain"},
        )
        if response.status_code != 200:
            raise WowiPyException(
                f"OPENWOWI-Anmeldung fehlgeschlagen (Status {response.status_code})."
            )
        try:
            data = response.json()
            return data["access_token"], data["refresh_token"]
        except (ValueError, KeyError, TypeError) as exc:
            raise WowiPyException("Ungültige OPENWOWI-Anmeldedaten erhalten.") from exc

    def _do(self, http_method, endpoint, ep_params=None, data=None, force_refresh=False):
        params = dict(ep_params or {})
        if http_method.upper() == "GET":
            params.setdefault("limit", 100)
            if not 1 <= params["limit"] <= 100:
                raise WowiPyException("Wert für limit muss zwischen 1 und 100 liegen")
        params["apiKey"] = self.api_key
        for attempt in range(2):
            with requests_cache.disabled() if force_refresh else nullcontext():
                response = self._request(
                    http_method, endpoint, params=params, json=data,
                    headers={
                        "User-Agent": self.user_agent,
                        "Accept": "text/plain",
                        "Authorization": f"Bearer {self.access_token}",
                    },
                )
            if response.status_code != 401 or attempt == 1:
                break
            self.access_token, self.refresh_token = self._create_token(self.refresh_token)
        if not 200 <= response.status_code < 300:
            details = self._validation_error_details(response)
            raise WowiPyException(
                f"OPENWOWI hat {http_method} {endpoint} "
                f"mit Status {response.status_code} abgelehnt."
                + (f" OPENWOWI-Details: {details}" if details else "")
            )
        try:
            result = response.json()
        except ValueError as exc:
            raise WowiPyException("Ungültige OPENWOWI-Antwort erhalten.") from exc
        return Result(response.status_code, message=response.reason, data=result)


class WowiportClient(WowiPy):
    def __init__(self, hostname, **kwargs):
        super().__init__(hostname="", **kwargs)
        self._rest_adapter = WowiportRestAdapter(hostname=hostname, **kwargs)
