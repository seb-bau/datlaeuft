from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote
from typing import Any

import msal
import requests

from .extensions import db
from .models import User
from .settings import SettingKeys, settings
from .blacklist import include_blacklisted


GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
GRAPH_USER_FIELDS = (
    "id,displayName,givenName,surname,mail,userPrincipalName,accountEnabled"
)


@dataclass
class EntraImportResult:
    created: int = 0
    updated: int = 0
    skipped: int = 0


@include_blacklisted()
def import_entra_users() -> EntraImportResult:
    access_token = _acquire_graph_access_token()
    allowed_group_id = _configured_allowed_group_id()
    headers = {
        "Authorization": f"Bearer {access_token}",
    }
    params: dict[str, str] | None = {
        "$select": GRAPH_USER_FIELDS,
    }

    if allowed_group_id:
        encoded_group_id = quote(
            allowed_group_id,
            safe="",
        )
        url = (
            f"{GRAPH_BASE_URL}/groups/"
            f"{encoded_group_id}/transitiveMembers/"
            "microsoft.graph.user"
        )
        headers["ConsistencyLevel"] = "eventual"
        # noinspection PyUnresolvedReferences
        params["$count"] = "true"
    else:
        url = f"{GRAPH_BASE_URL}/users"

    result = EntraImportResult()

    while url:
        response = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        for entry in data.get("value", []):
            entra_id = str(
                entry.get("id") or ""
            ).strip()
            email = str(
                entry.get("mail")
                or entry.get("userPrincipalName")
                or ""
            ).strip().lower()

            if not entra_id or not email:
                result.skipped += 1
                continue

            user_by_oid = User.query.filter_by(
                entra_oid=entra_id,
            ).one_or_none()
            user_by_email = User.query.filter_by(
                email=email,
            ).one_or_none()

            if (
                    user_by_oid is not None
                    and user_by_email is not None
                    and user_by_oid.id != user_by_email.id
            ):
                result.skipped += 1
                continue

            if (
                    user_by_oid is None
                    and user_by_email is not None
                    and user_by_email.entra_oid
                    not in (None, entra_id)
            ):
                result.skipped += 1
                continue

            user = user_by_oid or user_by_email
            created = user is None

            if created:
                user = User(
                    email=email,
                    display_name=(
                            entry.get("displayName")
                            or email
                    ),
                    entra_oid=entra_id,
                    is_active_flag=bool(
                        entry.get(
                            "accountEnabled",
                            True,
                        )
                    ),
                )
                db.session.add(user)
            else:
                user.email = email
                # noinspection PyUnresolvedReferences
                user.display_name = (
                        entry.get("displayName")
                        or user.display_name
                )
                user.entra_oid = entra_id
                user.is_active_flag = bool(
                    entry.get(
                        "accountEnabled",
                        True,
                    )
                )

            for attribute, key in (("first_name", "givenName"), ("last_name", "surname")):
                if key in entry:
                    setattr(user, attribute, str(entry[key] or "").strip() or None)

            if created:
                result.created += 1
            else:
                result.updated += 1

        url = data.get("@odata.nextLink")
        params = None

    db.session.commit()
    return result


def is_entra_user_in_allowed_group(
    user_id: str | None,
) -> bool:
    allowed_group_id = _configured_allowed_group_id()

    if not allowed_group_id:
        return True

    normalized_user_id = str(user_id or "").strip()

    if not normalized_user_id:
        return False

    access_token = _acquire_graph_access_token()
    encoded_user_id = quote(
        normalized_user_id,
        safe="",
    )
    response = requests.post(
        (
            f"{GRAPH_BASE_URL}/users/"
            f"{encoded_user_id}/checkMemberGroups"
        ),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json={
            "groupIds": [allowed_group_id],
        },
        timeout=30,
    )
    response.raise_for_status()

    group_ids = {
        str(value).strip().lower()
        for value in response.json().get("value", [])
    }
    return allowed_group_id.lower() in group_ids


def get_entra_user_profile(
    user_id: str,
) -> dict[str, Any]:
    normalized_user_id = str(user_id or "").strip()

    if not normalized_user_id:
        raise ValueError(
            "Die Entra-Benutzer-ID fehlt."
        )

    access_token = _acquire_graph_access_token()
    encoded_user_id = quote(
        normalized_user_id,
        safe="",
    )
    response = requests.get(
        (
            f"{GRAPH_BASE_URL}/users/"
            f"{encoded_user_id}"
        ),
        headers={
            "Authorization": f"Bearer {access_token}",
        },
        params={
            "$select": GRAPH_USER_FIELDS,
        },
        timeout=30,
    )
    response.raise_for_status()
    profile = response.json()

    if not isinstance(profile, dict):
        raise RuntimeError(
            "Das Entra-Benutzerprofil ist ungültig."
        )

    return profile


def _configured_allowed_group_id() -> str:
    return settings.get(
        SettingKeys.ENTRA_ALLOWED_GROUP_ID
    ).strip()


def _acquire_graph_access_token() -> str:
    tenant_id = settings.get(
        SettingKeys.ENTRA_TENANT_ID
    ).strip()
    client_id = settings.get(
        SettingKeys.ENTRA_CLIENT_ID
    ).strip()
    client_secret = settings.get(
        SettingKeys.ENTRA_CLIENT_SECRET
    ).strip()

    if not tenant_id or not client_id or not client_secret:
        raise RuntimeError(
            "Die Entra-Konfiguration ist unvollständig."
        )

    app = msal.ConfidentialClientApplication(
        client_id=client_id,
        client_credential=client_secret,
        authority=(
            "https://login.microsoftonline.com/"
            f"{tenant_id}"
        ),
    )
    token_result = app.acquire_token_for_client(
        scopes=[GRAPH_SCOPE],
    )
    access_token = token_result.get("access_token")

    if not access_token:
        raise RuntimeError(
            "Das Entra-Zugriffstoken konnte nicht abgerufen werden."
        )

    return str(access_token)
