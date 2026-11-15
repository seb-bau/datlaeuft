from urllib.parse import urlencode, urlsplit, urlunsplit

from .settings import SettingKeys, settings


def commission_wowiport_url(commission):
    if commission.erp_id is None or commission.erp_id <= 0 or not commission.erp_idnum:
        return None
    hostname = settings.get(SettingKeys.WOWI_HOSTNAME).strip()
    if not hostname or any(character.isspace() or ord(character) < 32 for character in hostname):
        return None
    try:
        parsed = urlsplit(hostname if "://" in hostname else "https://" + hostname)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        parsed.port
    except ValueError:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, "/EntityOpen", urlencode({
        "entityName": "Commission", "id": commission.erp_id,
    }), ""))
