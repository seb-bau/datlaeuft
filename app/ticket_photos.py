from dataclasses import dataclass
from io import BytesIO
import mimetypes
import re

from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.datastructures import FileStorage

from .photo_service import MAX_PHOTO_BYTES, MAX_PHOTO_DIMENSION


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


def ticket_photo_choices(ticket, service):
    rows = service.get_document_rows(entity_name="Ticket", entity_id=ticket.erp_id)
    choices = {}
    for row in rows:
        if document_value(row, "FileIsObsolete") or not is_photo_document(row):
            continue
        file_guid = str(document_value(row, "FileGuid") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_{}-]{1,64}", file_guid):
            raise ValueError("Ein Ticketfoto besitzt keine gültige Dateikennung.")
        choices[file_guid] = {
            "file_guid": file_guid,
            "filename": str(document_value(row, "FileName") or "Ticketfoto")[:255],
        }
    return list(choices.values())


@dataclass
class TicketPhotoSelection:
    ticket_erp_id: int
    choices: list[dict]
    selected_guids: list[str]


def prepare_ticket_photo_selection(ticket, form, service):
    if form.get("ticket_photo_selection_ready") != "true":
        raise ValueError("Bitte warten, bis die Ticketfotos geladen wurden, und die Fotoauswahl prüfen.")
    choices = ticket_photo_choices(ticket, service)
    selected_guids = list(dict.fromkeys(form.getlist("ticket_photo_guid")))
    if len(selected_guids) > 100:
        raise ValueError("Es können höchstens 100 Ticketfotos übernommen werden.")
    available_guids = {item["file_guid"] for item in choices}
    if any(file_guid not in available_guids for file_guid in selected_guids):
        raise ValueError("Ein ausgewähltes Ticketfoto ist nicht mehr verfügbar. Bitte die Fotoauswahl erneut prüfen.")
    return TicketPhotoSelection(ticket.erp_id, choices, selected_guids)


def read_ticket_photo(photo, service):
    raw_data = service.download_document_content(entity_name="Ticket", file_guid=photo["file_guid"])
    if not isinstance(raw_data, bytes) or not raw_data:
        raise ValueError("Das Ticketfoto konnte nicht gelesen werden.")
    if len(raw_data) > MAX_PHOTO_BYTES:
        raise ValueError("Das Ticketfoto darf höchstens 15 MB groß sein.")
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


def store_selected_ticket_photos(commission, selection, service, store_photo):
    for photo in selection.choices:
        if photo["file_guid"] not in selection.selected_guids:
            continue
        raw_data = read_ticket_photo(photo, service)
        upload = FileStorage(stream=BytesIO(raw_data), filename=photo["filename"])
        try:
            store_photo(commission=commission, upload=upload, caption=photo["filename"])
        finally:
            upload.close()
    commission.raw_payload = {
        **(commission.raw_payload or {}),
        "ticket_photo_selection": {
            "version": 1,
            "ticket_erp_id": selection.ticket_erp_id,
            "photo_file_guids": [item["file_guid"] for item in selection.choices],
            "selected_file_guids": selection.selected_guids,
        },
    }
