from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from flask import current_app
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.utils import secure_filename

from .models import Commission


MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
THUMBNAIL_DIMENSION = 480


@dataclass(frozen=True)
class StoredCommissionAttachment:
    original_filename: str
    storage_name: str
    thumbnail_storage_name: str | None
    mime_type: str
    size_bytes: int
    content_sha256: str


def commission_attachment_root() -> Path:
    configured_path = current_app.config.get(
        "COMMISSION_ATTACHMENT_DIR"
    )

    if configured_path:
        root = Path(configured_path)

        if not root.is_absolute():
            root = (
                Path(current_app.instance_path)
                / root
            )
    else:
        root = (
            Path(current_app.instance_path)
            / "commission_attachments"
        )

    root.mkdir(
        parents=True,
        exist_ok=True,
    )

    return root


def commission_attachment_path(
    storage_name: str,
) -> Path:
    root = commission_attachment_root().resolve()
    path = (root / storage_name).resolve()

    if root != path and root not in path.parents:
        raise ValueError(
            "Ungültiger Dateipfad."
        )

    return path


def store_commission_attachment_bytes(
    commission: Commission,
    raw_data: bytes,
    original_filename: str,
) -> StoredCommissionAttachment:
    if not raw_data:
        raise ValueError(
            "Die Datei ist leer."
        )

    if len(raw_data) > MAX_ATTACHMENT_BYTES:
        raise ValueError(
            "Die Datei darf höchstens 25 MB groß sein."
        )

    safe_filename = (
        secure_filename(original_filename)
        or "datei"
    )

    extension = Path(
        safe_filename
    ).suffix.lower()

    mime_type = (
        mimetypes.guess_type(
            safe_filename
        )[0]
        or "application/octet-stream"
    )

    commission_directory = Path(
        str(commission.public_id)
    )
    file_token = uuid4().hex

    storage_name = str(
        commission_directory
        / f"{file_token}{extension}"
    )

    storage_path = commission_attachment_path(
        storage_name
    )
    storage_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary_path = storage_path.with_name(
        f"{storage_path.name}.tmp"
    )
    temporary_path.write_bytes(
        raw_data
    )
    temporary_path.replace(
        storage_path
    )

    thumbnail_storage_name = None

    try:
        with Image.open(
            BytesIO(raw_data)
        ) as source_image:
            source_format = source_image.format
            source_image.load()

            thumbnail_image = (
                ImageOps.exif_transpose(
                    source_image
                )
                .convert("RGB")
            )

            thumbnail_image.thumbnail(
                (
                    THUMBNAIL_DIMENSION,
                    THUMBNAIL_DIMENSION,
                ),
                Image.Resampling.LANCZOS,
            )

            thumbnail_buffer = BytesIO()

            thumbnail_image.save(
                thumbnail_buffer,
                format="JPEG",
                quality=80,
                optimize=True,
            )

            thumbnail_storage_name = str(
                commission_directory
                / f"{file_token}-thumb.jpg"
            )

            thumbnail_path = (
                commission_attachment_path(
                    thumbnail_storage_name
                )
            )

            thumbnail_temporary_path = (
                thumbnail_path.with_name(
                    f"{thumbnail_path.name}.tmp"
                )
            )

            thumbnail_temporary_path.write_bytes(
                thumbnail_buffer.getvalue()
            )
            thumbnail_temporary_path.replace(
                thumbnail_path
            )

            detected_mime_type = (
                Image.MIME.get(source_format)
                if source_format
                else None
            )

            if detected_mime_type:
                mime_type = detected_mime_type

    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError,
    ):
        thumbnail_storage_name = None

    return StoredCommissionAttachment(
        original_filename=safe_filename,
        storage_name=storage_name,
        thumbnail_storage_name=(
            thumbnail_storage_name
        ),
        mime_type=mime_type,
        size_bytes=len(raw_data),
        content_sha256=sha256(
            raw_data
        ).hexdigest(),
    )