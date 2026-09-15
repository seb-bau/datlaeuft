from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Generic, TypeVar, cast

from sqlalchemy.exc import SQLAlchemyError

from .extensions import db
from .models import AppSetting, SettingValueType
from email.errors import HeaderParseError
from email.headerregistry import Address

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
    APP_SECRET_KEY = SettingDefinition[str](
        "security.secret_key", SettingValueType.SECRET, "change-me", "Flask Secret Key", "Sicherheit", True)
    JWT_SECRET_KEY = SettingDefinition[str](
        "security.jwt_secret_key", SettingValueType.SECRET, "change-me", "JWT Secret Key", "Sicherheit", True)
    PASSWORD_LOGIN_ENABLED = SettingDefinition[bool](
        "auth.password_login_enabled", SettingValueType.BOOL, True, "Login mit Kennwort erlauben", "Anmeldung")
    MICROSOFT_LOGIN_ENABLED = SettingDefinition[bool](
        "auth.microsoft_login_enabled", SettingValueType.BOOL, False, "Microsoft-Login erlauben", "Anmeldung")
    MICROSOFT_AUTO_CREATE_USERS = SettingDefinition[bool](
        "auth.microsoft_auto_create_users",
        SettingValueType.BOOL,
        False,
        "Microsoft-Benutzer automatisch anlegen",
        "Anmeldung"
    )
    MICROSOFT_ALLOWED_CLIENT_IDS = SettingDefinition[str](
        "auth.microsoft_allowed_client_ids",
        SettingValueType.STRING,
        "",
        "Erlaubte Microsoft Client-IDs fuer App-Login",
        "Anmeldung"
    )
    ENTRA_TENANT_ID = SettingDefinition[str](
        "entra.tenant_id", SettingValueType.STRING, "", "Entra Tenant-ID", "Entra ID")
    ENTRA_CLIENT_ID = SettingDefinition[str](
        "entra.client_id", SettingValueType.STRING, "", "Entra Client-ID", "Entra ID")
    ENTRA_CLIENT_SECRET = SettingDefinition[str](
        "entra.client_secret", SettingValueType.SECRET, "", "Entra Client Secret", "Entra ID", True)
    ENTRA_ALLOWED_GROUP_ID = SettingDefinition[str](
        "entra.allowed_group_id",
        SettingValueType.STRING,
        "",
        "Zugelassene Entra-Gruppen-Objekt-ID (optional)",
        "Entra ID",
    )
    WOWI_HOSTNAME = SettingDefinition[str](
        "wowi.hostname", SettingValueType.STRING, "", "Wowiport Hostname", "Wowiport")
    WOWI_USERNAME = SettingDefinition[str](
        "wowi.username", SettingValueType.STRING, "", "Wowiport Benutzer", "Wowiport")
    WOWI_PASSWORD = SettingDefinition[str](
        "wowi.password", SettingValueType.SECRET, "", "Wowiport Kennwort", "Wowiport", True)
    WOWI_API_KEY = SettingDefinition[str](
        "wowi.api_key", SettingValueType.SECRET, "", "OPENWOWI API-Key", "Wowiport", True)
    WOWI_API_VERSION = SettingDefinition[str](
        "wowi.api_version", SettingValueType.STRING, "v1.2", "OPENWOWI Version", "Wowiport")
    WOWI_DOCUMENT_FILE_TYPE_ID = SettingDefinition[int](
        "wowi.document_file_type_id",
        SettingValueType.INT,
        62,
        "Dateiart für hochgeladene Auftragsdateien",
        "Wowiport",
    )

    WOWI_DOCUMENT_PRIVACY_CATEGORY_ID = SettingDefinition[int](
        "wowi.document_privacy_category_id",
        SettingValueType.INT,
        3,
        "Datenschutzkategorie für Auftragsdateien",
        "Wowiport",
    )

    WOWI_DOCUMENT_SYNC_INTERVAL_MINUTES = SettingDefinition[int](
        "wowi.document_sync_interval_minutes",
        SettingValueType.INT,
        60,
        "Abgleichintervall für Auftragsdateien in Minuten",
        "Synchronisierung",
    )

    WOWI_MEDIA_SYNC_INTERVAL_MINUTES = SettingDefinition[int](
        "wowi.media_sync_interval_minutes",
        SettingValueType.INT,
        60,
        "Abgleichintervall für Gebäude- und Wohnungsfotos in Minuten",
        "Synchronisierung",
    )
    PROCESS_INFO_SYNC_INTERVAL_MINUTES = SettingDefinition[int](
        "wowi.process_info_sync_interval_minutes",
        SettingValueType.INT,
        5,
        "Abgleichintervall für Vorgangsinformationen in Minuten",
        "Synchronisierung",
    )
    WOWI_APPROVED_STATUS_ID = SettingDefinition[int](
        "wowi.approved_status_id", SettingValueType.INT, 6, "Status-ID Freigegeben", "Wowiport")
    WOWI_WEBHOOK_TOKEN = SettingDefinition[str](
        "wowi.webhook_bearer_token", SettingValueType.SECRET, "change-me", "Webhook Bearer Token", "Wowiport", True)
    WOWI_DEFAULT_CRAFTS_PROCESS_TYPE_CODE = SettingDefinition[str](
        "wowi.default_crafts_process_type_code",
        SettingValueType.STRING,
        "Instandhaltung",
        "Standard-Vorgangsart",
        "Wowiport",
    )
    WOWI_INITIAL_CRAFTS_PROCESS_STATUS_CODE = SettingDefinition[str](
        "wowi.initial_crafts_process_status_code",
        SettingValueType.STRING,
        "Offen",
        "Anfangsstatus des Vorgangs",
        "Wowiport",
    )
    WOWI_INTERNAL_SALES_TAX_CODE = SettingDefinition[str](
        "wowi.internal_sales_tax_code",
        SettingValueType.STRING,
        "V0",
        "Umsatzsteuercode für interne Handwerker",
        "Wowiport",
    )
    WOWI_ALLOWED_COMMISSION_TYPE_IDS = SettingDefinition[list](
        "wowi.allowed_commission_type_ids",
        SettingValueType.JSON,
        [],
        "Erlaubte Auftragsarten",
        "Wowiport",
    )
    WOWI_VISIBLE_COMMISSION_STATUS_IDS = SettingDefinition[list](
        "wowi.visible_commission_status_ids",
        SettingValueType.JSON,
        [12, 7, 9, 11],
        "Zu synchronisierende Auftragsstatus",
        "Wowiport",
    )
    WOWI_USER_EMAIL_DOMAIN_WHITELIST = SettingDefinition[str](
        "wowi.user_email_domain_whitelist",
        SettingValueType.STRING,
        "",
        "Erlaubte E-Mail-Domains für Handwerker (kommagetrennt)",
        "Wowiport",
    )
    DMS_HOSTNAME = SettingDefinition[str](
        "dms.hostname",
        SettingValueType.STRING,
        "",
        "d.velop Hostname",
        "d.velop",
    )

    DMS_API_KEY = SettingDefinition[str](
        "dms.api_key",
        SettingValueType.SECRET,
        "",
        "d.velop API-Key",
        "d.velop",
        True,
    )

    DMS_REPOSITORY = SettingDefinition[str](
        "dms.repository",
        SettingValueType.STRING,
        "",
        "d.velop Repository-ID",
        "d.velop",
    )

    DMS_INVOICE_CATEGORY_ID = SettingDefinition[str](
        "dms.invoice_category_id",
        SettingValueType.STRING,
        "",
        "Kategorie-ID für Rechnungen",
        "d.velop",
    )

    DMS_INVOICE_ID_PROPERTY_ID = SettingDefinition[str](
        "dms.invoice_id_property_id",
        SettingValueType.STRING,
        "",
        "Eigenschafts-ID für den eindeutigen Rechnungsschlüssel",
        "d.velop",
    )

    DMS_INVOICE_PROPERTY_MAP = SettingDefinition[dict](
        "dms.invoice_property_map",
        SettingValueType.JSON,
        {},
        "Weitere Eigenschaften für Rechnungen",
        "d.velop",
    )
    DMS_OFFER_CATEGORY_ID = SettingDefinition[str](
        "dms.offer_category_id",
        SettingValueType.STRING,
        "",
        "Kategorie-ID für Angebote",
        "d.velop",
    )
    DMS_OFFER_ID_PROPERTY_ID = SettingDefinition[str](
        "dms.offer_id_property_id",
        SettingValueType.STRING,
        "",
        "Eigenschafts-ID für den Angebotsschlüssel",
        "d.velop",
    )
    DMS_OFFER_PROPERTY_MAP = SettingDefinition[dict](
        "dms.offer_property_map",
        SettingValueType.JSON,
        {},
        "Weitere Eigenschaften für Angebote",
        "d.velop",
    )
    DMS_SAFETY_ARCHIVE_ENABLED = (
        SettingDefinition[bool](
            "dms.safety_archive_enabled",
            SettingValueType.BOOL,
            False,
            (
                "Prüfprotokolle automatisch "
                "archivieren"
            ),
            "d.velop",
        )
    )
    DMS_SAFETY_ECONOMIC_UNIT_CATEGORY_ID = (
        SettingDefinition[str](
            (
                "dms.safety_economic_unit_"
                "category_id"
            ),
            SettingValueType.STRING,
            "",
            (
                "Kategorie-ID für Prüfprotokolle "
                "an Wirtschaftseinheiten"
            ),
            "d.velop",
        )
    )
    DMS_SAFETY_BUILDING_CATEGORY_ID = (
        SettingDefinition[str](
            "dms.safety_building_category_id",
            SettingValueType.STRING,
            "",
            (
                "Kategorie-ID für Prüfprotokolle "
                "an Gebäuden"
            ),
            "d.velop",
        )
    )
    DMS_SAFETY_USE_UNIT_CATEGORY_ID = (
        SettingDefinition[str](
            "dms.safety_use_unit_category_id",
            SettingValueType.STRING,
            "",
            (
                "Kategorie-ID für Prüfprotokolle "
                "an Nutzungseinheiten"
            ),
            "d.velop",
        )
    )
    DMS_SAFETY_PROTOCOL_ID_PROPERTY_ID = (
        SettingDefinition[str](
            (
                "dms.safety_protocol_id_"
                "property_id"
            ),
            SettingValueType.STRING,
            "",
            (
                "Eigenschafts-ID für den "
                "eindeutigen Protokollschlüssel"
            ),
            "d.velop",
        )
    )
    DMS_SAFETY_PROPERTY_MAP = (
        SettingDefinition[dict](
            "dms.safety_property_map",
            SettingValueType.JSON,
            {},
            (
                "Weitere Eigenschaften für "
                "Prüfprotokolle"
            ),
            "d.velop",
        )
    )
    SMTP_HOST = SettingDefinition[str](
        "smtp.host",
        SettingValueType.STRING,
        "",
        "SMTP-Server",
        "E-Mail",
    )
    SMTP_PORT = SettingDefinition[int](
        "smtp.port",
        SettingValueType.INT,
        25,
        "SMTP-Port",
        "E-Mail",
    )
    SMTP_SENDER = SettingDefinition[str](
        "smtp.sender",
        SettingValueType.STRING,
        "",
        "Absenderadresse",
        "E-Mail",
    )
    SMTP_DEBUG_RECIPIENT = SettingDefinition[str](
        "smtp.debug_recipient",
        SettingValueType.STRING,
        "",
        "Debug-E-Mail-Adresse (leer = normaler Versand)",
        "E-Mail",
    )
    PUBLIC_BASE_URL = SettingDefinition[str](
        "application.public_base_url",
        SettingValueType.STRING,
        "",
        "Öffentliche Basis-URL",
        "E-Mail",
    )
    APPOINTMENT_EMAIL_INCLUDE_TOPIC = SettingDefinition[bool](
        "appointments.email_include_topic",
        SettingValueType.BOOL,
        True,
        "Auftragsthema in Termin-E-Mails anzeigen",
        "Terminvereinbarung",
    )

    APPOINTMENT_LINK_VALID_DAYS = SettingDefinition[int](
        "appointments.link_valid_days",
        SettingValueType.INT,
        30,
        "Gültigkeit von Terminlinks in Tagen",
        "Terminvereinbarung",
    )

    APPOINTMENT_TIMEZONE = SettingDefinition[str](
        "appointments.timezone",
        SettingValueType.STRING,
        "Europe/Berlin",
        "Zeitzone",
        "Terminvereinbarung",
    )
    TIME_DEVIATION_PERCENT = SettingDefinition[float](
        "time.deviation_percent", SettingValueType.FLOAT, 20.0, "Abweichung in Prozent", "Zeiterfassung")
    TIME_DEVIATION_MINUTES = SettingDefinition[int](
        "time.deviation_minutes", SettingValueType.INT, 15, "Abweichung in Minuten", "Zeiterfassung")
    INVOICE_DEFAULT_HOURLY_RATE_NET = SettingDefinition[float](
        "invoice.default_hourly_rate_net",
        SettingValueType.FLOAT,
        0.0,
        "Standard-Stundensatz netto",
        "Rechnungen",
    )
    INVOICE_DEFAULT_VAT_RATE = SettingDefinition[float](
        "invoice.default_vat_rate",
        SettingValueType.FLOAT,
        19.0,
        "Standard-Umsatzsteuersatz",
        "Rechnungen",
    )
    INVOICE_MATERIAL_MARKUP_PERCENT = SettingDefinition[float](
        "invoice.material_markup_percent",
        SettingValueType.FLOAT,
        0.0,
        "Standard-Materialaufschlag in Prozent",
        "Rechnungen",
    )
    INVOICE_PAYMENT_TERMS_DAYS = SettingDefinition[int](
        "invoice.payment_terms_days",
        SettingValueType.INT,
        14,
        "Standard-Zahlungsziel in Tagen",
        "Rechnungen",
    )
    OFFER_VALIDITY_DAYS = SettingDefinition[int](
        "offer.validity_days",
        SettingValueType.INT,
        30,
        "Standardgültigkeit in Tagen",
        "Angebote",
    )
    OFFER_COMMISSION_TYPE_ID = SettingDefinition[int](
        "offer.commission_type_id",
        SettingValueType.INT,
        0,
        "Auftragsart für angenommene Angebote",
        "Angebote",
    )
    LOG_LEVEL = SettingDefinition[str](
        "logging.level", SettingValueType.STRING, "INFO", "Log-Level", "Logging")
    LOG_HANDLER = SettingDefinition[str](
        "logging.handler", SettingValueType.STRING, "file", "Logging-Ziel", "Logging")
    LOG_FILE_PATH = SettingDefinition[str](
        "logging.file_path", SettingValueType.STRING, "logs/datlaeuft.log", "Logdatei", "Logging")
    GRAYLOG_HOST = SettingDefinition[str](
        "logging.graylog_host", SettingValueType.STRING, "", "Graylog Host", "Logging")
    GRAYLOG_PORT = SettingDefinition[int](
        "logging.graylog_port", SettingValueType.INT, 12201, "Graylog Port", "Logging")
    CORS_ORIGINS = SettingDefinition[str](
        "api.cors_origins", SettingValueType.STRING, "*", "CORS Origins", "API")
    MOBILE_TOKEN_NEVER_EXPIRES = SettingDefinition[bool](
        "mobile.token_never_expires", SettingValueType.BOOL, True, "App-Login dauerhaft gültig", "Mobile App")
    MOBILE_ALLOW_COLLEAGUE_COMMISSIONS = SettingDefinition[bool](
        "mobile.allow_colleague_commissions",
        SettingValueType.BOOL,
        False,
        "Kollegen-Aufträge in der App anzeigen",
        "Mobile App"
    )
    MOBILE_MICROSOFT_CLIENT_ID = SettingDefinition[str](
        "mobile.microsoft_client_id", SettingValueType.STRING, "", "Microsoft Client-ID der iOS-App", "Mobile App")
    MOBILE_MICROSOFT_REDIRECT_URI = SettingDefinition[str](
        "mobile.microsoft_redirect_uri",
        SettingValueType.STRING,
        "",
        "Microsoft Redirect-URI der iOS-App",
        "Mobile App"
    )
    MOBILE_MICROSOFT_SCOPES = SettingDefinition[str](
        "mobile.microsoft_scopes", SettingValueType.STRING, "User.Read", "Microsoft Scopes der iOS-App", "Mobile App")
    PUSH_RELAY_URL = SettingDefinition[str](
        "push.relay_url",
        SettingValueType.STRING,
        "",
        "Basis-URL des Push-Providers",
        "Push-Mitteilungen",
    )
    PUSH_RELAY_API_KEY = SettingDefinition[str](
        "push.relay_api_key",
        SettingValueType.SECRET,
        "",
        "API-Key des Push-Providers",
        "Push-Mitteilungen",
        True,
    )
    PUSH_RELAY_TIMEOUT_SECONDS = SettingDefinition[float](
        "push.relay_timeout_seconds",
        SettingValueType.FLOAT,
        10.0,
        "Timeout des Push-Providers in Sekunden",
        "Push-Mitteilungen",
    )
    PUSH_MAX_ATTEMPTS = SettingDefinition[int](
        "push.max_attempts",
        SettingValueType.INT,
        8,
        "Maximale Zustellversuche je Gerät",
        "Push-Mitteilungen",
    )
    GENERIC_OIDC_ENABLED = SettingDefinition[bool](
        "auth.generic_oidc_enabled",
        SettingValueType.BOOL,
        False,
        "Generischen OIDC-Login erlauben",
        "Anmeldung",
    )
    GENERIC_OIDC_PROVIDER_NAME = SettingDefinition[str](
        "auth.generic_oidc_provider_name",
        SettingValueType.STRING,
        "Single Sign-on",
        "Name des OIDC-Anbieters",
        "Anmeldung",
    )
    GENERIC_OIDC_DISCOVERY_URL = SettingDefinition[str](
        "auth.generic_oidc_discovery_url",
        SettingValueType.STRING,
        "",
        "OIDC Discovery-URL",
        "Anmeldung",
    )
    GENERIC_OIDC_CLIENT_ID = SettingDefinition[str](
        "auth.generic_oidc_client_id",
        SettingValueType.STRING,
        "",
        "OIDC Client-ID",
        "Anmeldung",
    )
    GENERIC_OIDC_CLIENT_SECRET = SettingDefinition[str](
        "auth.generic_oidc_client_secret",
        SettingValueType.SECRET,
        "",
        "OIDC Client-Secret",
        "Anmeldung",
        True,
    )
    GENERIC_OIDC_SCOPES = SettingDefinition[str](
        "auth.generic_oidc_scopes",
        SettingValueType.STRING,
        "openid profile email",
        "OIDC Scopes",
        "Anmeldung",
    )
    GENERIC_OIDC_AUTO_CREATE_USERS = SettingDefinition[bool](
        "auth.generic_oidc_auto_create_users",
        SettingValueType.BOOL,
        False,
        "OIDC-Benutzer automatisch anlegen",
        "Anmeldung",
    )
    SYNC_BUSINESS_YEAR_START_MONTH = SettingDefinition[int](
        "sync.business_year_start_month",
        SettingValueType.INT,
        1,
        "Startmonat des Geschäftsjahres",
        "Synchronisierung",
    )

    SYNC_COMMISSION_DATE_FIELD = SettingDefinition[str](
        "sync.commission_date_field",
        SettingValueType.STRING,
        "recording_date",
        "Datumsfeld für Auftragssync-Geschäftsjahr",
        "Synchronisierung",
    )

    GEOAPIFY_API_KEY = SettingDefinition[str](
        "geoapify.api_key",
        SettingValueType.STRING,
        "",
        "Geoapify API-Key",
        "Geocoding",
    )

    GEOCODING_ENABLED = SettingDefinition[bool](
        "geocoding.enabled",
        SettingValueType.BOOL,
        True,
        "Geocoding aktivieren",
        "Geocoding",
    )

    GEOCODING_REQUEST_INTERVAL_SECONDS = SettingDefinition[float](
        "geocoding.request_interval_seconds",
        SettingValueType.FLOAT,
        0.25,
        "Mindestabstand zwischen Geocoding-Anfragen",
        "Geocoding",
    )


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


def validate_debug_email_address(value: str) -> str:
    error_message = (
        "Bitte genau eine gültige Debug-E-Mail-Adresse ohne "
        "Anzeigenamen eingeben oder das Feld leer lassen."
    )

    if not isinstance(value, str) or any(
        ord(character) < 32 or ord(character) == 127
        for character in value
    ):
        raise ValueError(error_message)

    value = value.strip()

    if not value:
        return ""

    try:
        address = Address(addr_spec=value)
    except (ValueError, HeaderParseError) as exc:
        raise ValueError(error_message) from exc

    if (
        not value.isascii()
        or len(value) > 254
        or not address.username
        or not address.domain
        or "." not in address.domain
        or address.addr_spec != value
    ):
        raise ValueError(error_message)

    return address.addr_spec


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
        if definition.key == SettingKeys.SMTP_DEBUG_RECIPIENT.key:
            value = cast(T, validate_debug_email_address(cast(str, value)))
        row = AppSetting.query.filter_by(key=definition.key).one_or_none()
        if row is None:
            row = AppSetting(key=definition.key, value_type=definition.value_type.value, label=definition.label,
                             group=definition.group, is_secret=definition.secret)
            db.session.add(row)
        row.value_text = self._serialize(value, definition.value_type)
        row.value_type = definition.value_type.value
        row.label = definition.label
        row.group = definition.group
        row.is_secret = definition.secret

    def set_from_form(self, definition: SettingDefinition[Any], raw_value: str | None) -> None:
        if (
            definition.key == SettingKeys.SMTP_DEBUG_RECIPIENT.key
            and raw_value is None
        ):
            return
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
        if self.__dict__:
            pass
        if value_type == SettingValueType.JSON:
            return json.dumps(value)
        if value_type == SettingValueType.BOOL:
            return "true" if bool(value) else "false"
        return str(value)

    def _deserialize(self, value: str, value_type: SettingValueType) -> Any:
        if self.__dict__:
            pass
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
