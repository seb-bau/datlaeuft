from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from flask import current_app
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from .models import Commission


MAX_PHOTO_BYTES = 15 * 1024 * 1024
MAX_PHOTO_DIMENSION = 2560
THUMBNAIL_DIMENSION = 480


@dataclass(frozen=True)
class StoredCommissionPhoto:
    original_filename: str
    storage_name: str
    thumbnail_storage_name: str
    mime_type: str
    size_bytes: int
    content_sha256: str


def commission_photo_root() -> Path:
    configured_path = current_app.config.get(
        "COMMISSION_PHOTO_DIR"
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
            / "commission_photos"
        )

    root.mkdir(
        parents=True,
        exist_ok=True,
    )

    return root


def commission_photo_path(
    storage_name: str,
) -> Path:
    root = commission_photo_root().resolve()
    path = (root / storage_name).resolve()

    if root != path and root not in path.parents:
        raise ValueError(
            "Ungültiger Fotopfad."
        )

    return path


def store_commission_photo(
    commission: Commission,
    upload: FileStorage,
) -> StoredCommissionPhoto:
    if upload is None or not upload.filename:
        raise ValueError(
            "Es wurde kein Foto übermittelt."
        )

    raw_data = upload.stream.read(
        MAX_PHOTO_BYTES + 1
    )

    if not raw_data:
        raise ValueError(
            "Das Foto ist leer."
        )

    if len(raw_data) > MAX_PHOTO_BYTES:
        raise ValueError(
            "Das Foto darf höchstens 15 MB groß sein."
        )

    try:
        with Image.open(
            BytesIO(raw_data)
        ) as source_image:
            source_image.load()

            normalized_image = (
                ImageOps.exif_transpose(
                    source_image
                )
                .convert("RGB")
            )

            normalized_image.thumbnail(
                (
                    MAX_PHOTO_DIMENSION,
                    MAX_PHOTO_DIMENSION,
                ),
                Image.Resampling.LANCZOS,
            )

            original_buffer = BytesIO()

            normalized_image.save(
                original_buffer,
                format="JPEG",
                quality=86,
                optimize=True,
            )

            thumbnail_image = (
                normalized_image.copy()
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

    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError,
    ) as exc:
        raise ValueError(
            "Die hochgeladene Datei ist kein gültiges Bild."
        ) from exc

    original_data = original_buffer.getvalue()
    thumbnail_data = thumbnail_buffer.getvalue()
    content_hash = sha256(
        original_data
    ).hexdigest()

    commission_directory = (
        Path(str(commission.public_id))
    )
    file_token = uuid4().hex

    storage_name = str(
        commission_directory
        / f"{file_token}.jpg"
    )

    thumbnail_storage_name = str(
        commission_directory
        / f"{file_token}-thumb.jpg"
    )

    original_path = commission_photo_path(
        storage_name
    )
    thumbnail_path = commission_photo_path(
        thumbnail_storage_name
    )

    original_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    original_temp_path = original_path.with_suffix(
        ".tmp"
    )
    thumbnail_temp_path = thumbnail_path.with_suffix(
        ".tmp"
    )

    original_temp_path.write_bytes(
        original_data
    )
    thumbnail_temp_path.write_bytes(
        thumbnail_data
    )

    original_temp_path.replace(
        original_path
    )
    thumbnail_temp_path.replace(
        thumbnail_path
    )

    original_filename = (
        secure_filename(upload.filename)
        or "foto.jpg"
    )

    return StoredCommissionPhoto(
        original_filename=original_filename,
        storage_name=storage_name,
        thumbnail_storage_name=(
            thumbnail_storage_name
        ),
        mime_type="image/jpeg",
        size_bytes=len(original_data),
        content_sha256=content_hash,
    )