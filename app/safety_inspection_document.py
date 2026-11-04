from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4
import re

from flask import current_app, render_template
from pypdf import PdfReader
from weasyprint import HTML

from .audit import audit
from .extensions import db
from .models import (
    SafetyInspectionJob,
    SafetyInspectionJobStatus,
    SafetyInspectionProtocol,
    SafetyInspectionResultState,
    User,
)
from .photo_service import commission_photo_path
from .utils import ensure_utc


class SafetyInspectionDocumentError(
    RuntimeError
):
    pass


STATE_LABELS = {
    SafetyInspectionResultState
    .NOT_CHECKED.value: "Nicht geprüft",
    SafetyInspectionResultState
    .NO_DEFECT.value: "Ohne Mangel",
    SafetyInspectionResultState
    .NOT_PRESENT.value: "Nicht vorhanden",
    SafetyInspectionResultState
    .DEFECT_INFO.value: "Mangel",
    SafetyInspectionResultState
    .DEFECT_URGENT.value: "Mangel",
}


STATE_CLASSES = {
    SafetyInspectionResultState
    .NOT_CHECKED.value: "not-checked",
    SafetyInspectionResultState
    .NO_DEFECT.value: "no-defect",
    SafetyInspectionResultState
    .NOT_PRESENT.value: "not-present",
    SafetyInspectionResultState
    .DEFECT_INFO.value: "defect-info",
    SafetyInspectionResultState
    .DEFECT_URGENT.value: "defect-urgent",
}


OBJECT_LEVEL_LABELS = {
    "economic_unit": "Wirtschaftseinheit",
    "building": "Gebäude",
    "use_unit": "Wohnung/Nutzungseinheit",
}


def format_date(
    value: date | None,
) -> str:
    if value is None:
        return ""

    return value.strftime("%d.%m.%Y")


def format_datetime(
    value: datetime | None,
) -> str:
    if value is None:
        return ""

    return value.strftime(
        "%d.%m.%Y %H:%M"
    )


def safe_filename(value: str) -> str:
    normalized = re.sub(
        r"[^A-Za-z0-9._-]+",
        "-",
        str(value or "").strip(),
    ).strip("-")

    if not normalized:
        raise (
            SafetyInspectionDocumentError(
                "Für das Prüfprotokoll konnte "
                "kein Dateiname gebildet werden."
            )
        )

    return normalized


def result_rows(
    job: SafetyInspectionJob,
) -> list[dict]:
    rows = []

    for number, result in enumerate(
        sorted(
            job.results,
            key=lambda item: (
                item.position_index,
                item.id or 0,
            ),
        ),
        start=1,
    ):
        photos = []

        for photo in sorted(
            (
                item
                for item in result.photos
                if item.active
                and item.deleted_at is None
            ),
            key=lambda item: (
                ensure_utc(item.created_at),
                item.id or 0,
            ),
        ):
            photo_path = (
                commission_photo_path(
                    photo.storage_name
                )
            )

            if not photo_path.is_file():
                raise (
                    SafetyInspectionDocumentError(
                        "Die Fotodatei zu "
                        f"Prüfpunkt "
                        f"„{result.display_title}“ "
                        "fehlt."
                    )
                )

            photos.append(
                {
                    "uri": (
                        photo_path
                        .resolve()
                        .as_uri()
                    ),
                    "caption": (
                        photo.caption or ""
                    ),
                    "captured_at": (
                        format_datetime(
                            photo.captured_at
                            or photo.created_at
                        )
                    ),
                }
            )

        rows.append(
            {
                "number": number,
                "title": result.item_title,
                "catalog_position": result.item_catalog_position or "",
                "section_key": result.section_key,
                "section_title": result.section_title,
                "description": (
                    result.item_description
                    or ""
                ),
                "state": result.state,
                "state_label": (
                    STATE_LABELS.get(
                        result.state,
                        result.state,
                    )
                ),
                "state_class": (
                    STATE_CLASSES.get(
                        result.state,
                        "not-checked",
                    )
                ),
                "note": result.note or "",
                "immediate_action": (
                    result.immediate_action
                    or ""
                ),
                "checked_by": (
                    result.checked_by
                    .display_name
                    if result.checked_by
                    else ""
                ),
                "checked_at": (
                    format_datetime(
                        result.checked_at
                    )
                ),
                "photos": photos,
            }
        )

    return rows


def document_context(
    job: SafetyInspectionJob,
    version_number: int,
    correction_reason: str | None,
) -> dict:
    rows = result_rows(job)
    counts = Counter(
        row["state"]
        for row in rows
    )
    template = (
        job.template_version.template
    )

    return {
        "job": job,
        "protocol_version": (
            version_number
        ),
        "correction_reason": (
            correction_reason or ""
        ),
        "template_name": template.name,
        "template_version": (
            job.template_version
            .version_number
        ),
        "legal_reference": (
            template.legal_reference or ""
        ),
        "group_name": job.group.name,
        "object_level_label": (
            OBJECT_LEVEL_LABELS.get(
                job.object_level,
                job.object_level,
            )
        ),
        "period_text": (
            f"{format_date(job.period_start)} "
            "bis "
            f"{format_date(job.period_end)}"
        ),
        "due_text": (
            f"{format_date(job.due_from)} "
            "bis "
            f"{format_date(job.due_to)}"
        ),
        "completed_at": (
            format_datetime(
                job.completed_at
            )
        ),
        "completed_by": (
            job.completed_by.display_name
            if job.completed_by
            else ""
        ),
        "assigned_user": (
            job.assigned_user.display_name
            if job.assigned_user
            else ""
        ),
        "department_name": (
            job.department.name
            if job.department
            else ""
        ),
        "rows": rows,
        "summary": {
            "total": len(rows),
            "no_defect": counts.get(
                SafetyInspectionResultState
                .NO_DEFECT.value,
                0,
            ),
            "not_present": counts.get(
                SafetyInspectionResultState
                .NOT_PRESENT.value,
                0,
            ),
            "defects": (
                counts.get(
                    SafetyInspectionResultState
                    .DEFECT_INFO.value,
                    0,
                )
                + counts.get(
                    SafetyInspectionResultState
                    .DEFECT_URGENT.value,
                    0,
                )
            ),
            "urgent": counts.get(
                SafetyInspectionResultState
                .DEFECT_URGENT.value,
                0,
            ),
        },
        "created_at": format_datetime(
            datetime.now().astimezone()
        ),
    }


def protocol_storage_root() -> Path:
    return (
        Path(current_app.instance_path)
        / "safety_inspection_protocols"
    ).resolve()


def resolve_safety_inspection_protocol_path(
    document_filename: str,
) -> Path:
    storage_root = (
        protocol_storage_root()
    )
    candidate = (
        Path(current_app.instance_path)
        / document_filename
    ).resolve()

    if (
        candidate != storage_root
        and storage_root
        not in candidate.parents
    ):
        raise (
            SafetyInspectionDocumentError(
                "Der Pfad des Prüfprotokolls "
                "ist ungültig."
            )
        )

    return candidate


def create_safety_inspection_protocol(
    job: SafetyInspectionJob,
    user: User,
    correction_reason: str | None = None,
) -> SafetyInspectionProtocol:
    if job.status not in {
        SafetyInspectionJobStatus
        .COMPLETED.value,
        SafetyInspectionJobStatus
        .COMPLETED_WITH_DEFECTS.value,
    }:
        raise (
            SafetyInspectionDocumentError(
                "Ein Prüfprotokoll kann erst "
                "nach Abschluss der Prüfung "
                "erstellt werden."
            )
        )

    normalized_reason = str(
        correction_reason or ""
    ).strip()
    version_number = max(
        (
            protocol.version_number
            for protocol in job.protocols
        ),
        default=0,
    ) + 1

    if (
        version_number > 1
        and not normalized_reason
    ):
        raise (
            SafetyInspectionDocumentError(
                "Für eine neue "
                "Protokollversion ist eine "
                "Korrekturbegründung "
                "erforderlich."
            )
        )

    context = document_context(
        job,
        version_number,
        normalized_reason,
    )
    completion_date = (
        job.completed_at.date()
        if job.completed_at is not None
        else date.today()
    )
    public_id = safe_filename(
        job.public_id
    )
    target_directory = (
        protocol_storage_root()
        / str(completion_date.year)
        / public_id
    )
    target_directory.mkdir(
        parents=True,
        exist_ok=True,
    )
    target_path = (
        target_directory
        / (
            f"pruefprotokoll-{public_id}-"
            f"v{version_number}.pdf"
        )
    )
    temporary_path = (
        target_path.with_name(
            f".{target_path.name}."
            f"{uuid4().hex}.tmp"
        )
    )

    try:
        html = render_template(
            (
                "admin/"
                "safety_inspection_"
                "protocol_pdf.html"
            ),
            **context,
        )
        HTML(
            string=html,
            base_url=(
                current_app.root_path
            ),
        ).write_pdf(
            str(temporary_path)
        )

        reader = PdfReader(
            str(temporary_path)
        )

        if not reader.pages:
            raise (
                SafetyInspectionDocumentError(
                    "Das erzeugte "
                    "Prüfprotokoll enthält "
                    "keine Seite."
                )
            )

        temporary_path.replace(
            target_path
        )

    except SafetyInspectionDocumentError:
        temporary_path.unlink(
            missing_ok=True
        )
        raise

    except Exception as exc:
        temporary_path.unlink(
            missing_ok=True
        )
        raise (
            SafetyInspectionDocumentError(
                "Das Prüfprotokoll konnte "
                "nicht erstellt werden."
            )
        ) from exc

    document_hash = sha256(
        target_path.read_bytes()
    ).hexdigest()
    instance_path = Path(
        current_app.instance_path
    ).resolve()

    protocol = SafetyInspectionProtocol(
        job=job,
        version_number=version_number,
        correction_reason=(
            normalized_reason or None
        ),
        document_filename=str(
            target_path.relative_to(
                instance_path
            )
        ),
        document_sha256=document_hash,
        created_by=user,
    )
    db.session.add(protocol)
    db.session.flush()

    from .dms_context import protocol_context
    from .dms_service import register_generated_pdf

    try:
        register_generated_pdf(
            f"safety_protocol_{job.object_level}",
            "safety_protocol",
            protocol.id,
            target_path.name,
            target_path,
            protocol_context(protocol),
        )
    except (RuntimeError, OSError) as exc:
        raise SafetyInspectionDocumentError(
            "Der Archivierungsauftrag konnte nicht gespeichert werden."
        ) from exc

    audit(
        (
            "safety_inspection."
            "protocol.create"
        ),
        "safety_inspection_protocol",
        protocol.id,
        {
            "job_id": job.id,
            "version_number": (
                version_number
            ),
            "correction_reason": (
                normalized_reason or None
            ),
            "pdf_sha256": document_hash,
        },
    )

    return protocol