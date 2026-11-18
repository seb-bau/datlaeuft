from email import policy
from email.message import EmailMessage
from hashlib import sha256
from io import BytesIO
from uuid import UUID

from flask import Blueprint, abort, current_app, jsonify, request, send_file
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
from .models import Invoice, InvoiceAttachment, WorkflowNotification, WowiResponsibleOfficial
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


def final_invoice(invoice_id):
    invoice = db.get_or_404(Invoice, invoice_id)
    if invoice.status != "finalized" or not invoice.invoice_number or not invoice.document_filename or not invoice.document_sha256:
        abort(invoice_response({"message": "Bitte die Rechnung vor dem Versand festschreiben."}, 409))
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


def invoice_mail_content(invoice, official):
    return render_email_template("invoice_tenant_charge", {
        "invoice_number": invoice.invoice_number, "invoice_recipient_name": invoice.recipient_name or "",
        "contract_number": invoice.recipient_license_agreement_idnum or "", "recipient_name": official.name or "",
        "total_gross": f"{invoice.total_gross:,.2f}".replace(",", "_").replace(".", ",").replace("_", "."),
        "currency": invoice.currency_code, "invoice_date": invoice.invoice_date.strftime("%d.%m.%Y") if invoice.invoice_date else "",
        "due_date": invoice.due_date.strftime("%d.%m.%Y") if invoice.due_date else "",
        "company_name": invoice.company_code.name or invoice.company_code_code, "sender_name": current_user.display_name,
    })


@invoice_delivery_bp.get("/admin/invoices/<int:invoice_id>/delivery/recipients")
@invoice_required
def delivery_recipients(invoice_id):
    final_invoice(invoice_id)
    query = WowiResponsibleOfficial.query.filter_by(active=True).filter(~WowiResponsibleOfficial.erp_id.in_(blacklist_ids("official")))
    items = [item for item in query.order_by(WowiResponsibleOfficial.name, WowiResponsibleOfficial.erp_id).all() if not is_blacklisted("person", item.person_erp_id)]
    return invoice_response({"items": [{"id": item.erp_id, "name": item.name or f"Sachbearbeiter {item.erp_id}"} for item in items]})


@invoice_delivery_bp.get("/admin/invoices/<int:invoice_id>/delivery/preview")
@invoice_required
def delivery_preview(invoice_id):
    invoice = final_invoice(invoice_id)
    try:
        official, recipient = selected_official(request.args.get("official_id"))
        content = invoice_mail_content(invoice, official)
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
    invoice = final_invoice(invoice_id)
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return invoice_response({"message": "Bitte gültige Versandangaben übermitteln."}, 400)
    try:
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
        key = f"invoice-mail:{invoice.id}:{token}"
        existing = WorkflowNotification.query.filter_by(notification_key=key).one_or_none()
        if existing is not None:
            validate_delivery_retry(existing, data.get("official_id"), body)
            return invoice_response(delivery_data(existing), 200)
        if not settings.get(SettingKeys.SMTP_HOST).strip() or not settings.get(SettingKeys.SMTP_SENDER).strip():
            raise ValueError("SMTP-Server oder Absenderadresse fehlen. Bitte die E-Mail-Einstellungen ergänzen.")
        final_invoice_pdf(invoice)
        official, recipient = selected_official(data.get("official_id"))
        content = invoice_mail_content(invoice, official)
        if body is not None:
            content["body"] = body
        item = enqueue_notification(key, "invoice_delivery", payload={
            "invoice_id": invoice.id, "invoice_number": invoice.invoice_number,
            "document_filename": invoice.document_filename, "document_sha256": invoice.document_sha256,
            "official_id": official.erp_id, "recipient": recipient, "requested_by_user_id": current_user.id, **content,
        })
        validate_delivery_retry(item, data.get("official_id"), body)
        audit("invoice.mail_queued", "invoice", invoice.id, {"notification_id": item.id, "recipient": recipient, "official_id": official.erp_id})
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return invoice_response({"message": str(exc) or "Die Versandkennung ist ungültig. Bitte den Dialog erneut öffnen."}, 400)
    except Exception:
        db.session.rollback()
        current_app.logger.exception("invoice_mail_queue_failed invoice_id=%s", invoice_id)
        return invoice_response({"message": "Die Rechnung konnte nicht zum Versand vorgemerkt werden."}, 502)
    return invoice_response(delivery_data(item), 202)


def delivery_data(item):
    return {"id": item.id, "state": item.state, "recipient": item.payload.get("delivery_recipient") or item.payload.get("recipient"),
            "message": item.last_error if item.state in {"failed", "uncertain"} else "", "sent_at": item.sent_at.isoformat() if item.sent_at else None}


@invoice_delivery_bp.get("/admin/invoices/<int:invoice_id>/delivery/status")
@invoice_required
def delivery_status(invoice_id):
    db.get_or_404(Invoice, invoice_id)
    item = WorkflowNotification.query.filter(WorkflowNotification.notification_key.startswith(f"invoice-mail:{invoice_id}:"))
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
        message = EmailMessage(policy=policy.SMTP)
        message["Subject"] = "Mieterbelastung - Rechnung " + " ".join(invoice.invoice_number.split())
        message["X-Unsent"] = "1"
        message.set_content("")
        message.add_attachment(final_invoice_pdf(invoice), maintype="application", subtype="pdf", filename=invoice_pdf_filename(invoice))
    except ValueError as exc:
        return invoice_response({"message": str(exc)}, 400)
    response = send_file(BytesIO(message.as_bytes()), mimetype="message/rfc822", as_attachment=True,
                         download_name=secure_filename(f"Mieterbelastung-Rechnung-{invoice.invoice_number}.eml"))
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def invoice_delivery_is_current(item):
    invoice = db.session.get(Invoice, item.payload.get("invoice_id"), populate_existing=True)
    return bool(invoice is not None and invoice.status == "finalized" and invoice.document_sha256 == item.payload.get("document_sha256")
                and invoice.document_filename == item.payload.get("document_filename"))


def attach_invoice_delivery_pdf(message, item):
    invoice = db.session.get(Invoice, item.payload["invoice_id"])
    message.add_attachment(final_invoice_pdf(invoice), maintype="application", subtype="pdf", filename=invoice_pdf_filename(invoice))
