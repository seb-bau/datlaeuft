from io import BytesIO

from werkzeug.datastructures import FileStorage

from .audit import audit
from .extensions import db
from .models import Commission, ExternalCraftsman, CraftActivity, WowiLicenseAgreement, WowiUseUnit, WowiBuilding, WowiEconomicUnit, utc_now
from .utils import format_local_datetime


def can_handle_commission_review(commission, user):
    return bool(user.can_review_work and (
        user.is_admin or commission.created_by_user_id in (None, user.id) or commission.local_official_user_id == user.id
    ))


def review_commission(commission_id, user, *, lock=False, require_open=True):
    query = Commission.query.filter_by(id=commission_id).populate_existing()
    if lock:
        query = query.with_for_update()
    commission = query.one_or_none()
    if commission is None or not can_handle_commission_review(commission, user):
        raise ValueError("Der Auftrag ist für Ihre Auftragsprüfung nicht verfügbar.")
    if require_open and (not commission.requires_office_review or commission.deleted_at is not None):
        raise ValueError("Dieser Prüfauftrag wurde bereits bearbeitet oder ist nicht mehr verfügbar.")
    return commission


def resolve_commission_review(commission, user, *, replacement=None):
    if not commission.requires_office_review:
        raise ValueError("Dieser Prüfauftrag wurde bereits bearbeitet.")
    commission.review_resolved_at = utc_now()
    commission.review_resolved_by = user
    commission.review_resolution = "reissued" if replacement is not None else "acknowledged"
    if replacement is not None:
        replacement.reissued_from = commission
    audit("commission.review." + commission.review_resolution, "commission", commission.id, {
        "replacement_commission_id": replacement.id if replacement is not None else None,
    }, actor_user_id=user.id)


def commission_reissue_data(commission):
    object_type = commission.object_type
    model, erp_id = {
        "license_agreement": (WowiLicenseAgreement, commission.license_agreement_id),
        "use_unit": (WowiUseUnit, commission.use_unit_id),
        "building": (WowiBuilding, commission.building_erp_id),
        "economic_unit": (WowiEconomicUnit, commission.economic_unit_erp_id),
    }.get(object_type, (None, None))
    directory = model.query.filter_by(erp_id=erp_id).one_or_none() if model is not None and erp_id is not None else None
    craft_ids = {position.craft_activity_erp_id for position in commission.positions if position.craft_activity_erp_id is not None}
    craft = CraftActivity.query.filter(CraftActivity.erp_id.in_(craft_ids)).first() if len(craft_ids) == 1 else None
    external = ExternalCraftsman.query.filter_by(craftsman_erp_id=commission.craftsman_erp_id).one_or_none() if commission.assignment_type == "external" else None
    values = {
        "object_type": object_type or "", "object_id": directory.id if directory else "",
        "title": commission.title or "", "damage_location": commission.damage_location or "", "contact_person": commission.contact_person or "",
        "craft_activity_id": craft.id if craft else "", "commission_type_id": commission.commission_type_erp_id or "",
        "company_code_erp_id": commission.company_code_erp_id or "", "crafts_process_id": commission.crafts_process_erp_id or "",
        "creditor_id": commission.creditor_erp_id or "", "assignment_type": commission.assignment_type,
        "craftsman_user_id": commission.user_id or "", "external_craftsman_id": external.id if external else "",
        "priority": "high" if commission.is_high_priority else "normal",
        "execution_from": format_local_datetime(commission.execution_from, "%Y-%m-%dT%H:%M") if commission.execution_from else "",
        "execution_to": format_local_datetime(commission.execution_to, "%Y-%m-%dT%H:%M") if commission.execution_to else "",
        "notify_tenants": "true" if commission.notify_tenants else "false",
        "notify_on_completion": "true" if commission.notify_on_completion else "false",
        "is_retrospective": "false",
    }
    return {"form_values": values, "position_rows": [{
        "article_number": position.article_number, "service_catalog_id": position.service_catalog_erp_id,
        "commission_text": position.commission_text or position.description or "",
        "internal_description": position.internal_description or "", "units": str(position.units),
    } for position in commission.positions]}


def copy_commission_files(source, target, store_photo, store_attachment):
    from .photo_service import commission_photo_path
    from .attachment_service import commission_attachment_path
    try:
        for photo in source.photos:
            if not photo.active or photo.deleted_at is not None:
                continue
            upload = FileStorage(stream=BytesIO(commission_photo_path(photo.storage_name).read_bytes()), filename=photo.original_filename)
            copied, duplicate = store_photo(target, upload, photo.caption)
            copied.picture_type_erp_id, copied.picture_type_name = photo.picture_type_erp_id, photo.picture_type_name
        for attachment in source.attachments:
            if not attachment.active:
                continue
            upload = FileStorage(stream=BytesIO(commission_attachment_path(attachment.storage_name).read_bytes()), filename=attachment.original_filename)
            copied, duplicate = store_attachment(target, upload, attachment.caption)
            for name in ("file_type_erp_id", "file_type_name", "privacy_category_erp_id", "privacy_category_name"):
                setattr(copied, name, getattr(attachment, name))
    except OSError as exc:
        raise ValueError("Die Fotos oder Dateien des ursprünglichen Auftrags sind nicht vollständig verfügbar. Bitte die Anhänge prüfen.") from exc
