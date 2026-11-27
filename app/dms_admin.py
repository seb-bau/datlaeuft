from __future__ import annotations

import json

from flask import (
    Blueprint, abort, flash, redirect, render_template,
    request, send_file, url_for,
)
from sqlalchemy.exc import IntegrityError

from .utils import commission_detail_url
from .audit import audit
from .auth import admin_required, foreman_required
from .dms_client import DmsError, normalize_hostname
from .dms_context import DOCUMENT_TYPES, available_fields, template_fields
from .dms_service import (
    INVOICE_DOCUMENT_TYPES, STATUS_LABELS, connection, document_path, field_name,
    link_existing_document, profile_snapshot, refresh_metadata,
    remove_from_queue, repository_metadata, retry_document,
    seed_profiles, validate_profile,
)
from .extensions import db
from .models import Commission, DmsDocument, DmsProfile, SettingValueType
from .settings import SettingKeys, all_definitions, settings
from .work_report_document import (
    WorkReportDocumentError, commission_work_report_entries,
    create_work_report_pdf,
)
from flask import current_app
from traceback import format_exception
from urllib.parse import quote, quote_plus


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
                    if document_type == "invoice_with_work_reports":
                        continue
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
    from .invoice_dms_dispatch import dispatch_settings

    metadata = repository_metadata()
    category_ids = {
        profile.category_id for profile in DmsProfile.query.filter(
            DmsProfile.document_type.in_(("invoice", "cancellation_invoice")),
        )
    }
    dispatch_fields = {
        field["id"]: field_name(field)
        for category_id in category_ids
        for field in metadata.get("categories", {}).get(category_id, {}).get("fields", [])
        if field["dataType"] == "STRING" and not field.get("isMultiValue")
    }
    return render_template(
        "admin/dms_connection.html",
        hostname=settings.get(SettingKeys.DMS_HOSTNAME),
        repository=settings.get(SettingKeys.DMS_REPOSITORY),
        enabled=settings.get(SettingKeys.DMS_ENABLED),
        has_api_key=bool(settings.get(SettingKeys.DMS_API_KEY)),
        metadata=metadata,
        legacy_definitions=legacy_definitions,
        legacy_values=legacy_values,
        paused=settings.get(SettingKeys.DMS_PROCESSING_PAUSED),
        dispatch=dispatch_settings(),
        dispatch_fields=sorted(dispatch_fields.items(), key=lambda item: item[1]),
    )


@dms_admin_bp.post("/invoice-dispatch")
@admin_required
def invoice_dispatch_settings():
    from .invoice_dms_dispatch import PAPERWORK_ID

    try:
        postal = request.form.get("postal_property", "").strip()
        portal = request.form.get("portal_property", "").strip()
        signer = request.form.get("signer_property", "").strip()
        alternate_signer = request.form.get("alternate_signer_property", "").strip()
        system_user = request.form.get("system_user_id", "").strip()
        ids = [value for value in (postal, portal, signer, alternate_signer) if value]
        if any(len(value) > 128 or any(not char.isprintable() or char.isspace() for char in value) for value in ids):
            raise ValueError("Bitte gültige DMS-Eigenschafts-IDs eingeben.")
        if len(set(ids)) != len(ids):
            raise ValueError("Bitte für den Rechnungsversand unterschiedliche DMS-Eigenschaften auswählen.")
        if system_user and not PAPERWORK_ID.fullmatch(system_user):
            raise ValueError("Bitte die d.velop-ID des Paperwork-Systembenutzers prüfen.")
        postal_ids = {postal, signer} - {""} if postal else set()
        invoice_ids = {portal, alternate_signer if postal else ""} - {""}
        for profile in DmsProfile.query:
            property_ids = {profile.key_property_id, *profile.mappings}
            if postal_ids.intersection(property_ids) or (
                profile.document_type in INVOICE_DOCUMENT_TYPES and invoice_ids.intersection(property_ids)
            ):
                raise ValueError("Bitte die Versandeigenschaften aus den festen DMS-Dokumentzuordnungen entfernen.")
        for definition, value in (
            (SettingKeys.DMS_POSTAL_PROPERTY, postal),
            (SettingKeys.DMS_PORTAL_PROPERTY, portal),
            (SettingKeys.DMS_POSTAL_SIGNER_PROPERTY, signer),
            (SettingKeys.DMS_POSTAL_DEFAULT, "postal_default" in request.form),
            (SettingKeys.DMS_PORTAL_DEFAULT, "portal_default" in request.form),
            (SettingKeys.DMS_POSTAL_SYSTEM_USER_ID, system_user),
            (SettingKeys.DMS_POSTAL_ALTERNATE_SIGNER_PROPERTY, alternate_signer),
        ):
            settings.set(definition, value)
        audit("dms.invoice_dispatch.settings")
        db.session.commit()
        flash("Rechnungsversand über DMS gespeichert.", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return redirect(url_for("dms_admin.connection_page"))


@dms_admin_bp.post("/invoice-dispatch/users")
@admin_required
def invoice_dispatch_users():
    from .invoice_dms_dispatch import import_paperwork_signers

    upload = request.files.get("paperwork_users")
    try:
        if not upload or not upload.filename:
            raise ValueError("Bitte die JSON-Benutzerliste aus Paperwork auswählen.")
        content = upload.read(1_048_577)
        if len(content) > 1_048_576:
            raise ValueError("Die Paperwork-Benutzerliste darf höchstens 1 MB groß sein.")
        try:
            payload = json.loads(content)
        except (ValueError, RecursionError) as exc:
            raise ValueError("Die Paperwork-Benutzerliste konnte nicht als JSON gelesen werden.") from exc
        matched, skipped = import_paperwork_signers(payload)
        audit("dms.invoice_dispatch.import_users", metadata={"matched": matched, "skipped": skipped})
        db.session.commit()
        flash(f"Paperwork-Zuordnungen übernommen: {matched}. Nicht zugeordnet: {skipped} aktive Konten mit Entra-ID.", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    except IntegrityError:
        db.session.rollback()
        flash("Benutzerzuordnungen wurden gleichzeitig geändert. Bitte den Import wiederholen.", "danger")
    return redirect(url_for("dms_admin.connection_page"))


@dms_admin_bp.post("/processing/pause")
@admin_required
def processing_pause():
    raw_value = request.form.get("paused")
    if raw_value not in ("true", "false"):
        abort(400)

    paused = raw_value == "true"
    settings.set(SettingKeys.DMS_PROCESSING_PAUSED, paused)
    audit("dms.processing.pause", metadata={"paused": paused})
    db.session.commit()

    flash(
        "Automatische PDF-Übertragung pausiert."
        if paused else "Automatische PDF-Übertragung fortgesetzt.",
        "warning" if paused else "success",
    )
    if request.form.get("return_to") == "documents":
        return redirect_to_documents()
    return redirect(url_for("dms_admin.connection_page"))


@dms_admin_bp.post("/metadata/refresh")
@admin_required
def metadata_refresh():
    api_key = ""
    try:
        api_key = str(settings.get(SettingKeys.DMS_API_KEY) or "").strip()
        current_app.logger.info("dms_metadata_refresh_started")

        seed_profiles()
        metadata = refresh_metadata()
        db.session.commit()

        current_app.logger.info(
            "dms_metadata_refresh_completed categories=%s",
            len(metadata["categories"]),
        )
        flash(
            f"{len(metadata['categories'])} Dokumentkategorien eingelesen.",
            "success",
        )
    except Exception as exc:
        db.session.rollback()

        details = "".join(
            format_exception(type(exc), exc, exc.__traceback__)
        )
        message = (
            str(exc)
            if isinstance(exc, DmsError)
            else "Die Repository-Metadaten konnten nicht eingelesen werden."
        )

        if api_key:
            for secret in {
                api_key,
                quote(api_key, safe=""),
                quote_plus(api_key, safe=""),
            }:
                details = details.replace(secret, "[API-Key ausgeblendet]")
                message = message.replace(secret, "[API-Key ausgeblendet]")

        current_app.logger.error(
            "dms_metadata_refresh_failed\n%s",
            details,
        )
        flash(message, "danger")

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
            key_property_id = request.form.get("key_property_id", "").strip()
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
        template_sources=template_fields(document_type),
        field_name=field_name,
    )


def redirect_to_documents():
    status = request.args.get("status")
    return redirect(url_for(
        "dms_admin.documents", status=status if status == "" or status in STATUS_LABELS else None,
    ))


@dms_admin_bp.get("/documents")
@admin_required
def documents():
    status = request.args.get("status", "blocked")
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
        enabled=settings.get(SettingKeys.DMS_ENABLED),
        paused=settings.get(SettingKeys.DMS_PROCESSING_PAUSED),
    )


@dms_admin_bp.post("/documents/<document_id>/retry")
@admin_required
def document_retry(document_id):
    document = db.get_or_404(DmsDocument, document_id)
    try:
        retry_document(
            document,
            confirmed_absent=request.form.get("confirmed_absent") == "on",
            manual=True,
        )
        db.session.commit()
        flash("Einzelübertragung vorgemerkt.", "success")
    except DmsError as exc:
        db.session.rollback()
        flash(str(exc), "danger")

    return redirect_to_documents()


@dms_admin_bp.post("/documents/<document_id>/remove")
@admin_required
def document_remove(document_id):
    document = db.get_or_404(DmsDocument, document_id)
    try:
        remove_from_queue(
            document,
            confirmed_absent=request.form.get("confirmed_absent") == "on",
        )
        db.session.commit()
        flash(
            "Aus der Warteschlange entfernt. Die lokale PDF bleibt erhalten.",
            "success",
        )
    except DmsError as exc:
        db.session.rollback()
        flash(str(exc), "danger")

    return redirect_to_documents()


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


@dms_admin_bp.post("/documents/<document_id>/link")
@admin_required
def document_link(document_id):
    document = db.get_or_404(DmsDocument, document_id)
    try:
        if request.form.get("confirmed_document") != "on":
            raise DmsError("Bitte bestätigen, dass das DMS-Dokument zu diesem PDF gehört.")

        link_existing_document(document, request.form.get("dms_document_id", ""))
        db.session.commit()
        flash("Vorhandene d.velop-ID zugeordnet. Das Dokument ist archiviert.", "success")
    except DmsError as exc:
        db.session.rollback()
        flash(str(exc), "danger")

    return redirect_to_documents()


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
        commission_detail_url(commission.id)
    )
