from __future__ import annotations

from contextlib import nullcontext
from hashlib import sha256
import json
from pathlib import Path
from urllib.parse import parse_qsl, quote, urljoin, urlsplit

from dvelopdmspy.dvelopdmspy import DvelopDmsPy
from dvelopdmspy.models import Result
from requests import RequestException
from requests_cache import CachedSession


class DmsError(RuntimeError):
    def __init__(self, message, *, retryable=False, uncertain=False):
        super().__init__(message)
        self.retryable = retryable
        self.uncertain = uncertain


def _http_error_message(response, *credentials):
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

    secrets = sorted(
        {encoded for credential in credentials if credential for encoded in (credential, quote(credential, safe=""), quote_plus(credential, safe=""))},
        key=len, reverse=True,
    )

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

    def request(self, method, url, *, params=None, data=None, upload_file_path=None, version_document_id=None, track_write=True, auth_session_id=None):
        method = method.upper()
        parsed = urlsplit(url)

        if parsed.scheme != "https" or parsed.netloc != self.hostname:
            raise DmsError(
                "d.velop hat einen ungültigen Verweis geliefert."
            )

        create_path = urlsplit(self.url + "o2m").path
        blob_path = urlsplit(self.url + "blob/chunk/").path
        is_create = method == "POST" and parsed.path == create_path
        is_version = (
            method == "PUT"
            and version_document_id
            and parsed.path == f"{create_path}/{quote(version_document_id, safe='')}"
        )
        version_source_query = is_version and [
            (key.casefold(), value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        ] == [("sourceid", urlsplit(self.url + "source").path)]

        if method != "GET" and (
            not (is_create or is_version or (method == "POST" and parsed.path == blob_path))
            or (parsed.query and not version_source_query)
            or parsed.fragment
            or params
        ):
            raise DmsError(
                "Dieser Schreibzugriff auf d.velop ist nicht zulässig."
            )

        if auth_session_id is not None and (method != "GET" or url != self.root + "identityprovider/validate"):
            raise DmsError("Die d.velop-Sitzung darf nur zur Benutzerprüfung verwendet werden.")

        writes_document = bool(is_create or is_version)
        if writes_document and track_write and self.before_create:
            self.before_create()
        if upload_file_path:
            headers = {"Content-Type": "application/octet-stream"}
            body = None
        else:
            headers = {"Content-Type": "application/hal+json" if is_version else "application/json"}
            body = data
        if auth_session_id is not None:
            headers["Authorization"] = f"Bearer {auth_session_id}"
            headers["Accept"] = "application/json"
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
                uncertain=writes_document,
            ) from exc
        if not 200 <= response.status_code < 300:
            api_key = self.session.headers.get("Authorization", "").removeprefix("Bearer ")
            raise DmsError(
                _http_error_message(response, api_key, auth_session_id),
                retryable=(
                    response.status_code in (408, 429)
                    or response.status_code >= 500
                ),
                uncertain=(
                    writes_document
                    and (
                        response.status_code == 408
                        or response.status_code >= 500
                    )
                ),
            )
        return response

    def authenticated_user_id(self):
        try:
            login = self.request("GET", self.root + "identityprovider/login").json()
            session_id = login.get("authSessionId") or login.get("AuthSessionId")
            if not isinstance(session_id, str) or not session_id:
                raise ValueError()
            user = self.request(
                "GET", self.root + "identityprovider/validate", auth_session_id=session_id,
            ).json()
            user_id = user.get("id")
            if not isinstance(user_id, str) or not user_id or user.get("active") is False:
                raise ValueError()
        except (AttributeError, ValueError) as exc:
            raise DmsError("Das Konto des d.velop-API-Keys konnte nicht ermittelt werden.") from exc
        return user_id

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

    def _document_details(self, document_id):
        transport = self._rest_adapter
        details = transport.get(
            f"o2m/{quote(document_id, safe='')}",
            ep_params={"sourceid": urlsplit(transport.url + "source").path},
        ).data
        if not isinstance(details, dict) or details.get("id") != document_id:
            raise DmsError("Das Rechnungsdokument wurde in d.velop nicht gefunden.")
        return details

    def document_sha256(self, document_id):
        transport = self._rest_adapter
        details = self._document_details(document_id)
        link = details.get("_links", {}).get("mainblobcontent", {}).get("href")
        if not link:
            raise DmsError("Die aktuelle PDF-Version in d.velop ist nicht verfügbar.")
        url = urljoin(transport.root, link)
        document_path = urlsplit(transport.url + f"o2/{quote(document_id, safe='')}/").path
        if not urlsplit(url).path.startswith(document_path):
            raise DmsError("d.velop hat einen ungültigen Verweis auf die Rechnungsdatei geliefert.")
        return sha256(transport.request("GET", url).content).hexdigest()

    def archive_invoice_version(self, filepath, document_id):
        transport = self._rest_adapter
        details = self._document_details(document_id)
        state = next((
            item.get("value") for item in details.get("sourceProperties", [])
            if item.get("key") == "property_state"
        ), None)
        if str(state).casefold() not in {"release", "released", "archive", "archived", "fr", "ar"}:
            raise DmsError("Die Rechnung muss in d.velop freigegeben oder archiviert sein, um sie zu versionieren.")

        source_id = urlsplit(transport.url + "source").path
        update_link = details.get("_links", {}).get("updateWithContent", {}).get("href")
        if not update_link:
            results = transport.get("srm", ep_params={
                "sourceid": source_id,
                "sourceproperties": json.dumps({"property_document_id": [document_id]}),
            }, limit=2).data
            for item in results:
                if item.get("id") == document_id:
                    update_link = item.get("_links", {}).get("updateWithContent", {}).get("href")
                    break
        if not update_link:
            raise DmsError("Der DMS-Zugang darf keine neue Dateiversion für diese Rechnung speichern.")

        upload_url = transport.url + "blob/chunk/"
        upload = transport.request("POST", upload_url, upload_file_path=filepath)
        location = upload.headers.get("Location", "")
        blob = urlsplit(urljoin(transport.root, location))
        if (
            upload.status_code != 201 or blob.scheme != "https" or blob.netloc != transport.hostname
            or not blob.path.startswith(urlsplit(upload_url).path)
            or blob.path == urlsplit(upload_url).path or blob.fragment
        ):
            raise DmsError("d.velop hat keinen gültigen Verweis auf die hochgeladene PDF geliefert.", retryable=True)

        response = transport.request("PUT", urljoin(transport.root, update_link), data={
            "filename": Path(filepath).name,
            "alterationText": "Rechnungsunterlagen aktualisiert",
            "sourceId": source_id,
            "contentLocationUri": blob._replace(scheme="", netloc="").geturl(),
            "sourceProperties": {"properties": []},
        }, version_document_id=document_id)
        if response.status_code not in (200, 204):
            raise DmsError("d.velop hat die neue PDF-Version noch nicht bestätigt.", retryable=True, uncertain=True)

    def apply_invoice_dispatch(
        self, document_id, properties, flag_property, *,
        submission_started=False, system_user_id=None, alternate_signer_property=None,
    ):
        transport = self._rest_adapter
        details = self._document_details(document_id)
        current = self._dispatch_property_values(details)
        if all(current.get(key) == value for key, value in properties.items()):
            return
        if submission_started:
            raise DmsError("d.velop hat die Versandeigenschaften noch nicht bestätigt.", retryable=True, uncertain=True)
        if current.get(flag_property) == "Ja":
            raise DmsError("Für dieses DMS-Dokument ist die Versand-Eigenschaft bereits gesetzt. Bitte die frühere Übergabe prüfen.")
        identity = {key: value for key, value in properties.items() if key != flag_property}
        if identity and (not system_user_id or transport.authenticated_user_id() != system_user_id):
            raise DmsError("Der DMS-API-Key gehört nicht zum eingetragenen Paperwork-Systembenutzer.")
        if alternate_signer_property and current.get(alternate_signer_property):
            raise DmsError("Im DMS-Dokument ist eine abweichende Unterschrift hinterlegt. Bitte die Angabe im DMS prüfen.")
        source_id = urlsplit(transport.url + "source").path
        links = details.get("_links", {})
        update_link = (links.get("update") or links.get("updateWithContent") or {}).get("href")
        if not update_link:
            results = transport.get("srm", ep_params={
                "sourceid": source_id,
                "sourceproperties": json.dumps({"property_document_id": [document_id]}),
            }, limit=2).data
            for item in results:
                if item.get("id") == document_id:
                    links = item.get("_links", {})
                    update_link = (links.get("update") or links.get("updateWithContent") or {}).get("href")
                    break
        if not update_link:
            raise DmsError("Der DMS-Zugang darf die Versandeigenschaften dieser Rechnung nicht aktualisieren.")

        def update_properties(values, *, track_write, alteration_text):
            transport.request("PUT", urljoin(transport.root, update_link), data={
                "sourceId": source_id, "alterationText": alteration_text,
                "sourceProperties": {"properties": [{"key": key, "values": [value]} for key, value in values.items()]},
            }, version_document_id=document_id, track_write=track_write)
            return self._dispatch_property_values(self._document_details(document_id))

        if any(current.get(key) != value for key, value in identity.items()):
            current = update_properties(identity, track_write=False, alteration_text="Poststellenbenutzer hinterlegt")
            if any(current.get(key) != value for key, value in identity.items()):
                raise DmsError("d.velop hat das Feld für den Poststellenbenutzer nicht übernommen.")
            if current.get(flag_property) == "Ja":
                raise DmsError("Die Versand-Eigenschaft wurde zwischenzeitlich gesetzt. Bitte die Übergabe prüfen.")
            if alternate_signer_property and current.get(alternate_signer_property):
                raise DmsError("Im DMS-Dokument ist eine abweichende Unterschrift hinterlegt. Bitte die Angabe im DMS prüfen.")

        confirmed = update_properties(
            {flag_property: properties[flag_property]}, track_write=True, alteration_text="Rechnung zum Versand freigegeben",
        )
        if any(confirmed.get(key) != value for key, value in properties.items()):
            raise DmsError("d.velop hat die Versandeigenschaften noch nicht bestätigt.", retryable=True, uncertain=True)

    @staticmethod
    def _dispatch_property_values(details):
        values = {}
        for item in details.get("sourceProperties", []):
            if item.get("key") and isinstance(item.get("value"), str):
                values[item["key"]] = item["value"]
            elif item.get("key") and len(item.get("values") or []) == 1:
                value = item["values"][0]
                values[item["key"]] = value.get("value") if isinstance(value, dict) else value
            elif item.get("key") and item.get("values"):
                values[item["key"]] = item["values"]
        return values

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
