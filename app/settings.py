from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Generic, TypeVar, cast
from string import Formatter

from sqlalchemy.exc import SQLAlchemyError

from .extensions import db
from .models import AppSetting, Commission, SettingValueType
from email.errors import HeaderParseError
from email.headerregistry import Address
from .health_rules import DEFAULT_HEALTH_RULES, normalize_health_rules
from .invoice_cover_letter_layout import DEFAULT_COVER_LETTER_LAYOUT, validate_cover_letter_layout
from .invoice_layout import validate_invoice_layout
from .offer_layout import validate_offer_layout

T = TypeVar("T")

LEGACY_TENANT_CHARGE_CLOSING_TEMPLATE = "Mit freundlichen Grüßen\n{seller_name}"


@dataclass(frozen=True)
class SettingDefinition(Generic[T]):
    key: str
    value_type: SettingValueType
    default: T
    label: str
    group: str
    secret: bool = False


SMS_DEBUG_SUPPRESSED_COMMISSION_ID = SettingDefinition[int](
    "sms.debug_suppressed_commission_id", SettingValueType.INT, 0,
    "SMS durch Debug-E-Mail gesperrt bis Auftrag", "SMS",
)


def normalize_external_portal_name(value: Any) -> str:
    name = str(value or "")
    if any(ord(character) < 32 or ord(character) == 127 for character in name):
        raise ValueError(
            "Der Portalname darf keine Zeilenumbrüche oder Steuerzeichen enthalten."
        )

    name = name.strip()
    if len(name) > 120:
        raise ValueError("Der Portalname darf höchstens 120 Zeichen lang sein.")

    return name or SettingKeys.EXTERNAL_PORTAL_NAME.default


def normalize_ticket_source_ids(value: Any) -> list[int]:
    if not isinstance(value, list) or any(
        type(source_id) is not int or source_id <= 0
        for source_id in value
    ):
        raise ValueError(
            "Ticketquellen müssen als Liste positiver Wowiport-IDs angegeben werden."
        )
    return sorted(set(value))


class SettingKeys:
    HEALTH_RULES = SettingDefinition[dict[str, Any]](
        "health.rules", SettingValueType.JSON, DEFAULT_HEALTH_RULES,
        "Health-Grenzen und aktuelle Bewertung", "Health",
    )
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

    WOWI_DEFAULT_COMMISSION_TYPE_ID = (
        SettingDefinition[int](
            "wowi.default_commission_type_id",
            SettingValueType.INT,
            0,
            "Standard-Auftragsart",
            "Wowiport",
        )
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
    DMS_ENABLED = SettingDefinition[bool](
        "dms.enabled",
        SettingValueType.BOOL,
        False,
        "Automatische Archivierung",
        "d.velop",
    )
    DMS_PROCESSING_PAUSED = SettingDefinition[bool](
        "dms.processing_paused",
        SettingValueType.BOOL,
        False,
        "PDF-Übertragung pausieren",
        "d.velop",
    )
    DMS_METADATA = SettingDefinition[dict](
        "dms.metadata",
        SettingValueType.JSON,
        {},
        "Repository-Metadaten",
        "d.velop",
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

    DMS_POSTAL_PROPERTY = SettingDefinition[str](
        "dms.postal_property", SettingValueType.STRING, "", "Eigenschaft für die Poststelle", "Archiv/DMS",
    )
    DMS_PORTAL_PROPERTY = SettingDefinition[str](
        "dms.portal_property", SettingValueType.STRING, "", "Eigenschaft für die Mieter-App", "Archiv/DMS",
    )
    DMS_POSTAL_DEFAULT = SettingDefinition[bool](
        "dms.postal_default", SettingValueType.BOOL, True, "Poststelle vorauswählen", "Archiv/DMS",
    )
    DMS_PORTAL_DEFAULT = SettingDefinition[bool](
        "dms.portal_default", SettingValueType.BOOL, True, "Mieter-App vorauswählen", "Archiv/DMS",
    )
    DMS_POSTAL_SIGNER_PROPERTY = SettingDefinition[str](
        "dms.postal_signer_property", SettingValueType.STRING, "property_remark1", "Feld-ID für den Poststellenbenutzer", "Archiv/DMS",
    )
    DMS_POSTAL_SYSTEM_USER_ID = SettingDefinition[str](
        "dms.postal_system_user_id", SettingValueType.STRING, "", "d.velop-ID des Paperwork-Systembenutzers", "Archiv/DMS",
    )
    DMS_POSTAL_ALTERNATE_SIGNER_PROPERTY = SettingDefinition[str](
        "dms.postal_alternate_signer_property", SettingValueType.STRING, "", "Feld-ID für abweichende Unterschriften", "Archiv/DMS",
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
    EXTERNAL_PORTAL_NAME = SettingDefinition[str](
        "external_portal.display_name",
        SettingValueType.STRING,
        "MachMa!",
        "Anzeigename für externe Handwerker (leer = MachMa!)",
        "Auftragnehmerportal",
    )
    SMTP_HOST = SettingDefinition[str](
        "smtp.host",
        SettingValueType.STRING,
        "",
        "SMTP-Server",
        "E-Mail",
    )
    EXTERNAL_PORTAL_LIGHT_DEFAULT = SettingDefinition[bool](
        "external_portal.light_default", SettingValueType.BOOL, False,
        "Portal light und Antwort-Schaltflächen in Handwerker-Mails standardmäßig aktivieren",
        "Auftragnehmerportal",
    )
    SMTP_EXTERNAL_DEBUG_RECIPIENT = SettingDefinition[str](
        "smtp.external_debug_recipient", SettingValueType.STRING, "",
        "Debug-E-Mail-Adresse für externe Handwerker", "E-Mail",
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
    COMMISSION_NOTIFY_TENANTS_DEFAULT = SettingDefinition[bool](
        "commissions.notify_tenants_default",
        SettingValueType.BOOL,
        False,
        "Mieter bei Beauftragung standardmäßig informieren",
        "E-Mail",
    )
    COMMISSION_TENANT_CHARGE_TYPE_IDS = SettingDefinition[list](
        "commissions.tenant_charge_type_ids", SettingValueType.JSON, [],
        "Auftragsarten mit Mieterbelastung", "Aufträge",
    )
    SMS_ENABLED = SettingDefinition[bool](
        "sms.enabled", SettingValueType.BOOL, False,
        "SMS zusätzlich zu Mieter-E-Mails bei Beauftragung versenden", "SMS",
    )
    SMS_API_KEY = SettingDefinition[str](
        "sms.api_key", SettingValueType.STRING, "", "seven API-Key (SMS und Lookup)", "SMS", secret=True,
    )
    SMS_SENDER = SettingDefinition[str](
        "sms.sender", SettingValueType.STRING, "", "SMS-Absenderkennung (optional)", "SMS",
    )
    TENANT_TICKET_REPLY_TO = SettingDefinition[str](
        "commissions.ticket_reply_to", SettingValueType.STRING, "",
        "Antwortadresse für Ticket-Mails (mail2ticket-Postfach)", "E-Mail",
    )
    TENANT_TICKET_WRITEBACK_ENABLED = SettingDefinition[bool](
        "commissions.ticket_writeback_enabled", SettingValueType.BOOL, True,
        "Beauftragungsmails in Wowiport-Tickets dokumentieren", "E-Mail",
    )
    TENANT_TICKET_WRITEBACK_EXCLUDED_SOURCE_IDS = SettingDefinition[list[int]](
        "commissions.ticket_writeback_excluded_source_ids",
        SettingValueType.JSON,
        [],
        "Ticketquellen ohne Bestätigung im Ticket",
        "E-Mail",
    )
    TENANT_TICKET_REPLY_DELIMITER = SettingDefinition[str](
        "commissions.ticket_reply_delimiter", SettingValueType.STRING,
        "--- Bitte antworten Sie oberhalb dieser Zeile ---",
        "Antwort-Trennzeile für Ticket-Mails", "E-Mail",
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

    APPOINTMENT_CUSTOM_TIMES_ENABLED = SettingDefinition[bool](
        "appointments.custom_times_enabled",
        SettingValueType.BOOL,
        False,
        "Konkrete Uhrzeiten in der App erlauben",
        "Terminvereinbarung",
    )

    APPOINTMENT_CUSTOM_TIME_DURATION_MINUTES = SettingDefinition[int](
        "appointments.custom_time_duration_minutes",
        SettingValueType.INT,
        60,
        "Kalenderdauer bei konkreten Uhrzeiten in Minuten",
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
    INVOICE_INTRODUCTORY_TEXT = SettingDefinition[str](
        "invoice.introductory_text", SettingValueType.STRING,
        "Für die ausgeführten Arbeiten berechnen wir Ihnen folgende Leistungen:",
        "Einleitungstext", "Rechnungen",
    )
    INVOICE_OFFER_INTRODUCTORY_TEXT = SettingDefinition[str](
        "invoice.offer_introductory_text", SettingValueType.STRING,
        "Für die ausgeführten und im Angebot {offer_number} vereinbarten Leistungen berechnen wir:",
        "Einleitungstext bei Rechnungen aus Angeboten", "Rechnungen",
    )
    INVOICE_CONCLUDING_TEXT = SettingDefinition[str](
        "invoice.concluding_text", SettingValueType.STRING, "", "Schlusstext", "Rechnungen",
    )
    INVOICE_TRANSFER_PAYMENT_TEXT = SettingDefinition[str](
        "invoice.transfer_payment_text", SettingValueType.STRING,
        "Bitte überweisen Sie den Rechnungsbetrag bis zum {due_date} auf das unten angegebene Konto. Bitte geben Sie als Verwendungszweck {payment_reference} an.",
        "Zahlungstext für Überweisung", "Rechnungen",
    )
    INVOICE_SEPA_PAYMENT_TEXT = SettingDefinition[str](
        "invoice.sepa_payment_text", SettingValueType.STRING,
        "Der Rechnungsbetrag wird am {due_date} über das bestehende SEPA-Mandat von Ihrem unten angegebenen Konto eingezogen.",
        "Zahlungstext für SEPA-Mandat", "Rechnungen",
    )
    INVOICE_COVER_LETTER_TITLE = SettingDefinition[str](
        "invoice.cover_letter_title", SettingValueType.STRING, "Ihre Rechnung {invoice_number}",
        "Anschreiben: Standardüberschrift", "Rechnungen",
    )
    INVOICE_COVER_LETTER_TEXT = SettingDefinition[str](
        "invoice.cover_letter_text", SettingValueType.STRING,
        "Sehr geehrte Damen und Herren,\n\n"
        "beigefügt erhalten Sie unsere Rechnung {invoice_number} über {amount} {currency}.\n\n"
        "{payment_terms}\n\nFür Rückfragen stehen wir Ihnen gerne zur Verfügung.\n\n"
        "Mit freundlichen Grüßen\n{seller_name}\n{sig}",
        "Anschreiben: Standardtext", "Rechnungen",
    )
    INVOICE_TENANT_CHARGE_CUTOFF_DAY = SettingDefinition[int](
        "invoice.tenant_charge_cutoff_day", SettingValueType.INT, 16,
        "Mieteranschreiben: Stichtag für die Fälligkeit im übernächsten Monat", "Rechnungen",
    )
    INVOICE_TENANT_CHARGE_PURPOSE_TEMPLATE = SettingDefinition[str](
        "invoice.tenant_charge_purpose_template", SettingValueType.STRING,
        "Für die ausgeführten Arbeiten berechnen wir Ihnen die Kosten gemäß der beigefügten Rechnung.\n{work_description}",
        "Standardtext für „Anlass der Belastung“", "Rechnungen",
    )
    INVOICE_TENANT_CHARGE_CLOSING_TEMPLATE = SettingDefinition[str](
        "invoice.tenant_charge_closing_template", SettingValueType.STRING,
        LEGACY_TENANT_CHARGE_CLOSING_TEMPLATE + "\n{sig}",
        "Standardgrußformel für Mieteranschreiben", "Rechnungen",
    )
    INVOICE_COVER_LETTER_LAYOUT = SettingDefinition[str](
        "invoice.cover_letter_layout", SettingValueType.STRING, DEFAULT_COVER_LETTER_LAYOUT,
        "Anschreiben: Aufbau und Gestaltung", "Rechnungen",
    )
    INVOICE_LAYOUT = SettingDefinition[str](
        "invoice.layout", SettingValueType.STRING, "",
        "Rechnung: Aufbau und Gestaltung", "Rechnungen",
    )
    OFFER_LAYOUT = SettingDefinition[str](
        "offer.layout", SettingValueType.STRING, "", "Angebot: Aufbau und Gestaltung", "Angebote",
    )
    OFFER_INTRODUCTORY_TEXT = SettingDefinition[str](
        "offer.introductory_text", SettingValueType.STRING,
        "Vielen Dank für Ihre Anfrage. Wir bieten Ihnen folgende Leistungen an:",
        "Einleitungstext", "Angebote",
    )
    OFFER_CONCLUDING_TEXT = SettingDefinition[str](
        "offer.concluding_text", SettingValueType.STRING,
        "Wir freuen uns auf Ihre Rückmeldung.", "Schlusstext", "Angebote",
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
    MOBILE_TIME_COMPONENTS_ENABLED = SettingDefinition[bool](
        "mobile.time_components_enabled",
        SettingValueType.BOOL,
        True,
        "Zeitbausteine bei Aufträgen in der App aktivieren",
        "Mobile App",
    )
    TIME_ALLOW_ON_COMPLETED_COMMISSIONS = SettingDefinition[bool](
        "time.allow_on_completed_commissions",
        SettingValueType.BOOL,
        True,
        "Neue Zeiten auf abgeschlossenen Aufträgen erlauben",
        "Zeiterfassung",
    )
    MOBILE_SHOW_SERVICE_TARGET_TIMES = SettingDefinition[bool](
        "mobile.show_service_target_times",
        SettingValueType.BOOL,
        True,
        "SOLL-Zeiten für Leistungspositionen in der App anzeigen",
        "Mobile App",
    )
    MOBILE_SHOW_WORK_TIME_TARGET_TIMES = SettingDefinition[bool](
        "mobile.show_work_time_target_times",
        SettingValueType.BOOL,
        True,
        "SOLL-Zeiten für die Zeiterfassung in der App anzeigen",
        "Mobile App",
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
    PUSH_DEFAULT_NEW_COMMISSION = SettingDefinition[bool]("push.default.new_commission", SettingValueType.BOOL, True, "Neuer Auftrag", "Push-Standardwerte")
    PUSH_DEFAULT_APPOINTMENT_ACCEPTED = SettingDefinition[bool]("push.default.appointment_accepted", SettingValueType.BOOL, True, "Terminzusage", "Push-Standardwerte")
    PUSH_DEFAULT_APPOINTMENT_REJECTED = SettingDefinition[bool]("push.default.appointment_rejected", SettingValueType.BOOL, True, "Terminabsage", "Push-Standardwerte")
    PUSH_DEFAULT_COMMISSION_PRIORITY_RAISED = SettingDefinition[bool]("push.default.commission_priority_raised", SettingValueType.BOOL, True, "Auftrag mit hoher Priorität", "Push-Standardwerte")
    PUSH_DEFAULT_NEW_PROCESS_INFO = SettingDefinition[bool]("push.default.new_process_info", SettingValueType.BOOL, True, "Neue Vorgangsinformation", "Push-Standardwerte")
    PUSH_DEFAULT_NEW_SAFETY_INSPECTION = SettingDefinition[bool]("push.default.new_safety_inspection", SettingValueType.BOOL, True, "Neue Verkehrssicherheitsprüfung", "Push-Standardwerte")
    PUSH_DEFAULT_CHANGED_SAFETY_INSPECTION = SettingDefinition[bool]("push.default.changed_safety_inspection", SettingValueType.BOOL, True, "Geänderte Verkehrssicherheitsprüfung", "Push-Standardwerte")
    PUSH_DEFAULT_URGENT_SAFETY_DEFECT = SettingDefinition[bool]("push.default.urgent_safety_defect", SettingValueType.BOOL, True, "Dringender Sicherheitsmangel", "Push-Standardwerte")

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

    OPENAI_ENABLED = SettingDefinition[bool](
        "openai.enabled",
        SettingValueType.BOOL,
        False,
        "KI-Analyse aktivieren",
        "OpenAI",
    )
    TICKET_AI_PREFETCH_ENABLED = SettingDefinition[bool](
        "tickets.ai_prefetch_enabled",
        SettingValueType.BOOL,
        True,
        "KI-Auftragsvorschläge automatisch vorbereiten",
        "OpenAI",
    )
    OPENAI_API_KEY = SettingDefinition[str](
        "openai.api_key",
        SettingValueType.SECRET,
        "",
        "OpenAI API-Key",
        "OpenAI",
        True,
    )
    OPENAI_MODEL = SettingDefinition[str](
        "openai.model",
        SettingValueType.STRING,
        "",
        "OpenAI-Modell",
        "OpenAI",
    )
    OPENAI_TIMEOUT_SECONDS = SettingDefinition[float](
        "openai.timeout_seconds",
        SettingValueType.FLOAT,
        45.0,
        "Zeitlimit für KI-Analysen in Sekunden",
        "OpenAI",
    )
    PROCESS_INFO_EMAIL_DELAY_MINUTES = SettingDefinition[int](
        "notifications.process_info_email_delay_minutes", SettingValueType.INT, 15,
        "E-Mail bei ungelesenen Zusatzinformationen nach Minuten", "Benachrichtigungen",
    )
    WEB_PUSH_ENABLED = SettingDefinition[bool](
        "notifications.web_push_enabled", SettingValueType.BOOL, True,
        "Desktop-Benachrichtigungen erlauben", "Benachrichtigungen",
    )
    WEB_PUSH_VAPID_PUBLIC_KEY = SettingDefinition[str](
        "notifications.web_push_vapid_public_key", SettingValueType.STRING, "",
        "Öffentlicher Web-Push-Schlüssel", "Benachrichtigungen",
    )
    WEB_PUSH_VAPID_PRIVATE_KEY = SettingDefinition[str](
        "notifications.web_push_vapid_private_key", SettingValueType.SECRET, "",
        "Privater Web-Push-Schlüssel", "Benachrichtigungen", True,
    )
    WEB_PUSH_VAPID_SUBJECT = SettingDefinition[str](
        "notifications.web_push_vapid_subject", SettingValueType.STRING, "",
        "Web-Push-Kontaktadresse mit mailto:", "Benachrichtigungen",
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


def validate_invoice_payment_text(value: str) -> str:
    if not isinstance(value, str) or len(value) > 4000:
        raise ValueError("Der Zahlungstext darf höchstens 4000 Zeichen enthalten.")
    allowed = {"due_date", "invoice_number", "payment_reference", "amount", "currency", "iban", "bic", "mandate_reference", "creditor_id", "account_holder"}
    try:
        for literal, field, specification, conversion in Formatter().parse(value):
            if field is not None and (field not in allowed or specification or conversion):
                raise ValueError("Der Zahlungstext enthält einen unbekannten oder ungültigen Platzhalter.")
    except ValueError as exc:
        raise ValueError("Bitte gültige Platzhalter im Zahlungstext verwenden.") from exc
    return value


COVER_LETTER_FIELDS = {
    "invoice_number", "invoice_date", "contract_number", "recipient_name", "amount", "currency",
    "due_date", "payment_terms", "seller_name", "caseworker_name", "caseworker_email", "caseworker_phone",
}
TENANT_CHARGE_PURPOSE_FIELDS = {"invoice_issuer", "gross_amount", "work_description"}


def _validate_text_placeholders(value: str, allowed_fields: set[str]) -> None:
    for _, field, specification, conversion in Formatter().parse(value):
        if field is not None and (field not in allowed_fields or specification or conversion):
            raise ValueError("Ungültiger Platzhalter.")


def validate_document_default_text(value: str, *, for_offer_invoice=False) -> str:
    if isinstance(value, str):
        value = value.replace("\r\n", "\n").replace("\r", "\n")
    if not isinstance(value, str) or len(value) > 4000 or any(
        ord(character) < 32 and character not in "\n\t" for character in value
    ):
        raise ValueError("Einleitungs- und Schlusstexte dürfen höchstens 4.000 Zeichen und keine ungültigen Steuerzeichen enthalten.")
    if for_offer_invoice:
        try:
            _validate_text_placeholders(value, {"offer_number"})
        except ValueError as exc:
            raise ValueError("Im Einleitungstext für Rechnungen aus Angeboten ist {offer_number} als Platzhalter verfügbar.") from exc
    return value.strip()


def validate_cover_letter_text(value, *, title=False):
    limit = 200 if title else 20000
    label = "Die Überschrift" if title else "Der Anschreibentext"
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(
        ord(character) < 32 and character not in "\n\r\t" for character in value
    ) or (title and any(character in value for character in "\r\n")):
        raise ValueError(f"{label} muss ausgefüllt sein und darf höchstens {limit} Zeichen enthalten.")
    try:
        allowed_fields = COVER_LETTER_FIELDS if title else COVER_LETTER_FIELDS | {"sig"}
        _validate_text_placeholders(value, allowed_fields)
    except ValueError as exc:
        raise ValueError("Das Anschreiben enthält einen unbekannten oder ungültigen Platzhalter.") from exc
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def validate_tenant_charge_purpose_template(value: str) -> str:
    if isinstance(value, str):
        value = value.replace("\r\n", "\n").replace("\r", "\n")
    if not isinstance(value, str) or not value.strip() or len(value) > 4000 or any(
        ord(character) < 32 and character not in "\n\t" for character in value
    ):
        raise ValueError("Die Vorlage für den Anlass der Belastung muss ausgefüllt sein und darf höchstens 4.000 Zeichen enthalten.")
    try:
        _validate_text_placeholders(value, TENANT_CHARGE_PURPOSE_FIELDS)
    except ValueError as exc:
        raise ValueError("Die Vorlage für den Anlass der Belastung enthält einen unbekannten oder ungültigen Platzhalter.") from exc
    return value.strip()


def validate_tenant_charge_closing(value: str) -> str:
    if isinstance(value, str):
        value = value.replace("\r\n", "\n").replace("\r", "\n")
    if not isinstance(value, str) or len(value) > 4000 or any(
        ord(character) < 32 and character not in "\n\t" for character in value
    ):
        raise ValueError("Bitte eine Grußformel mit höchstens 4.000 Zeichen und ohne ungültige Steuerzeichen eingeben.")
    return value.strip()


def validate_tenant_charge_closing_template(value: str) -> str:
    value = validate_tenant_charge_closing(value)
    try:
        _validate_text_placeholders(value, {"seller_name", "sig"})
    except ValueError as exc:
        raise ValueError("Die Grußformel enthält einen unbekannten oder ungültigen Platzhalter. Verfügbar sind {seller_name} und {sig}.") from exc
    return value


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
        if definition.key == SettingKeys.INVOICE_COVER_LETTER_LAYOUT.key:
            value = cast(T, validate_cover_letter_layout(value))
        if definition.key == SettingKeys.INVOICE_LAYOUT.key and value != "":
            value = cast(T, validate_invoice_layout(value))
        if definition.key == SettingKeys.OFFER_LAYOUT.key and value != "":
            value = cast(T, validate_offer_layout(value))
        if definition.key in {
            SettingKeys.INVOICE_INTRODUCTORY_TEXT.key,
            SettingKeys.INVOICE_OFFER_INTRODUCTORY_TEXT.key,
            SettingKeys.INVOICE_CONCLUDING_TEXT.key,
            SettingKeys.OFFER_INTRODUCTORY_TEXT.key,
            SettingKeys.OFFER_CONCLUDING_TEXT.key,
        }:
            value = cast(T, validate_document_default_text(
                value, for_offer_invoice=definition.key == SettingKeys.INVOICE_OFFER_INTRODUCTORY_TEXT.key,
            ))
        if definition.key in {SettingKeys.INVOICE_COVER_LETTER_TITLE.key, SettingKeys.INVOICE_COVER_LETTER_TEXT.key}:
            value = cast(T, validate_cover_letter_text(value, title=definition.key == SettingKeys.INVOICE_COVER_LETTER_TITLE.key))
        if definition.key == SettingKeys.INVOICE_TENANT_CHARGE_CUTOFF_DAY.key and not 1 <= cast(int, value) <= 31:
            raise ValueError("Bitte geben Sie für den Stichtag im Mieteranschreiben einen Tag von 1 bis 31 ein.")
        if definition.key == SettingKeys.INVOICE_TENANT_CHARGE_PURPOSE_TEMPLATE.key:
            value = cast(T, validate_tenant_charge_purpose_template(value))
        if definition.key == SettingKeys.INVOICE_TENANT_CHARGE_CLOSING_TEMPLATE.key:
            value = cast(T, validate_tenant_charge_closing_template(value))
        if definition.key in {SettingKeys.INVOICE_TRANSFER_PAYMENT_TEXT.key, SettingKeys.INVOICE_SEPA_PAYMENT_TEXT.key}:
            value = cast(T, validate_invoice_payment_text(value))
        if definition.key == SettingKeys.SMS_API_KEY.key:
            key = str(value or "").strip()
            if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in key):
                raise ValueError("Der seven API-Key darf keine Leer- oder Steuerzeichen enthalten.")
            value = cast(T, key)
        if definition.key == SettingKeys.SMS_SENDER.key:
            sender = str(value or "").strip()
            if sender and not ((sender.isdigit() and len(sender) <= 16) or (sender.replace(" ", "").isalnum() and len(sender) <= 11)):
                raise ValueError("Die SMS-Absenderkennung darf maximal 11 Zeichen (Buchstaben, Ziffern und Leerzeichen) oder 16 Ziffern enthalten.")
            value = cast(T, sender)
        if definition.key == SettingKeys.HEALTH_RULES.key:
            value = cast(T, normalize_health_rules(value))
        if definition.key in {SettingKeys.SMTP_DEBUG_RECIPIENT.key, SettingKeys.SMTP_EXTERNAL_DEBUG_RECIPIENT.key}:
            value = cast(T, validate_debug_email_address(cast(str, value)))
        if definition.key == SettingKeys.TENANT_TICKET_REPLY_TO.key:
            try:
                value = cast(T, validate_debug_email_address(cast(str, value)))
            except ValueError as exc:
                raise ValueError("Bitte eine gültige mail2ticket-Antwortadresse ohne Anzeigenamen eingeben.") from exc
        if definition.key == SettingKeys.TENANT_TICKET_WRITEBACK_EXCLUDED_SOURCE_IDS.key:
            value = cast(T, normalize_ticket_source_ids(value))
        if definition.key == SettingKeys.EXTERNAL_PORTAL_NAME.key:
            value = cast(T, normalize_external_portal_name(value))
        row = AppSetting.query.filter_by(key=definition.key).one_or_none()
        previous_value = row.value_text if row is not None else ""
        if row is None:
            row = AppSetting(key=definition.key, value_type=definition.value_type.value, label=definition.label,
                             group=definition.group, is_secret=definition.secret)
            db.session.add(row)
        row.value_text = self._serialize(value, definition.value_type)
        row.value_type = definition.value_type.value
        row.label = definition.label
        row.group = definition.group
        row.is_secret = definition.secret
        if definition.key == SettingKeys.SMTP_DEBUG_RECIPIENT.key and (value or previous_value):
            commission_id = db.session.query(Commission.id).order_by(Commission.id.desc()).limit(1).with_for_update().scalar() or 0
            self.set(SMS_DEBUG_SUPPRESSED_COMMISSION_ID, max(
                self.get(SMS_DEBUG_SUPPRESSED_COMMISSION_ID), commission_id,
            ))

    def set_from_form(
        self,
        definition: SettingDefinition[Any],
        raw_value: str | None,
    ) -> None:
        if raw_value is None and definition.key in {
            SettingKeys.SMTP_DEBUG_RECIPIENT.key,
            SettingKeys.SMTP_EXTERNAL_DEBUG_RECIPIENT.key,
            SettingKeys.INVOICE_TENANT_CHARGE_CUTOFF_DAY.key,
            SettingKeys.INVOICE_TENANT_CHARGE_PURPOSE_TEMPLATE.key,
            SettingKeys.INVOICE_TENANT_CHARGE_CLOSING_TEMPLATE.key,
        }:
            return

        if definition.secret and not raw_value:
            return

        if definition.key == SettingKeys.PROCESS_INFO_EMAIL_DELAY_MINUTES.key:
            try:
                value = int(raw_value or "")
            except ValueError as exc:
                raise ValueError(
                    "Die E-Mail-Verzögerung muss eine ganze Zahl sein."
                ) from exc
            if not 1 <= value <= 1440:
                raise ValueError(
                    "Die E-Mail-Verzögerung muss zwischen 1 und 1.440 Minuten liegen."
                )
            self.set(definition, value)
            return

        try:
            value = self._deserialize(
                raw_value or "",
                definition.value_type,
            )
        except (TypeError, ValueError) as exc:
            if (
                definition.key
                == SettingKeys.APPOINTMENT_CUSTOM_TIME_DURATION_MINUTES.key
            ):
                raise ValueError(
                    "Die Kalenderdauer muss eine positive ganze Zahl sein."
                ) from exc

            raise

        if (
            definition.key
            == SettingKeys.APPOINTMENT_CUSTOM_TIME_DURATION_MINUTES.key
            and value < 1
        ):
            raise ValueError(
                "Die Kalenderdauer muss mindestens eine Minute betragen."
            )

        self.set(
            definition,
            value,
        )

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
