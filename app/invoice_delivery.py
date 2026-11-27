from email import policy
from email.message import EmailMessage
from hashlib import sha256
from io import BytesIO
from uuid import UUID

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import current_user
from werkzeug.utils import secure_filename

from .audit import audit
from .auth import invoice_required
from .blacklist import blacklist_ids, is_blacklisted
from .email_templates import render_email_template
from .extensions import db
from .invoice_attachments import invoice_attachment_path, remove_invoice_attachment_files, store_invoice_attachment
from .invoice_document import InvoiceDocumentError, resolve_invoice_document_path
from .invoice_payments import payment_value
from .models import (
    Invoice, InvoiceAttachment, InvoiceKind, WorkflowNotification, WowiCompanyCode, WowiResponsibleOfficial, utc_now,
)
from .settings import SettingKeys, settings, validate_debug_email_address
from .workflow_notifications import enqueue_notification
from .wowiport import WowiportService


invoice_delivery_bp = Blueprint("invoice_delivery", __name__)

def invoice_response(data, status=200):
    response = jsonify(data)
    response.status_code = status
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    return response


def locked_draft(invoice_id):
    invoice = Invoice.query.filter_by(id=invoice_id).populate_existing().with_for_update().first_or_404()
    if invoice.status != "draft":
        abort(invoice_response({"message": "PDF-Anhänge können nur im Entwurf geändert werden."}, 409))
    return invoice


def final_invoice(invoice_id, *, lock=False):
    invoice = db.get_or_404(Invoice, invoice_id, with_for_update=lock, populate_existing=lock)
    if invoice.status != "finalized" or not invoice.invoice_number or not invoice.document_filename or not invoice.document_sha256:
        abort(invoice_response({"message": "Bitte zuerst die Rechnung erstellen."}, 409))
    return invoice


def final_invoice_pdf(invoice):
    try:
        path = resolve_invoice_document_path(invoice.document_filename)
        data = path.read_bytes()
    except (InvoiceDocumentError, OSError) as exc:
        raise ValueError("Die festgeschriebene Rechnungs-PDF ist nicht verfügbar.") from exc
    if not data.startswith(b"%PDF-") or sha256(data).hexdigest() != invoice.document_sha256:
        raise ValueError("Die Rechnungsdatei stimmt nicht mit der festgeschriebenen Rechnung überein.")
    return data


def invoice_pdf_filename(invoice):
    return secure_filename(f"Rechnung-{invoice.invoice_number}.pdf")


def attachment_data(attachment):
    return {"id": attachment.id, "name": attachment.original_filename, "pages": attachment.page_count,
            "size_bytes": attachment.size_bytes, "position_index": attachment.position_index}


@invoice_delivery_bp.get("/admin/invoices/<int:invoice_id>/attachments")
@invoice_required
def attachment_list(invoice_id):
    invoice = db.get_or_404(Invoice, invoice_id)
    return invoice_response({"items": [attachment_data(item) for item in invoice.pdf_attachments], "editable": invoice.status == "draft"})


@invoice_delivery_bp.post("/admin/invoices/<int:invoice_id>/attachments")
@invoice_required
def attachment_upload(invoice_id):
    invoice = locked_draft(invoice_id)
    uploads = [upload for upload in request.files.getlist("files") if upload.filename]
    if not uploads:
        return invoice_response({"message": "Bitte mindestens eine PDF-Datei auswählen."}, 400)
    paths = []
    try:
        position = max((item.position_index for item in invoice.pdf_attachments), default=-1) + 1
        for offset, upload in enumerate(uploads):
            attachment = store_invoice_attachment(invoice, upload, current_user, position + offset)
            paths.append(invoice_attachment_path(attachment))
            db.session.add(attachment)
        db.session.flush()
        audit("invoice.attachments.add", "invoice", invoice.id, {"count": len(uploads)})
        db.session.commit()
    except (ValueError, OSError) as exc:
        db.session.rollback()
        remove_invoice_attachment_files(paths)
        if isinstance(exc, OSError):
            current_app.logger.exception("invoice_attachment_upload_failed invoice_id=%s", invoice_id)
        return invoice_response({"message": str(exc) if isinstance(exc, ValueError) else "Die PDF-Anhänge konnten nicht gespeichert werden."}, 400 if isinstance(exc, ValueError) else 500)
    except Exception:
        db.session.rollback()
        remove_invoice_attachment_files(paths)
        current_app.logger.exception("invoice_attachment_upload_failed invoice_id=%s", invoice_id)
        return invoice_response({"message": "Die PDF-Anhänge konnten nicht gespeichert werden."}, 500)
    return invoice_response({"items": [attachment_data(item) for item in invoice.pdf_attachments], "editable": True}, 201)


@invoice_delivery_bp.delete("/admin/invoices/<int:invoice_id>/attachments/<int:attachment_id>")
@invoice_required
def attachment_delete(invoice_id, attachment_id):
    invoice = locked_draft(invoice_id)
    attachment = InvoiceAttachment.query.filter_by(id=attachment_id, invoice_id=invoice.id).first_or_404()
    path = invoice_attachment_path(attachment)
    audit("invoice.attachments.delete", "invoice", invoice.id, {"attachment_id": attachment.id})
    db.session.delete(attachment)
    db.session.commit()
    remove_invoice_attachment_files([path])
    return invoice_response({"message": "PDF-Anhang entfernt."})


@invoice_delivery_bp.get("/admin/invoices/<int:invoice_id>/attachments/<int:attachment_id>")
@invoice_required
def attachment_document(invoice_id, attachment_id):
    db.get_or_404(Invoice, invoice_id)
    attachment = InvoiceAttachment.query.filter_by(id=attachment_id, invoice_id=invoice_id).first_or_404()
    try:
        path = invoice_attachment_path(attachment)
        if not path.is_file():
            abort(404)
    except ValueError:
        abort(404)
    response = send_file(path, mimetype="application/pdf", as_attachment=False, download_name=attachment.original_filename)
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def selected_official(official_id):
    try:
        official_id = int(official_id)
    except (ValueError, TypeError) as exc:
        raise ValueError("Bitte einen Sachbearbeiter auswählen.") from exc
    official = db.session.get(WowiResponsibleOfficial, official_id)
    if official is None or not official.active or is_blacklisted("official", official.erp_id) or is_blacklisted("person", official.person_erp_id):
        raise ValueError("Der ausgewählte Sachbearbeiter ist nicht verfügbar.")
    row = WowiportService().get_responsible_official(official.erp_id)
    if payment_value(row, "Id") != official.erp_id or payment_value(row, "Active", True) is False:
        raise ValueError("Der ausgewählte Sachbearbeiter konnte nicht eindeutig aus Wowiport geladen werden.")
    person = payment_value(row, "Person") or {}
    if is_blacklisted("person", payment_value(person, "Id")):
        raise ValueError("Der ausgewählte Sachbearbeiter ist nicht verfügbar.")
    communication = payment_value(person, "FirstEmailCommunication") or {}
    recipient = validate_debug_email_address(str(payment_value(communication, "Content") or "").strip())
    if not recipient:
        raise ValueError("Für den ausgewählten Sachbearbeiter ist keine gültige E-Mail-Adresse hinterlegt.")
    return official, recipient


def selected_invoice_recipient(invoice, selection):
    if invoice.tenant_billing_to_company:
        if selection != "company" or not invoice.recipient_email:
            raise ValueError("Bitte wählen Sie die hinterlegte Rechnungsadresse des empfangenden Buchwerks.")
        return "company", invoice.recipient_name, validate_debug_email_address(invoice.recipient_email)

    if selection == "tenant" and invoice.tenant_billing_reference and invoice.recipient_email:
        return "tenant", invoice.recipient_name, validate_debug_email_address(invoice.recipient_email)

    official, recipient = selected_official(selection)
    return official.erp_id, official.name, recipient


def invoice_mail_content(invoice, recipient_name):
    return render_email_template("invoice_tenant_charge", {
        "invoice_number": invoice.invoice_number, "invoice_recipient_name": invoice.recipient_name or "",
        "contract_number": invoice.billing_contract_number or "", "recipient_name": recipient_name or "",
        "total_gross": f"{invoice.total_gross:,.2f}".replace(",", "_").replace(".", ",").replace("_", "."),
        "currency": invoice.currency_code, "invoice_date": invoice.invoice_date.strftime("%d.%m.%Y") if invoice.invoice_date else "",
        "due_date": invoice.due_date.strftime("%d.%m.%Y") if invoice.due_date else "",
        "company_name": invoice.company_code.name or invoice.company_code_code, "sender_name": current_user.display_name,
    })


@invoice_delivery_bp.get("/admin/invoices/<int:invoice_id>/delivery/recipients")
@invoice_required
def delivery_recipients(invoice_id):
    invoice = final_invoice(invoice_id)
    if invoice.tenant_billing_to_company:
        items = [{"id": "company", "name": invoice.recipient_name}] if invoice.recipient_email else []
        return invoice_response({"items": items})

    query = WowiResponsibleOfficial.query.filter_by(active=True).filter(~WowiResponsibleOfficial.erp_id.in_(blacklist_ids("official")))
    items = [
        {"id": item.erp_id, "name": item.name or f"Sachbearbeiter {item.erp_id}"}
        for item in query.order_by(WowiResponsibleOfficial.name, WowiResponsibleOfficial.erp_id).all()
        if not is_blacklisted("person", item.person_erp_id)
    ]
    if invoice.tenant_billing_reference and invoice.recipient_email:
        items.insert(0, {"id": "tenant", "name": invoice.recipient_name})
    return invoice_response({"items": items})


@invoice_delivery_bp.get("/admin/invoices/<int:invoice_id>/delivery/preview")
@invoice_required
def delivery_preview(invoice_id):
    invoice = final_invoice(invoice_id)
    try:
        _, recipient_name, recipient = selected_invoice_recipient(invoice, request.args.get("official_id"))
        content = invoice_mail_content(invoice, recipient_name)
    except ValueError as exc:
        return invoice_response({"message": str(exc)}, 400)
    except Exception:
        current_app.logger.exception("invoice_mail_preview_failed invoice_id=%s", invoice_id)
        return invoice_response({"message": "Die E-Mail-Adresse konnte nicht aus Wowiport geladen werden."}, 502)
    return invoice_response({"recipient": recipient, **content})


def validate_delivery_retry(item, official_id, body):
    if str(item.payload.get("official_id")) != str(official_id):
        raise ValueError("Die Versandkennung wurde bereits für einen anderen Empfänger verwendet. Bitte den Dialog erneut öffnen.")
    if body is not None and item.payload.get("body") != body:
        raise ValueError("Diese Mail wurde bereits mit einem anderen Text zum Versand vorgemerkt. Bitte den Versandstatus prüfen.")


@invoice_delivery_bp.post("/admin/invoices/<int:invoice_id>/delivery/send")
@invoice_required
def delivery_send(invoice_id):
    invoice = final_invoice(invoice_id, lock=True)
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return invoice_response({"message": "Bitte gültige Versandangaben übermitteln."}, 400)
    completing = data.get("complete_invoice") is True
    correspondence = {}
    try:
        from .invoice_dms_dispatch import dispatch_channel, existing_dispatch, queue_invoice_dispatch

        channel = dispatch_channel("direct", data.get("postal", False), data.get("portal", False)) if completing else None
        body = None
        if "body" in data:
            body = data["body"]
            if not isinstance(body, str) or len(body) > 250000 or "\x00" in body:
                raise ValueError("Der Mailtext muss ein Text mit höchstens 250.000 Zeichen sein und darf keine ungültigen Zeichen enthalten.")
            body = body.replace("\r\n", "\n").replace("\r", "\n")
        try:
            token = str(UUID(str(data.get("request_id") or "")))
        except ValueError as exc:
            raise ValueError("Die Versandkennung ist ungültig. Bitte den Dialog erneut öffnen.") from exc
        prefix = completion_notification_prefix(invoice) if completing else f"invoice-mail:{invoice.id}:"
        key = prefix + token
        existing = WorkflowNotification.query.filter_by(notification_key=key).one_or_none()
        if existing is not None:
            validate_delivery_retry(existing, data.get("official_id"), body)
            if existing.payload.get("dms_channel") != channel:
                raise ValueError("Für diese Versandkennung ist bereits eine andere DMS-Auswahl gespeichert.")
            return invoice_response(delivery_data(existing), 200)
        if completing and invoice.completed_at:
            return invoice_response({"message": "Die Rechnung wurde bereits abgeschlossen."}, 409)
        if pending_invoice_completion(invoice):
            return invoice_response({"message": "Für die Rechnung läuft bereits ein Direktversand. Bitte den Versandstatus prüfen."}, 409)
        if completing and (dispatch := existing_dispatch(invoice.id, lock=True)) and dispatch.invoice_dispatch["mode"] == "dms":
            return invoice_response({"message": "Für die Rechnung wurde bereits ein Versand über das DMS vorgemerkt."}, 409)
        if not settings.get(SettingKeys.SMTP_HOST).strip() or not settings.get(SettingKeys.SMTP_SENDER).strip():
            raise ValueError("SMTP-Server oder Absenderadresse fehlen. Bitte die E-Mail-Einstellungen ergänzen.")
        original = final_invoice_pdf(invoice)
        destination_id, recipient_name, recipient = selected_invoice_recipient(invoice, data.get("official_id"))
        content = invoice_mail_content(invoice, recipient_name)
        if body is not None:
            content["body"] = body
        if completing or invoice.cover_letter_title is not None:
            from .invoice_correspondence import store_delivery_correspondence

            package = completion_package(invoice, original) if completing else None
            if channel:
                queue_invoice_dispatch(invoice, package, "direct", channel, current_user)
            if package != original:
                correspondence = store_delivery_correspondence(invoice, content=package)
        item = enqueue_notification(key, "invoice_delivery", payload={
            "invoice_id": invoice.id, "invoice_number": invoice.invoice_number,
            "document_filename": invoice.document_filename, "document_sha256": invoice.document_sha256,
            "official_id": destination_id, "recipient": recipient, "requested_by_user_id": current_user.id,
            "complete_invoice": completing, "dms_channel": channel, **content, **correspondence,
        })
        validate_delivery_retry(item, data.get("official_id"), body)
        if correspondence and item.payload.get("correspondence_filename") != correspondence["correspondence_filename"]:
            resolve_invoice_document_path(correspondence["correspondence_filename"]).unlink(missing_ok=True)
            correspondence = {}
        audit("invoice.mail_queued", "invoice", invoice.id, {"notification_id": item.id, "recipient": recipient, "official_id": destination_id})
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        if correspondence:
            resolve_invoice_document_path(correspondence["correspondence_filename"]).unlink(missing_ok=True)
        return invoice_response({"message": str(exc) or "Die Versandkennung ist ungültig. Bitte den Dialog erneut öffnen."}, 400)
    except Exception:
        db.session.rollback()
        if correspondence:
            resolve_invoice_document_path(correspondence["correspondence_filename"]).unlink(missing_ok=True)
        current_app.logger.exception("invoice_mail_queue_failed invoice_id=%s", invoice_id)
        return invoice_response({"message": "Die Rechnung konnte nicht zum Versand vorgemerkt werden."}, 502)
    return invoice_response(delivery_data(item), 202)


def delivery_data(item):
    return {
        "id": item.id, "state": item.state,
        "recipient": item.payload.get("delivery_recipient") or item.payload.get("recipient"),
        "message": item.last_error if item.state in {"failed", "uncertain"} else "",
        "sent_at": item.sent_at.isoformat() if item.sent_at else None,
        "test_delivery": invoice_delivery_was_redirected(item),
    }


@invoice_delivery_bp.get("/admin/invoices/<int:invoice_id>/delivery/status")
@invoice_required
def delivery_status(invoice_id):
    invoice = db.get_or_404(Invoice, invoice_id)
    prefix = completion_notification_prefix(invoice) if request.args.get("complete") == "1" else f"invoice-mail:{invoice_id}:"
    item = WorkflowNotification.query.filter(WorkflowNotification.notification_key.startswith(prefix))
    notification_id = request.args.get("notification_id", type=int)
    if notification_id is not None:
        item = item.filter_by(id=notification_id)
    item = item.order_by(WorkflowNotification.id.desc()).first()
    return invoice_response({"item": delivery_data(item) if item is not None else None})


@invoice_delivery_bp.get("/admin/invoices/<int:invoice_id>/delivery/draft.eml")
@invoice_required
def delivery_draft(invoice_id):
    invoice = final_invoice(invoice_id)
    try:
        content = final_invoice_pdf(invoice)
        if invoice.cover_letter_title is not None:
            from .invoice_correspondence import prepend_cover_letter
            content = prepend_cover_letter(invoice, content)
        return outlook_draft_response(invoice, content)
    except ValueError as exc:
        return invoice_response({"message": str(exc)}, 400)
    except Exception:
        current_app.logger.exception("invoice_mail_draft_failed invoice_id=%s", invoice_id)
        return invoice_response({"message": "Der E-Mail-Entwurf konnte nicht erstellt werden. Bitte den Briefbogen und die Rechnungsdatei prüfen."}, 502)


def outlook_draft_response(invoice, content):
    message = EmailMessage(policy=policy.SMTP)
    message["Subject"] = "Mieterbelastung - Rechnung " + " ".join(invoice.invoice_number.split())
    message["X-Unsent"] = "1"
    if invoice.tenant_billing_reference and invoice.recipient_email:
        message["To"] = validate_debug_email_address(invoice.recipient_email)
    message.set_content("")
    message.add_attachment(content, maintype="application", subtype="pdf", filename=invoice_pdf_filename(invoice))
    return delivery_download_response(
        message.as_bytes(), "message/rfc822",
        secure_filename(f"Mieterbelastung-Rechnung-{invoice.invoice_number}.eml"),
    )


def delivery_download_response(content, mimetype, filename):
    response = send_file(BytesIO(content), mimetype=mimetype, download_name=filename, as_attachment=True)
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def completion_package(invoice, original):
    if not invoice.tenant_billing_reference or invoice.invoice_kind == InvoiceKind.CANCELLATION.value:
        from .invoice_correspondence import prepend_cover_letter

        return prepend_cover_letter(invoice, original)
    from .tenant_charge_letter import tenant_charge_package, tenant_letter_payment

    letter = invoice.tenant_charge_letter
    if not letter:
        raise ValueError("Bitte speichern Sie zuerst das Mieteranschreiben.")
    company = db.get_or_404(WowiCompanyCode, invoice.tenant_billing_reference["company_code_id"])
    if letter["payment"] != tenant_letter_payment(invoice, company, WowiportService()):
        raise ValueError("Die Zahlungsdaten haben sich geändert. Bitte prüfen und speichern Sie das Mieteranschreiben erneut.")
    return tenant_charge_package(invoice, company, letter, invoice_pdf=original)


def completion_notification_prefix(invoice):
    return f"invoice-mail:{invoice.id}:completion:"


def pending_invoice_completion(invoice):
    if invoice.status != "finalized" or invoice.completed_at:
        return None
    items = WorkflowNotification.query.filter(
        WorkflowNotification.notification_key.startswith(completion_notification_prefix(invoice)),
        WorkflowNotification.state.in_(("pending", "sending", "failed", "uncertain")),
    ).order_by(WorkflowNotification.id.desc())
    return next((item for item in items if delivery_matches_invoice(item, invoice)), None)


def delivery_matches_invoice(item, invoice):
    return bool(invoice is not None and invoice.status == "finalized"
                and invoice.document_sha256 == item.payload.get("document_sha256")
                and invoice.document_filename == item.payload.get("document_filename"))


def invoice_delivery_is_current(item):
    invoice = db.session.get(Invoice, item.payload.get("invoice_id"), populate_existing=True)
    return delivery_matches_invoice(item, invoice) and (
        not item.payload.get("complete_invoice") or invoice.completed_at is None or item.payload.get("mail_sent")
    )


def invoice_delivery_was_redirected(item):
    recipient = item.payload.get("delivery_recipient")
    return bool(recipient and recipient.casefold() != item.payload["recipient"].casefold())


def record_invoice_completion(invoice, mode, completed_at, *, actor_user_id=None):
    if invoice.completed_at is None:
        invoice.completed_at = completed_at
        invoice.completion_mode = mode
        audit("invoice.complete", "invoice", invoice.id, {"mode": mode}, actor_user_id=actor_user_id)


def complete_invoice_after_email(item):
    if not item.payload.get("complete_invoice") or invoice_delivery_was_redirected(item):
        return
    invoice = db.session.get(Invoice, item.payload["invoice_id"], with_for_update=True, populate_existing=True)
    if delivery_matches_invoice(item, invoice):
        record_invoice_completion(
            invoice, "direct", item.sent_at, actor_user_id=item.payload["requested_by_user_id"],
        )


@invoice_delivery_bp.route("/admin/invoices/<int:invoice_id>/delivery/complete", methods=["GET", "POST"])
@invoice_required
def delivery_complete(invoice_id):
    from .invoice_dms_dispatch import dispatch_channel, dispatch_options, existing_dispatch, queue_invoice_dispatch

    downloading = request.method == "POST"
    invoice = final_invoice(invoice_id, lock=downloading)
    invoice_url = url_for("admin.invoice_edit", invoice_id=invoice.id)
    completing_tenant_charge = invoice.tenant_billing_reference and invoice.invoice_kind == InvoiceKind.INVOICE.value
    letter_url = (
        url_for("admin.invoice_tenant_charge", invoice_id=invoice.id, complete=1)
        if completing_tenant_charge else url_for("admin.invoice_complete", invoice_id=invoice.id)
    )
    dms_dispatch = existing_dispatch(invoice.id, lock=downloading)
    if not downloading:
        if invoice.completed_at:
            return redirect(invoice_url)
        if completing_tenant_charge and not invoice.tenant_charge_letter:
            return redirect(letter_url)
        return render_template(
            "admin/invoice_completion_delivery.html", item=invoice, letter_url=letter_url,
            pending_delivery=pending_invoice_completion(invoice),
            smtp_debug_recipient=settings.get(SettingKeys.SMTP_DEBUG_RECIPIENT).strip(),
            dispatch_options=dispatch_options(current_user), dms_dispatch=dms_dispatch,
        )

    mode = request.form.get("mode")
    if mode not in {"outlook", "print", "dms"}:
        return invoice_response({"message": "Bitte einen Versandweg auswählen."}, 400)
    if invoice.completed_at and invoice.completion_mode != mode:
        return invoice_response({"message": "Die Rechnung wurde bereits abgeschlossen."}, 409)
    if pending_invoice_completion(invoice):
        return invoice_response({"message": "Für die Rechnung läuft bereits ein Direktversand. Bitte den Versandstatus prüfen."}, 409)
    try:
        channel = dispatch_channel(mode, "postal" in request.form, "portal" in request.form)
        if mode == "dms" and not channel:
            raise ValueError("Bitte Poststelle oder Mieter-App für diese Rechnung auswählen.")
        if dms_dispatch and dms_dispatch.invoice_dispatch["mode"] == "dms":
            if mode == "dms" and channel == dms_dispatch.invoice_dispatch["channel"]:
                return invoice_response({"state": dms_dispatch.status}, 202)
            return invoice_response({"message": "Für die Rechnung wurde bereits ein Versand über das DMS vorgemerkt."}, 409)
        content = completion_package(invoice, final_invoice_pdf(invoice))
        if channel and not invoice.completed_at:
            queue_invoice_dispatch(invoice, content, mode, channel, current_user)
        if mode == "dms":
            db.session.commit()
            return invoice_response({"state": "pending"}, 202)
        response = (
            outlook_draft_response(invoice, content) if mode == "outlook"
            else delivery_download_response(content, "application/pdf", invoice_pdf_filename(invoice))
        )
        record_invoice_completion(invoice, mode, utc_now())
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return invoice_response({"message": str(exc)}, 400)
    except Exception:
        db.session.rollback()
        current_app.logger.exception("invoice_completion_download_failed invoice_id=%s", invoice_id)
        return invoice_response({"message": "Die Versanddatei konnte nicht erstellt werden. Bitte das Anschreiben und die Rechnungsdatei prüfen."}, 502)
    return response


def attach_invoice_delivery_pdf(message, item):
    invoice = db.session.get(Invoice, item.payload["invoice_id"])
    content = final_invoice_pdf(invoice)
    if item.payload.get("correspondence_filename"):
        from .invoice_correspondence import delivery_correspondence_pdf
        content = delivery_correspondence_pdf(invoice, item.payload)
    message.add_attachment(content, maintype="application", subtype="pdf", filename=invoice_pdf_filename(invoice))
