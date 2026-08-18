from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

from dvelopdmspy.dvelopdmspy import DvelopDmsPy
from flask import current_app

from .audit import audit
from .models import (
    Offer,
    OfferArchiveStatus,
    OfferStatus,
    User,
    utc_now,
)
from .offer_document import (
    OfferDocumentError,
    resolve_offer_document_path,
)
from .settings import SettingKeys, settings


class OfferArchiveError(RuntimeError):
    pass


@dataclass(frozen=True)
class OfferArchiveResult:
    document_id: str
    already_archived: bool


def required_archive_setting(
    definition,
) -> str:
    value = str(
        settings.get(definition)
        or ""
    ).strip()

    if not value:
        raise OfferArchiveError(
            "Die Einstellung "
            f"„{definition.label}“ fehlt."
        )

    return value


def decimal_text(
    value,
) -> str:
    decimal_value = Decimal(
        str(value or 0)
    ).quantize(
        Decimal("0.01")
    )

    return format(
        decimal_value,
        ".2f",
    )


def offer_archive_values(
    offer: Offer,
    archive_key: str,
) -> dict[str, str]:
    company_code = offer.company_code
    commission_number = ""

    if offer.commission is not None:
        commission_number = str(
            offer.commission.erp_idnum
            or offer.commission.id
            or ""
        )

    return {
        "archive_key": archive_key,
        "offer_number": str(
            offer.offer_number or ""
        ),
        "offer_date": (
            offer.offer_date.isoformat()
            if offer.offer_date
            else ""
        ),
        "valid_until": (
            offer.valid_until.isoformat()
            if offer.valid_until
            else ""
        ),
        "company_code": str(
            company_code.code or ""
        ),
        "company_code_name": str(
            company_code.name or ""
        ),
        "contract_number": str(
            offer.license_agreement_idnum
            or ""
        ),
        "recipient_name": str(
            offer.recipient_name or ""
        ),
        "craft": str(
            (
                offer.craft_activity.name
                if offer.craft_activity
                else ""
            )
            or ""
        ),
        "economic_unit_id": str(
            offer.economic_unit_erp_id
            or ""
        ),
        "commission_number": (
            commission_number
        ),
        "status": str(
            offer.status or ""
        ),
        "total_net": decimal_text(
            offer.total_net
        ),
        "total_vat": decimal_text(
            offer.total_vat
        ),
        "total_gross": decimal_text(
            offer.total_gross
        ),
    }


def render_archive_property_value(
    template: str,
    values: dict[str, str],
) -> str:
    try:
        return str(
            template
        ).format_map(
            values
        )
    except KeyError as exc:
        raise OfferArchiveError(
            "Unbekannter Platzhalter in der "
            "d.velop-Eigenschaftszuordnung: "
            f"{exc.args[0]}"
        ) from exc


def verify_offer_pdf(
    offer: Offer,
) -> tuple[Path, str]:
    if not offer.document_filename:
        raise OfferArchiveError(
            "Für das Angebot ist kein "
            "PDF gespeichert."
        )

    try:
        document_path = (
            resolve_offer_document_path(
                offer.document_filename
            )
        )
    except OfferDocumentError as exc:
        raise OfferArchiveError(
            str(exc)
        ) from exc

    if not document_path.is_file():
        raise OfferArchiveError(
            "Die gespeicherte Angebotsdatei "
            "wurde nicht gefunden."
        )

    content_hash = sha256(
        document_path.read_bytes()
    ).hexdigest()

    if (
        offer.document_sha256
        and content_hash
        != offer.document_sha256
    ):
        raise OfferArchiveError(
            "Die Prüfsumme der Angebotsdatei "
            "stimmt nicht mit dem "
            "freigegebenen Angebot überein."
        )

    return document_path, content_hash


def create_archive_client() -> DvelopDmsPy:
    hostname = required_archive_setting(
        SettingKeys.DMS_HOSTNAME
    )
    api_key = required_archive_setting(
        SettingKeys.DMS_API_KEY
    )
    repository = required_archive_setting(
        SettingKeys.DMS_REPOSITORY
    )

    try:
        return DvelopDmsPy(
            hostname=hostname,
            api_key=api_key,
            repository=repository,
            logger=current_app.logger,
            user_agent="datlaeuft/1.0",
        )
    except Exception as exc:
        raise OfferArchiveError(
            "Die Verbindung zu d.velop "
            "konnte nicht hergestellt werden."
        ) from exc


def existing_offer_document_id(
    client: DvelopDmsPy,
    category_id: str,
    property_id: str,
    archive_key: str,
) -> str | None:
    search_properties = (
        client.add_property(
            "",
            archive_key,
            prop_guid=property_id,
        )
    )

    try:
        documents = client.get_documents(
            properties=search_properties,
            categories=[category_id],
            limit=2,
        )
    except Exception as exc:
        raise OfferArchiveError(
            "Das Angebot konnte in d.velop "
            "nicht gesucht werden."
        ) from exc

    if len(documents) > 1:
        raise OfferArchiveError(
            "In d.velop wurden mehrere "
            "Dokumente mit demselben "
            "Angebotsschlüssel gefunden."
        )

    if not documents:
        return None

    document_id = str(
        documents[0].id_ or ""
    ).strip()

    if not document_id:
        raise OfferArchiveError(
            "d.velop hat für das vorhandene "
            "Dokument keine ID geliefert."
        )

    return document_id


def offer_upload_properties(
    client: DvelopDmsPy,
    offer: Offer,
    archive_key: str,
    id_property_id: str,
) -> list[dict]:
    raw_mapping = settings.get(
        SettingKeys.DMS_OFFER_PROPERTY_MAP
    )

    if raw_mapping is None:
        raw_mapping = {}

    if not isinstance(raw_mapping, dict):
        raise OfferArchiveError(
            "Die d.velop-Eigenschaftszuordnung "
            "für Angebote muss ein "
            "JSON-Objekt sein."
        )

    values = offer_archive_values(
        offer,
        archive_key,
    )
    property_values = {
        id_property_id: archive_key,
    }

    for property_id, template in (
        raw_mapping.items()
    ):
        normalized_property_id = str(
            property_id or ""
        ).strip()

        if not normalized_property_id:
            raise OfferArchiveError(
                "Die d.velop-Eigenschaftszuordnung "
                "enthält eine leere "
                "Eigenschafts-ID."
            )

        rendered_value = (
            render_archive_property_value(
                str(template),
                values,
            )
        )

        if (
            normalized_property_id
            == id_property_id
            and rendered_value
            != archive_key
        ):
            raise OfferArchiveError(
                "Die Eigenschaft für den "
                "eindeutigen Angebotsschlüssel "
                "darf nicht überschrieben werden."
            )

        property_values[
            normalized_property_id
        ] = rendered_value

    properties = []

    for property_id, value in (
        property_values.items()
    ):
        properties = (
            client.add_upload_property(
                "",
                value,
                prop_guid=property_id,
                plist=properties,
            )
        )

    return properties


def archive_offer(
    offer: Offer,
    user: User,
) -> OfferArchiveResult:
    if (
        offer.status
        == OfferStatus.DRAFT.value
    ):
        raise OfferArchiveError(
            "Angebotsentwürfe können nicht "
            "archiviert werden."
        )

    if not offer.offer_number:
        raise OfferArchiveError(
            "Dem Angebot wurde noch keine "
            "Angebotsnummer zugewiesen."
        )

    document_path, content_hash = (
        verify_offer_pdf(
            offer
        )
    )

    if (
        offer.dms_archive_status
        == OfferArchiveStatus.ARCHIVED.value
        and offer.dms_document_id
    ):
        if (
            offer.dms_archive_pdf_sha256
            and (
                offer.dms_archive_pdf_sha256
                != content_hash
            )
        ):
            raise OfferArchiveError(
                "Die archivierte Prüfsumme "
                "stimmt nicht mit dem aktuellen "
                "Angebots-PDF überein."
            )

        return OfferArchiveResult(
            document_id=(
                offer.dms_document_id
            ),
            already_archived=True,
        )

    category_id = required_archive_setting(
        SettingKeys.DMS_OFFER_CATEGORY_ID
    )
    id_property_id = (
        required_archive_setting(
            SettingKeys
            .DMS_OFFER_ID_PROPERTY_ID
        )
    )
    archive_key = (
        offer.dms_archive_key
        or f"datlaeuft-offer-{offer.id}"
    )
    offer.dms_archive_key = archive_key
    offer.dms_archive_attempted_at = (
        utc_now()
    )

    client = create_archive_client()
    existing_document_id = (
        existing_offer_document_id(
            client,
            category_id,
            id_property_id,
            archive_key,
        )
    )
    already_archived = (
        existing_document_id is not None
    )

    if existing_document_id is None:
        properties = offer_upload_properties(
            client,
            offer,
            archive_key,
            id_property_id,
        )

        try:
            upload_result = (
                client.archive_file(
                    str(document_path),
                    category_id=category_id,
                    properties=properties,
                )
            )
        except Exception as exc:
            raise OfferArchiveError(
                "Das Angebot konnte nicht "
                "nach d.velop übertragen werden."
            ) from exc

        document_id = str(
            upload_result or ""
        ).strip()

        if (
            not document_id
            or document_id == "unknown"
        ):
            raise OfferArchiveError(
                "d.velop hat nach der "
                "Archivierung keine "
                "Dokument-ID geliefert."
            )
    else:
        document_id = (
            existing_document_id
        )

    archived_at = utc_now()
    offer.dms_archive_status = (
        OfferArchiveStatus.ARCHIVED.value
    )
    offer.dms_document_id = document_id
    offer.dms_archive_pdf_sha256 = (
        content_hash
    )
    offer.dms_archived_at = archived_at
    offer.dms_archived_by_user_id = (
        user.id
    )
    offer.dms_archive_error = None

    audit(
        "offer.archive",
        "offer",
        offer.id,
        {
            "offer_number": (
                offer.offer_number
            ),
            "dms_document_id": (
                document_id
            ),
            "archive_key": archive_key,
            "pdf_sha256": content_hash,
            "existing_document": (
                already_archived
            ),
        },
    )

    return OfferArchiveResult(
        document_id=document_id,
        already_archived=already_archived,
    )