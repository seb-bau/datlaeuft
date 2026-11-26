from __future__ import annotations

from contextlib import nullcontext
from urllib.parse import quote, urljoin, urlsplit

from dvelopdmspy.dvelopdmspy import DvelopDmsPy
from dvelopdmspy.models import Result
from requests import RequestException
from requests_cache import CachedSession


class DmsError(RuntimeError):
    def __init__(self, message, *, retryable=False, uncertain=False):
        super().__init__(message)
        self.retryable = retryable
        self.uncertain = uncertain


def _http_error_message(response, api_key):
    from urllib.parse import quote_plus

    status_message = f"d.velop hat die Anfrage mit Status {response.status_code} abgelehnt."
    if len(response.content) > 64 * 1024:
        return status_message

    try:
        payload = response.json()
    except (ValueError, RecursionError):
        content_type = response.headers.get("Content-Type", "").partition(";")[0].strip().lower()
        if content_type != "text/plain":
            return status_message
        payload = response.text

    if isinstance(payload, str):
        payload = {"reason": payload}
    if not isinstance(payload, dict):
        return status_message

    secrets = sorted({api_key, quote(api_key, safe=""), quote_plus(api_key, safe="")} - {""}, key=len, reverse=True)

    def clean(value):
        if not isinstance(value, str):
            return ""
        text = "".join(character for character in value if character.isprintable() or character.isspace())
        for secret in secrets:
            text = text.replace(secret, "[API-Key ausgeblendet]")
        return " ".join(text.split())

    parts = [status_message]
    error_code = payload.get("errorCode", payload.get("code"))
    error_code = clean(str(error_code) if type(error_code) is int else error_code)
    if error_code:
        parts.append(f"Fehlercode {error_code[:80]}.")
    request_id = clean(payload.get("requestId"))
    if request_id:
        parts.append(f"Anfrage-ID {request_id[:160]}.")

    messages = [payload.get(field) for field in ("reason", "message", "title", "detail", "details", "hint", "error")]
    if isinstance(payload.get("details"), list):
        messages.extend(payload["details"][:10])
    parts.extend(dict.fromkeys(filter(None, map(clean, messages))))
    message = " ".join(parts)
    return message if len(message) <= 1000 else message[:999] + "…"


def normalize_hostname(value):
    try:
        parsed = urlsplit(value if "://" in value else f"https://{value}")
        parsed.port
    except ValueError as exc:
        raise DmsError(
            "Bitte einen gültigen d.velop-Host mit HTTPS eingeben."
        ) from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or len(parsed.netloc) > 255
        or any(character.isspace() for character in parsed.netloc)
    ):
        raise DmsError(
            "Bitte einen gültigen d.velop-Host mit HTTPS eingeben."
        )
    return parsed.netloc.lower()


class DmsTransport:
    def __init__(self, hostname, api_key, repository, before_create=None):
        self.hostname = normalize_hostname(hostname)
        self.repository = repository
        self.root = f"https://{self.hostname}/"
        self.url = f"{self.root}dms/r/{quote(repository, safe='')}/"
        self.before_create = before_create
        self.session = CachedSession(
            backend="memory", allowable_methods=(),
        )
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/hal+json",
            "User-Agent": "datlaeuft/1.0",
            "Origin": self.root.rstrip("/"),
        })

    def request(self, method, url, *, params=None, data=None, upload_file_path=None):
        method = method.upper()
        parsed = urlsplit(url)

        if parsed.scheme != "https" or parsed.netloc != self.hostname:
            raise DmsError(
                "d.velop hat einen ungültigen Verweis geliefert."
            )

        allowed_uploads = {
            urlsplit(self.url + "blob/chunk/").path,
            urlsplit(self.url + "o2m").path,
        }

        if method != "GET" and (
            method != "POST"
            or parsed.path not in allowed_uploads
            or parsed.query
            or parsed.fragment
            or params
        ):
            raise DmsError(
                "Die DMS-Anbindung erlaubt nur Lesen "
                "und das Anlegen neuer Dokumente."
            )

        is_create = (
            method == "POST"
            and parsed.path == urlsplit(self.url + "o2m").path
        )
        if is_create and self.before_create:
            self.before_create()
        if upload_file_path:
            headers = {"Content-Type": "application/octet-stream"}
            body = None
        else:
            headers = {"Content-Type": "application/json"}
            body = data
        try:
            with (
                open(upload_file_path, "rb")
                if upload_file_path
                else nullcontext(None)
            ) as content:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    json=body,
                    data=content,
                    headers=headers,
                    timeout=(10, 60),
                    allow_redirects=False,
                )
        except RequestException as exc:
            raise DmsError(
                "d.velop ist derzeit nicht erreichbar.",
                retryable=True,
                uncertain=is_create,
            ) from exc
        if not 200 <= response.status_code < 300:
            api_key = self.session.headers.get("Authorization", "").removeprefix("Bearer ")
            raise DmsError(
                _http_error_message(response, api_key),
                retryable=(
                    response.status_code in (408, 429)
                    or response.status_code >= 500
                ),
                uncertain=(
                    is_create
                    and (
                        response.status_code == 408
                        or response.status_code >= 500
                    )
                ),
            )
        return response

    def get(
        self, endpoint, ep_params=None, base_url=None, binary=False, limit=None,
    ):
        url = urljoin(base_url or self.url, endpoint)
        response = self.request("GET", url, params=ep_params)
        if binary:
            return Result(
                response.status_code, raw=response, headers=response.headers,
            )
        try:
            data = response.json()
            items = data.get("items") if isinstance(data, dict) else None
            visited = {url}
            while items is not None and data.get("_links", {}).get("next"):
                if limit is not None and len(items) >= limit:
                    break
                link = data["_links"]["next"]["href"]
                url = urljoin(url, link)
                if url in visited or len(visited) >= 1000:
                    raise DmsError(
                        "Die d.velop-Seitenverweise sind ungültig."
                    )
                visited.add(url)
                response = self.request("GET", url)
                data = response.json()
                items.extend(data.get("items", []))
        except (ValueError, TypeError, KeyError) as exc:
            raise DmsError(
                "d.velop hat ungültige Metadaten geliefert."
            ) from exc
        if items is not None:
            data = items[:limit] if limit is not None else items
        return Result(
            response.status_code, data=data, headers=response.headers,
        )

    def post(
        self, endpoint, ep_params=None, data=None,
        binary_upload=False, upload_file_path=None,
    ):
        response = self.request(
            "POST",
            self.url + endpoint,
            params=ep_params,
            data=data,
            upload_file_path=upload_file_path if binary_upload else None,
        )
        return Result(response.status_code, headers=response.headers)

    def collection(self, path, name):
        url = urljoin(self.root, path)
        items = []
        visited = set()
        while url:
            if url in visited or len(visited) >= 1000:
                raise DmsError("Die d.velop-Seitenverweise sind ungültig.")
            visited.add(url)
            response = self.request("GET", url)
            try:
                data = response.json()
                batch = data.get("_embedded", {}).get(name)
                if not isinstance(batch, list):
                    raise ValueError()
                items.extend(batch)
                link = data.get("_links", {}).get("next", {}).get("href")
                url = urljoin(url, link) if link else None
            except (ValueError, TypeError, AttributeError) as exc:
                raise DmsError(
                    "Die Repository-Metadaten sind unvollständig."
                ) from exc
        return items


class CloudDmsClient(DvelopDmsPy):
    def __init__(self, hostname, api_key, repository, before_create=None):
        self._rest_adapter = DmsTransport(
            hostname, api_key, repository, before_create,
        )
        try:
            self._source_mappings = self.get_mappings()
        except Exception:
            self.close()
            raise

    def archive_file(
        self,
        filepath,
        category_id,
        properties,
        doc_id=None,
        alteration_msg=None,
    ):
        if doc_id is not None or alteration_msg is not None:
            raise DmsError(
                "Bestehende DMS-Dokumente dürfen "
                "nicht geändert oder versioniert werden."
            )

        return super().archive_file(
            filepath,
            category_id,
            properties,
        )

    def update_properties(self, *args, **kwargs):
        raise DmsError(
            "Eigenschaften bestehender DMS-Dokumente "
            "dürfen nicht geändert werden."
        )

    def delete_document(self, *args, **kwargs):
        raise DmsError(
            "DMS-Dokumente dürfen nicht gelöscht werden."
        )

    def close(self):
        self._rest_adapter.session.close()

    def repository_metadata(self):
        transport = self._rest_adapter
        prefix = (
            f"dmsconfig/r/{quote(transport.repository, safe='')}"
            "/objectmanagement/"
        )
        properties = {}
        for item in transport.collection(prefix + "properties", "properties"):
            property_id = item["id"]
            properties[property_id] = transport.get(
                prefix + "properties/" + quote(property_id, safe=""),
                base_url=transport.root,
            ).data
        categories = {}
        for item in transport.collection(prefix + "categories", "categories"):
            category_id = item["id"]
            category = transport.get(
                prefix + "categories/" + quote(category_id, safe=""),
                base_url=transport.root,
            ).data
            if category.get("type") != "DOCUMENT_TYPE":
                continue
            fields = []
            for reference in category.get("propertyRefs", []):
                property_id = reference["propertyId"]
                property_data = properties.get(property_id)
                if property_data is None:
                    raise DmsError(
                        "Eine Kategorie verweist auf eine unbekannte Eigenschaft."
                    )
                if not property_data.get("isSystemProperty"):
                    fields.append({**property_data, **reference})
            categories[category_id] = {
                "id": category_id,
                "name": display_name(category["name"]),
                "fields": fields,
            }
        return {"categories": categories}


def display_name(value):
    if isinstance(value, str):
        return value
    return (
        value.get("de")
        or value.get("de-DE")
        or value.get("en")
        or next(iter(value.values()), "")
    )