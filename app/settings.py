from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Generic, TypeVar, cast

from sqlalchemy.exc import SQLAlchemyError

from .extensions import db
from .models import AppSetting, SettingValueType

T = TypeVar("T")


@dataclass(frozen=True)
class SettingDefinition(Generic[T]):
    key: str
    value_type: SettingValueType
    default: T
    label: str
    group: str
    secret: bool = False


class SettingKeys:
    APP_SECRET_KEY = SettingDefinition[str]("security.secret_key", SettingValueType.SECRET, "change-me", "Flask Secret Key", "Sicherheit", True)
    JWT_SECRET_KEY = SettingDefinition[str]("security.jwt_secret_key", SettingValueType.SECRET, "change-me", "JWT Secret Key", "Sicherheit", True)
    PASSWORD_LOGIN_ENABLED = SettingDefinition[bool]("auth.password_login_enabled", SettingValueType.BOOL, True, "Login mit Kennwort erlauben", "Anmeldung")
    MICROSOFT_LOGIN_ENABLED = SettingDefinition[bool]("auth.microsoft_login_enabled", SettingValueType.BOOL, False, "Microsoft-Login erlauben", "Anmeldung")
    MICROSOFT_AUTO_CREATE_USERS = SettingDefinition[bool]("auth.microsoft_auto_create_users", SettingValueType.BOOL, False, "Microsoft-Benutzer automatisch anlegen", "Anmeldung")
    MICROSOFT_ALLOWED_CLIENT_IDS = SettingDefinition[str]("auth.microsoft_allowed_client_ids", SettingValueType.STRING, "", "Erlaubte Microsoft Client-IDs fuer App-Login", "Anmeldung")
    ENTRA_TENANT_ID = SettingDefinition[str]("entra.tenant_id", SettingValueType.STRING, "", "Entra Tenant-ID", "Entra ID")
    ENTRA_CLIENT_ID = SettingDefinition[str]("entra.client_id", SettingValueType.STRING, "", "Entra Client-ID", "Entra ID")
    ENTRA_CLIENT_SECRET = SettingDefinition[str]("entra.client_secret", SettingValueType.SECRET, "", "Entra Client Secret", "Entra ID", True)
    WOWI_HOSTNAME = SettingDefinition[str]("wowi.hostname", SettingValueType.STRING, "", "Wowiport Hostname", "Wowiport")
    WOWI_USERNAME = SettingDefinition[str]("wowi.username", SettingValueType.STRING, "", "Wowiport Benutzer", "Wowiport")
    WOWI_PASSWORD = SettingDefinition[str]("wowi.password", SettingValueType.SECRET, "", "Wowiport Kennwort", "Wowiport", True)
    WOWI_API_KEY = SettingDefinition[str]("wowi.api_key", SettingValueType.SECRET, "", "OPENWOWI API-Key", "Wowiport", True)
    WOWI_API_VERSION = SettingDefinition[str]("wowi.api_version", SettingValueType.STRING, "v1.2", "OPENWOWI Version", "Wowiport")
    WOWI_APPROVED_STATUS_ID = SettingDefinition[int]("wowi.approved_status_id", SettingValueType.INT, 6, "Status-ID Freigegeben", "Wowiport")
    WOWI_WEBHOOK_TOKEN = SettingDefinition[str]("wowi.webhook_bearer_token", SettingValueType.SECRET, "change-me", "Webhook Bearer Token", "Wowiport", True)
    TIME_DEVIATION_PERCENT = SettingDefinition[float]("time.deviation_percent", SettingValueType.FLOAT, 20.0, "Abweichung in Prozent", "Zeiterfassung")
    TIME_DEVIATION_MINUTES = SettingDefinition[int]("time.deviation_minutes", SettingValueType.INT, 15, "Abweichung in Minuten", "Zeiterfassung")
    LOG_LEVEL = SettingDefinition[str]("logging.level", SettingValueType.STRING, "INFO", "Log-Level", "Logging")
    LOG_HANDLER = SettingDefinition[str]("logging.handler", SettingValueType.STRING, "file", "Logging-Ziel", "Logging")
    LOG_FILE_PATH = SettingDefinition[str]("logging.file_path", SettingValueType.STRING, "logs/datlaeuft.log", "Logdatei", "Logging")
    GRAYLOG_HOST = SettingDefinition[str]("logging.graylog_host", SettingValueType.STRING, "", "Graylog Host", "Logging")
    GRAYLOG_PORT = SettingDefinition[int]("logging.graylog_port", SettingValueType.INT, 12201, "Graylog Port", "Logging")
    CORS_ORIGINS = SettingDefinition[str]("api.cors_origins", SettingValueType.STRING, "*", "CORS Origins", "API")
    MOBILE_TOKEN_NEVER_EXPIRES = SettingDefinition[bool]("mobile.token_never_expires", SettingValueType.BOOL, True, "App-Login dauerhaft gültig", "Mobile App")
    MOBILE_ALLOW_COLLEAGUE_COMMISSIONS = SettingDefinition[bool]("mobile.allow_colleague_commissions", SettingValueType.BOOL, False, "Kollegen-Aufträge in der App anzeigen", "Mobile App")
    MOBILE_MICROSOFT_CLIENT_ID = SettingDefinition[str]("mobile.microsoft_client_id", SettingValueType.STRING, "", "Microsoft Client-ID der iOS-App", "Mobile App")
    MOBILE_MICROSOFT_REDIRECT_URI = SettingDefinition[str]("mobile.microsoft_redirect_uri", SettingValueType.STRING, "", "Microsoft Redirect-URI der iOS-App", "Mobile App")
    MOBILE_MICROSOFT_SCOPES = SettingDefinition[str]("mobile.microsoft_scopes", SettingValueType.STRING, "User.Read", "Microsoft Scopes der iOS-App", "Mobile App")


def all_definitions() -> list[SettingDefinition[Any]]:
    definitions: list[SettingDefinition[Any]] = []
    for value in vars(SettingKeys).values():
        if isinstance(value, SettingDefinition):
            definitions.append(value)
    return definitions


def grouped_definitions() -> dict[str, list[SettingDefinition[Any]]]:
    groups: dict[str, list[SettingDefinition[Any]]] = {}
    for definition in all_definitions():
        groups.setdefault(definition.group, []).append(definition)
    return groups


class SettingsService:
    def get(self, definition: SettingDefinition[T]) -> T:
        try:
            row = AppSetting.query.filter_by(key=definition.key).one_or_none()
        except SQLAlchemyError:
            db.session.rollback()
            return definition.default
        if row is None or row.value_text is None:
            return definition.default
        return cast(T, self._deserialize(row.value_text, definition.value_type))

    def set(self, definition: SettingDefinition[T], value: T) -> None:
        row = AppSetting.query.filter_by(key=definition.key).one_or_none()
        if row is None:
            row = AppSetting(key=definition.key, value_type=definition.value_type.value, label=definition.label, group=definition.group, is_secret=definition.secret)
            db.session.add(row)
        row.value_text = self._serialize(value, definition.value_type)
        row.value_type = definition.value_type.value
        row.label = definition.label
        row.group = definition.group
        row.is_secret = definition.secret

    def set_from_form(self, definition: SettingDefinition[Any], raw_value: str | None) -> None:
        if definition.secret and not raw_value:
            return
        value = self._deserialize(raw_value or "", definition.value_type)
        self.set(definition, value)

    def ensure_defaults(self) -> None:
        for definition in all_definitions():
            row = AppSetting.query.filter_by(key=definition.key).one_or_none()
            if row is None:
                self.set(definition, definition.default)
        db.session.commit()

    def _serialize(self, value: Any, value_type: SettingValueType) -> str:
        if value_type == SettingValueType.JSON:
            return json.dumps(value)
        if value_type == SettingValueType.BOOL:
            return "true" if bool(value) else "false"
        return str(value)

    def _deserialize(self, value: str, value_type: SettingValueType) -> Any:
        if value_type == SettingValueType.INT:
            return int(value or 0)
        if value_type == SettingValueType.FLOAT:
            return float(value or 0)
        if value_type == SettingValueType.BOOL:
            return str(value).lower() in {"1", "true", "yes", "on", "ja"}
        if value_type == SettingValueType.JSON:
            return json.loads(value or "null")
        return value


settings = SettingsService()