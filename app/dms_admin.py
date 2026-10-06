from __future__ import annotations

import json

from flask import (
    Blueprint, abort, flash, redirect, render_template,
    request, send_file, url_for,
)

from .audit import audit
from .auth import admin_required, foreman_required
from .dms_client import DmsError, normalize_hostname
from .dms_context import DOCUMENT_TYPES, available_fields
from .dms_service import (
    STATUS_LABELS, connection, document_path, field_name, profile_snapshot,
    refresh_metadata, repository_metadata, retry_document,
    seed_profiles, validate_profile,
)
from .extensions import db
from .models import Commission, DmsDocument, DmsProfile, SettingValueType
from .settings import SettingKeys, all_definitions, settings
from .work_report_document import (
    WorkReportDocumentError, commission_work_report_entries,
    create_work_report_pdf,
)


dms_admin_bp = Blueprint(
    "dms_admin", __name__, url_prefix="/admin/dms",
)


@dms_admin_bp.route("/connection", methods=["GET", "POST"])
@admin_required
def connection_page():
    legacy_definitions = [
        item
        for item in all_definitions()
        if item.key.startswith("dms.safety_")
    ]
    if request.method == "POST":
        try:
            hostname = request.form.get("hostname", "").strip()
            settings.set(
                SettingKeys.DMS_HOSTNAME,
                normalize_hostname(hostname) if hostname else "",
            )
            settings.set(
                SettingKeys.DMS_REPOSITORY,
                request.form.get("repository", "").strip(),
            )
            if request.form.get("clear_api_key"):
                settings.set(SettingKeys.DMS_API_KEY, "")
            elif request.form.get("api_key", "").strip():
                settings.set(
                    SettingKeys.DMS_API_KEY,
                    request.form["api_key"].strip(),
                )
            for definition in legacy_definitions:
                settings.set_from_form(
                    definition, request.form.get(definition.key),
                )
            enabled = request.form.get("enabled") == "on"
            if enabled:
                connection()
                seed_profiles()
                for document_type in DOCUMENT_TYPES:
                    try:
                        profile_snapshot(document_type)
                    except DmsError as exc:
                        raise DmsError(
                            f"{DOCUMENT_TYPES[document_type]}: {exc}"
                        ) from exc
            settings.set(SettingKeys.DMS_ENABLED, enabled)
            audit(
                "dms.connection.update", metadata={"enabled": enabled},
            )
            db.session.commit()
            flash("DMS-Anbindung gespeichert.", "success")
        except (DmsError, ValueError) as exc:
            db.session.rollback()
            flash(str(exc), "danger")
        return redirect(url_for("dms_admin.connection_page"))
    legacy_values = {
        item.key: (
            json.dumps(
                settings.get(item), ensure_ascii=False, indent=2,
            )
            if item.value_type == SettingValueType.JSON
            else settings.get(item)
        )
        for item in legacy_definitions
    }
    return render_template(
        "admin/dms_connection.html",
        hostname=settings.get(SettingKeys.DMS_HOSTNAME),
        repository=settings.get(SettingKeys.DMS_REPOSITORY),
        enabled=settings.get(SettingKeys.DMS_ENABLED),
        has_api_key=bool(settings.get(SettingKeys.DMS_API_KEY)),
        metadata=repository_metadata(),
        legacy_definitions=legacy_definitions,
        legacy_values=legacy_values,
    )


@dms_admin_bp.post("/metadata/refresh")
@admin_required
def metadata_refresh():
    try:
        seed_profiles()
        metadata = refresh_metadata()
        db.session.commit()
        flash(
            f"{len(metadata['categories'])} Dokumentkategorien eingelesen.",
            "success",
        )
    except DmsError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    except Exception:
        db.session.rollback()
        flash(
            "Die Repository-Metadaten konnten nicht eingelesen werden.",
            "danger",
        )
    return redirect(url_for("dms_admin.connection_page"))


@dms_admin_bp.get("/profiles")
@admin_required
def profiles():
    seed_profiles()
    db.session.commit()
    categories = repository_metadata().get("categories", {})
    rows = []
    for profile in DmsProfile.query.order_by(
        DmsProfile.document_type
    ).all():
        if profile.document_type not in DOCUMENT_TYPES:
            continue
        schema = categories.get(profile.category_id)
        error = None
        try:
            validate_profile(profile, schema)
        except DmsError as exc:
            error = str(exc)
        rows.append({
            "profile": profile, "schema": schema, "error": error,
        })
    return render_template(
        "admin/dms_profiles.html",
        rows=rows,
        document_types=DOCUMENT_TYPES,
    )


@dms_admin_bp.route(
    "/profiles/<document_type>", methods=["GET", "POST"],
)
@admin_required
def profile_edit(document_type):
    if document_type not in DOCUMENT_TYPES:
        abort(404)
    seed_profiles()
    db.session.commit()
    profile = db.get_or_404(DmsProfile, document_type)
    categories = repository_metadata().get("categories", {})
    if request.method == "POST":
        try:
            category_id = request.form.get("category_id", "")
            schema = categories.get(category_id)
            if schema is None:
                raise DmsError(
                    "Bitte eine eingelesene Dokumentkategorie auswählen."
                )
            mappings = {}
            key_property_id = request.form.get("key_property_id", "")
            for field in schema["fields"]:
                property_id = field["id"]
                if property_id == key_property_id:
                    continue
                source = request.form.get(
                    f"source.{property_id}", "",
                )
                if source == "__text__":
                    text = request.form.get(
                        f"value.{property_id}", "",
                    ).strip()
                    value = (
                        [
                            line.strip()
                            for line in text.splitlines()
                            if line.strip()
                        ]
                        if field.get("isMultiValue")
                        else text
                    )
                    mappings[property_id] = {
                        "kind": "text", "value": value,
                    }
                elif source:
                    mappings[property_id] = {
                        "kind": "field", "value": source,
                    }
            profile.category_id = category_id
            profile.key_property_id = key_property_id
            profile.mappings = mappings
            profile.schema = schema
            validate_profile(profile, schema)
            audit(
                "dms.profile.update", "dms_profile", document_type,
            )
            db.session.commit()
            flash("Dokumentzuordnung gespeichert.", "success")
            return redirect(url_for("dms_admin.profiles"))
        except DmsError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
    category_id = request.values.get(
        "category_id", profile.category_id,
    )
    schema = categories.get(category_id)
    mappings = (
        profile.mappings if category_id == profile.category_id else {}
    )
    mapping_text = {
        key: (
            "\n".join(str(item) for item in mapping.get("value", []))
            if isinstance(mapping.get("value"), list)
            else str(mapping.get("value", ""))
        )
        for key, mapping in mappings.items()
    }
    return render_template(
        "admin/dms_profile.html",
        profile=profile,
        title=DOCUMENT_TYPES[document_type],
        category_id=category_id,
        categories=categories,
        schema=schema,
        mappings=mappings,
        mapping_text=mapping_text,
        sources=available_fields(document_type),
        field_name=field_name,
    )


@dms_admin_bp.get("/documents")
@admin_required
def documents():
    status = request.args.get("status", "")
    if status and status not in STATUS_LABELS:
        abort(400)
    query = db.select(DmsDocument)
    if status:
        query = query.where(DmsDocument.status == status)
    pagination = db.paginate(
        query.order_by(DmsDocument.created_at.desc()),
        per_page=50,
        max_per_page=50,
    )
    return render_template(
        "admin/dms_documents.html",
        pagination=pagination,
        status=status,
        status_labels=STATUS_LABELS,
        document_types=DOCUMENT_TYPES,
    )


@dms_admin_bp.post("/documents/<document_id>/retry")
@admin_required
def document_retry(document_id):
    document = db.get_or_404(DmsDocument, document_id)
    try:
        retry_document(
            document,
            confirmed_absent=request.form.get("confirmed_absent") == "on",
        )
        db.session.commit()
        flash(
            "Archivierungsauftrag zur erneuten Bearbeitung vorgemerkt.",
            "success",
        )
    except DmsError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return redirect(url_for("dms_admin.documents"))


@dms_admin_bp.get("/documents/<document_id>/pdf")
@admin_required
def document_pdf(document_id):
    document = db.get_or_404(DmsDocument, document_id)
    try:
        path = document_path(document)
    except DmsError:
        abort(404)
    return send_file(
        path,
        mimetype="application/pdf",
        download_name=document.filename,
        as_attachment=False,
    )


@dms_admin_bp.post(
    "/commissions/<int:commission_id>/work-report/regenerate"
)
@foreman_required
def work_report_regenerate(commission_id):
    commission = db.get_or_404(Commission, commission_id)
    if commission.completed_at is None:
        abort(
            409,
            description=(
                "Ein Arbeitsnachweis kann erst nach "
                "Auftragsabschluss neu erzeugt werden."
            ),
        )
    try:
        create_work_report_pdf(
            commission, commission_work_report_entries(commission),
        )
        audit(
            "commission.work_report.regenerate",
            "commission",
            commission.id,
        )
        db.session.commit()
        flash(
            "Arbeitsnachweis neu erzeugt. "
            "Der Archivierungsstatus steht unter Archiv/DMS.",
            "success",
        )
    except WorkReportDocumentError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return redirect(
        url_for("admin.commission_detail", commission_id=commission.id)
    )