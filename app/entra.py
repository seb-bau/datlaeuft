from __future__ import annotations

from dataclasses import dataclass

import msal
import requests

from .extensions import db
from .models import User
from .settings import SettingKeys, settings


@dataclass
class EntraImportResult:
    created: int = 0
    updated: int = 0
    skipped: int = 0


def import_entra_users() -> EntraImportResult:
    tenant_id = settings.get(SettingKeys.ENTRA_TENANT_ID)
    client_id = settings.get(SettingKeys.ENTRA_CLIENT_ID)
    client_secret = settings.get(SettingKeys.ENTRA_CLIENT_SECRET)
    app = msal.ConfidentialClientApplication(client_id=client_id, client_credential=client_secret, authority=f"https://login.microsoftonline.com/{tenant_id}")
    token_result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" not in token_result:
        raise RuntimeError(str(token_result))
    headers = {"Authorization": f"Bearer {token_result['access_token']}"}
    url = "https://graph.microsoft.com/v1.0/users?$select=id,displayName,mail,userPrincipalName,accountEnabled"
    result = EntraImportResult()
    while url:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        for entry in data.get("value", []):
            email = (entry.get("mail") or entry.get("userPrincipalName") or "").strip().lower()
            if not email:
                result.skipped += 1
                continue
            user = User.query.filter((User.entra_oid == entry.get("id")) | (User.email == email)).one_or_none()
            if user is None:
                user = User(email=email, display_name=entry.get("displayName") or email, entra_oid=entry.get("id"), is_active_flag=bool(entry.get("accountEnabled", True)))
                db.session.add(user)
                result.created += 1
            else:
                user.email = email
                user.display_name = entry.get("displayName") or user.display_name
                user.entra_oid = entry.get("id") or user.entra_oid
                user.is_active_flag = bool(entry.get("accountEnabled", True))
                result.updated += 1
        url = data.get("@odata.nextLink")
    db.session.commit()
    return result