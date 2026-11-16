from dataclasses import dataclass
from io import BytesIO
import mimetypes
import re

from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.datastructures import FileStorage

from .attachment_service import MAX_ATTACHMENT_BYTES, commission_attachment_path, store_commission_attachment_bytes
from .extensions import db
from .models import CommissionAttachment, CommissionSyncState, utc_now
from .photo_service import MAX_PHOTO_BYTES, MAX_PHOTO_DIMENSION
from .utils import parse_datetime


def document_value(row, *names):
    if not isinstance(row, dict):
        return None
    values = {str(key).replace("_", "").casefold(): value for key, value in row.items()}
    return next((values[name.replace("_", "").casefold()] for name in names if values.get(name.replace("_", "").casefold()) is not None), None)


def is_photo_document(row):
    filename = str(document_value(row, "FileName") or "")
    mime = str(document_value(row, "MimeType", "ContentType") or mimetypes.guess_type(filename)[0] or "")
    return mime.casefold().startswith("image/")


def is_photo_content(raw_data):
    try:
        with Image.open(BytesIO(raw_data)):
            return True
    except Image.DecompressionBombError:
        return True
    except (UnidentifiedImageError, OSError, ValueError):
        return False


def ticket_photos_managed(commission):
    payload = commission.raw_payload if isinstance(commission.raw_payload, dict) else {}
    selection = payload.get("ticket_photo_selection")
    return isinstance(selection, dict) and selection.get("version") == 1


def ticket_attachments_managed(commission):
    payload = commission.raw_payload if isinstance(commission.raw_payload, dict) else {}
    selection = payload.get("ticket_attachment_selection")
    return isinstance(selection, dict) and selection.get("version") == 1


def document_reference_id(reference):
    try:
        return int(document_value(reference, "Id"))
    except (TypeError, ValueError):
        return None


def ticket_attachment_choices(ticket, service):
    rows = service.get_document_rows(entity_name="Ticket", entity_id=ticket.erp_id)
    choices = {}
    for row in rows:
        if document_value(row, "FileIsObsolete"):
            continue
        file_guid = str(document_value(row, "FileGuid") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_{}-]{1,160}", file_guid):
            raise ValueError("Ein Ticketanhang besitzt keine gültige Dateikennung.")
        filename = str(document_value(row, "FileName") or file_guid)[:255]
        mime_type = str(document_value(row, "MimeType", "ContentType") or mimetypes.guess_type(filename)[0] or "application/octet-stream")
        is_image = is_photo_document(row)
        is_pdf = mime_type.casefold() == "application/pdf" or filename.casefold().endswith(".pdf")
        extension = filename.rsplit(".", 1)[-1].upper() if "." in filename else ""
        type_label = f"{extension}-Datei" if extension.isalnum() and len(extension) <= 10 else "Datei"
        choices[file_guid] = {
            "file_guid": file_guid,
            "filename": filename,
            "is_image": is_image,
            "is_pdf": is_pdf,
            "type_label": "Bild" if is_image else "PDF-Dokument" if is_pdf else type_label,
            "metadata": row,
        }
    return list(choices.values())


@dataclass
class TicketAttachmentSelection:
    ticket_erp_id: int
    choices: list[dict]
    selected_guids: list[str]


def prepare_ticket_attachment_selection(ticket, form, service):
    if form.get("ticket_attachment_selection_ready") != "true":
        raise ValueError("Bitte warten, bis die Ticketanhänge geladen wurden, und die Auswahl prüfen.")
    choices = ticket_attachment_choices(ticket, service)
    selected_guids = list(dict.fromkeys(form.getlist("ticket_attachment_guid")))
    if len(selected_guids) > 100:
        raise ValueError("Es können höchstens 100 Ticketanhänge übernommen werden.")
    available_guids = {item["file_guid"] for item in choices}
    if any(file_guid not in available_guids for file_guid in selected_guids):
        raise ValueError("Ein ausgewählter Ticketanhang ist nicht mehr verfügbar. Bitte die Auswahl erneut prüfen.")
    return TicketAttachmentSelection(ticket.erp_id, choices, selected_guids)


def read_ticket_attachment(attachment, service):
    raw_data = service.download_document_content(entity_name="Ticket", file_guid=attachment["file_guid"])
    if not isinstance(raw_data, bytes) or not raw_data:
        raise ValueError("Der Ticketanhang konnte nicht gelesen werden.")
    if len(raw_data) > MAX_ATTACHMENT_BYTES:
        raise ValueError("Der Ticketanhang darf höchstens 25 MB groß sein.")
    return raw_data


def render_ticket_photo(raw_data, thumbnail=False):
    try:
        with Image.open(BytesIO(raw_data)) as source:
            source.load()
            image = ImageOps.exif_transpose(source).convert("RGB")
            dimension = 480 if thumbnail else MAX_PHOTO_DIMENSION
            image.thumbnail((dimension, dimension), Image.Resampling.LANCZOS)
            buffer = BytesIO()
            image.save(buffer, format="JPEG", quality=80)
            return buffer.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ValueError("Das Ticketfoto ist kein gültiges Bild.") from exc


def store_selected_ticket_attachments(commission, selection, service, store_photo, stored_paths, uploaded_by_user_id):
    for item in selection.choices:
        if item["file_guid"] not in selection.selected_guids:
            continue
        raw_data = read_ticket_attachment(item, service)
        store_as_photo = False
        if len(raw_data) <= MAX_PHOTO_BYTES:
            try:
                render_ticket_photo(raw_data, thumbnail=True)
                store_as_photo = True
            except ValueError:
                pass
        if store_as_photo:
            upload = FileStorage(stream=BytesIO(raw_data), filename=item["filename"])
            try:
                store_photo(commission=commission, upload=upload, caption=item["filename"])
            finally:
                upload.close()
            continue
        stored = store_commission_attachment_bytes(commission, raw_data, item["filename"])
        stored_paths.append(commission_attachment_path(stored.storage_name))
        if stored.thumbnail_storage_name:
            stored_paths.append(commission_attachment_path(stored.thumbnail_storage_name))
        metadata = item["metadata"]
        file_type = document_value(metadata, "FileType")
        privacy = document_value(metadata, "DataPrivacyCategory")
        created_at = document_value(metadata, "CreationDate")
        try:
            created_at = parse_datetime(created_at) if created_at else None
        except (TypeError, ValueError):
            created_at = None
        mime_type = stored.mime_type
        if mime_type == "image/svg+xml" or (mime_type == "application/pdf" and not raw_data.lstrip().startswith(b"%PDF-")):
            mime_type = "application/octet-stream"
        db.session.add(CommissionAttachment(
            commission=commission, uploaded_by_user_id=uploaded_by_user_id,
            source="ticket_selection", source_entity_name="Ticket", source_entity_id=selection.ticket_erp_id,
            external_id=str(document_value(metadata, "Id") or "")[:160] or None,
            external_file_guid=item["file_guid"], original_filename=stored.original_filename,
            storage_name=stored.storage_name, thumbnail_storage_name=stored.thumbnail_storage_name,
            mime_type=mime_type, size_bytes=stored.size_bytes, content_sha256=stored.content_sha256,
            file_type_erp_id=document_reference_id(file_type), file_type_name=document_value(file_type, "Name"),
            privacy_category_erp_id=document_reference_id(privacy), privacy_category_name=document_value(privacy, "Name"),
            external_created_at=created_at, external_updated_at=created_at,
            active=True, erp_sync_state=CommissionSyncState.SYNCED.value, erp_synced_at=utc_now(),
        ))
    commission.raw_payload = {
        **(commission.raw_payload or {}),
        "ticket_attachment_selection": {
            "version": 1,
            "ticket_erp_id": selection.ticket_erp_id,
            "file_guids": [item["file_guid"] for item in selection.choices],
            "selected_file_guids": selection.selected_guids,
        },
    }
