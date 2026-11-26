from email import policy
from email.parser import BytesParser
import importlib
from io import BytesIO
from html.parser import HTMLParser
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from flask import render_template
from pypdf import PdfReader, PdfWriter
from weasyprint import HTML

import test_invoice_payments as fixtures


class InvoiceDeliveryTests(unittest.TestCase):
    login = fixtures.InvoicePaymentTests.login
    start_patch = fixtures.InvoicePaymentTests.start_patch
    image = fixtures.InvoicePaymentTests.image
    form = fixtures.InvoicePaymentTests.form
    post_order = fixtures.InvoicePaymentTests.post_order
    draft = fixtures.InvoicePaymentTests.draft
    draft_form = fixtures.InvoicePaymentTests.draft_form
    pdf_text = fixtures.InvoicePaymentTests.pdf_text
    complete_draft = fixtures.InvoicePaymentTests.complete_draft

    @classmethod
    def setUpClass(cls):
        fixtures.InvoicePaymentTests.setUpClass.__func__(cls)
        sys.modules.pop(cls.package_name + ".invoice_delivery", None)
        cls.delivery = importlib.import_module(cls.package_name + ".invoice_delivery")
        cls.templates = importlib.import_module(cls.package_name + ".email_templates")
        cls.app.register_blueprint(cls.delivery.invoice_delivery_bp)
        cls.app.register_blueprint(cls.document_articles.document_articles_bp)

    def setUp(self):
        fixtures.InvoicePaymentTests.setUp(self)
        root = Path(self.directory_path.name)
        self.app.config["INVOICE_ATTACHMENT_DIR"] = str(root / "invoice_attachments")
        self.start_patch(patch.object(self.app, "instance_path", str(root / "instance")))
        self.start_patch(patch.object(self.delivery, "WowiportService", return_value=self.remote))
        self.archive = self.start_patch(patch.object(sys.modules[self.package_name + ".dms_service"], "register_generated_pdf", Mock(), create=True))
        self.start_patch(patch.object(importlib.import_module(self.package_name + ".dms_context"), "invoice_context", return_value={}))
        self.official = self.db.session.get(self.models.WowiResponsibleOfficial, 901)
        self.official.person_erp_id = 9901
        self.official.name = "Sachbearbeitung Beispiel"
        self.db.session.commit()
        self.remote.get_responsible_official.return_value = {"Id": 901, "Person": {"Id": 9901, "FirstEmailCommunication": {"Content": "office@example.invalid"}}}

    def pdf(self, text="ANLAGE", pages=1):
        return HTML(string="<html><body>" + "".join(f'<p style="break-before: page">{text} {index + 1}</p>' for index in range(pages)) + "</body></html>").write_pdf()

    def upload(self, invoice, files=None):
        files = files or [(self.pdf(), "anlage.pdf")]
        return self.client.post(f"/admin/invoices/{invoice.id}/attachments", data={"files": [(BytesIO(data), name) for data, name in files]}, content_type="multipart/form-data")

    def rendered_invoice(self, with_attachments=False):
        invoice = self.complete_draft()
        if with_attachments:
            self.assertEqual(self.upload(invoice, [(self.pdf("ANHANG A", 2), "a.pdf"), (self.pdf("ANHANG B"), "b.pdf")]).status_code, 201)
        self.prepare_stationery()
        result = self.invoice_document.create_invoice_document(invoice)
        invoice.status = "finalized"
        invoice.document_filename = result.pdf_storage_name
        invoice.document_sha256 = result.pdf_sha256
        self.db.session.commit()
        return invoice

    def prepare_stationery(self):
        stationery = Path(self.app.instance_path) / "invoice_stationery" / "briefbogen.pdf"
        stationery.parent.mkdir(parents=True, exist_ok=True)
        writer = PdfWriter()
        writer.add_blank_page(width=595.28, height=841.89)
        with stationery.open("wb") as output:
            writer.write(output)
        self.company.invoice_stationery_filename = stationery.name
        self.db.session.commit()

    def queue(self, invoice, token=None, official_id=901, **values):
        return self.client.post(f"/admin/invoices/{invoice.id}/delivery/send", json={"official_id": official_id, "request_id": token or str(uuid4()), **values})

    def test_multiple_attachments_are_ordered_previewed_and_appended_without_saving_the_form(self):
        invoice = self.draft("invoice")
        response = self.upload(invoice, [(self.pdf("ERSTE ANLAGE", 2), "erste.pdf"), (self.pdf("ZWEITE ANLAGE"), "zweite.pdf")])
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual([(item["name"], item["pages"]) for item in response.json["items"]], [("erste.pdf", 2), ("zweite.pdf", 1)])
        preview = self.client.post(f"/admin/invoices/{invoice.id}/preview", data=self.draft_form())
        self.assertEqual(preview.status_code, 200, preview.data[:500])
        pages = PdfReader(BytesIO(preview.data)).pages
        self.assertEqual(len(pages), 4)
        self.assertIn("Ungespeicherte Reparatur", pages[0].extract_text())
        self.assertIn("ERSTE ANLAGE 1", pages[1].extract_text())
        self.assertIn("ERSTE ANLAGE 2", pages[2].extract_text())
        self.assertIn("ZWEITE ANLAGE 1", pages[3].extract_text())
        self.db.session.expire_all()
        self.assertIsNone(invoice.invoice_number)
        self.assertEqual(invoice.recipient_name, "Gespeicherter Empfänger")
        self.assertEqual(self.models.InvoiceAttachment.query.count(), 2)

    def test_attachment_count_is_not_limited_and_invalid_batches_leave_no_files_or_rows(self):
        invoice = self.draft("invoice")
        raw = self.pdf()
        response = self.upload(invoice, [(raw, f"anlage-{index}.pdf") for index in range(35)])
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(response.json["items"]), 35)
        other = self.draft("invoice")
        response = self.upload(other, [(raw, "gueltig.pdf"), (b"Keine PDF", "defekt.pdf")])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.models.InvoiceAttachment.query.filter_by(invoice_id=other.id).count(), 0)
        self.assertEqual(len(list(Path(self.app.config["INVOICE_ATTACHMENT_DIR"]).rglob("*.pdf"))), 35)

    def test_encrypted_empty_oversized_and_non_pdf_files_are_rejected(self):
        invoice = self.draft("invoice")
        empty = BytesIO()
        PdfWriter().write(empty)
        encrypted = PdfWriter()
        encrypted.add_blank_page(width=100, height=100)
        encrypted.encrypt("passwort")
        protected = BytesIO()
        encrypted.write(protected)
        for raw, name in ((empty.getvalue(), "leer.pdf"), (protected.getvalue(), "geschuetzt.pdf"), (self.pdf(), "datei.txt")):
            with self.subTest(name=name):
                self.assertEqual(self.upload(invoice, [(raw, name)]).status_code, 400)
        with patch.object(self.invoice_attachments, "MAX_INVOICE_ATTACHMENT_BYTES", 10):
            self.assertEqual(self.upload(invoice).status_code, 400)
        self.assertEqual(self.models.InvoiceAttachment.query.count(), 0)

    def test_removing_an_attachment_deletes_its_file_and_checks_the_invoice_boundary(self):
        invoice = self.draft("invoice")
        self.upload(invoice)
        attachment = self.models.InvoiceAttachment.query.one()
        path = self.invoice_attachments.invoice_attachment_path(attachment)
        other = self.draft("invoice")
        self.assertEqual(self.client.delete(f"/admin/invoices/{other.id}/attachments/{attachment.id}").status_code, 404)
        self.assertEqual(self.client.get(f"/admin/invoices/{other.id}/attachments/{attachment.id}").status_code, 404)
        response = self.client.get(f"/admin/invoices/{invoice.id}/attachments/{attachment.id}")
        self.assertEqual(response.status_code, 200)
        response.close()
        self.assertEqual(self.client.delete(f"/admin/invoices/{invoice.id}/attachments/{attachment.id}").status_code, 200)
        self.assertFalse(path.exists())
        self.assertEqual(self.models.InvoiceAttachment.query.count(), 0)

    def test_finalized_attachments_are_readable_and_immutable_and_require_invoice_permissions(self):
        invoice = self.rendered_invoice(with_attachments=True)
        attachment = invoice.pdf_attachments[0]
        self.assertEqual(self.upload(invoice).status_code, 409)
        self.assertEqual(self.client.delete(f"/admin/invoices/{invoice.id}/attachments/{attachment.id}").status_code, 409)
        response = self.client.get(f"/admin/invoices/{invoice.id}/attachments/{attachment.id}")
        self.assertEqual(response.status_code, 200)
        response.close()
        self.creator.can_manage_invoices = False
        self.db.session.commit()
        self.login(self.creator)
        for path in (f"/admin/invoices/{invoice.id}/attachments", f"/admin/invoices/{invoice.id}/delivery/draft.eml", f"/admin/invoices/{invoice.id}/delivery/recipients"):
            self.assertEqual(self.client.get(path).status_code, 403)
        self.assertEqual(self.models.InvoiceAttachment.query.count(), 2)

    def test_final_pdf_contains_all_pages_and_embedded_zugferd_xml(self):
        invoice = self.rendered_invoice(with_attachments=True)
        data = self.delivery.final_invoice_pdf(invoice)
        reader = PdfReader(BytesIO(data))
        self.assertEqual(len(reader.pages), 4)
        self.assertIn("ANHANG A 1", reader.pages[1].extract_text())
        self.assertIn("ANHANG B 1", reader.pages[3].extract_text())
        xml = reader.attachments["factur-x.xml"][0]
        self.assertIn(invoice.invoice_number.encode(), xml)
        self.assertIn(b"TypeCode>58<", xml)
        self.assertEqual(self.archive.call_args.args[4], data)

    def test_corrupt_attachments_prevent_finalization_instead_of_silently_omitting_pages(self):
        invoice = self.complete_draft()
        invoice.invoice_number = None
        self.db.session.commit()
        self.prepare_stationery()
        self.upload(invoice)
        attachment = self.models.InvoiceAttachment.query.one()
        self.invoice_attachments.invoice_attachment_path(attachment).write_bytes(self.pdf("VERAENDERT"))
        response = self.client.post(f"/admin/invoices/{invoice.id}/preview", data=self.draft_form())
        self.assertEqual(response.status_code, 400)
        self.assertIn("verändert", response.json["error"])
        self.assertEqual(self.client.post(f"/admin/invoices/{invoice.id}/finalize").status_code, 302)
        self.db.session.expire_all()
        self.assertEqual(invoice.status, "draft")
        self.assertIsNone(invoice.invoice_number)
        self.assertIsNone(invoice.document_filename)

    def test_finalization_persists_the_complete_pdf_and_prevents_further_uploads(self):
        invoice = self.complete_draft()
        invoice.invoice_number = None
        self.db.session.commit()
        self.prepare_stationery()
        self.assertEqual(self.upload(invoice).status_code, 201)
        response = self.client.post(f"/admin/invoices/{invoice.id}/finalize")
        self.assertEqual(response.status_code, 302)
        self.db.session.expire_all()
        self.assertEqual(invoice.status, "finalized")
        reader = PdfReader(BytesIO(self.delivery.final_invoice_pdf(invoice)))
        self.assertEqual(len(reader.pages), 2)
        self.assertIn("ANLAGE", reader.pages[1].extract_text())
        self.assertIn("factur-x.xml", reader.attachments)
        self.assertEqual(self.upload(invoice).status_code, 409)

    def test_deleting_a_draft_removes_its_attachment_files(self):
        invoice = self.draft("invoice")
        self.upload(invoice)
        path = self.invoice_attachments.invoice_attachment_path(invoice.pdf_attachments[0])
        self.assertEqual(self.client.post(f"/admin/invoices/{invoice.id}/delete").status_code, 302)
        self.assertFalse(path.exists())
        self.assertEqual(self.models.InvoiceAttachment.query.count(), 0)

    def test_desktop_mail_draft_is_empty_has_no_recipient_and_contains_the_complete_invoice(self):
        invoice = self.rendered_invoice(with_attachments=True)
        response = self.client.get(f"/admin/invoices/{invoice.id}/delivery/draft.eml")
        self.assertEqual(response.status_code, 200, response.data[:500])
        self.assertEqual(response.mimetype, "message/rfc822")
        self.assertIn("no-store", response.headers["Cache-Control"])
        message = BytesParser(policy=policy.default).parsebytes(response.data)
        self.assertEqual(str(message["Subject"]), f"Mieterbelastung - Rechnung {invoice.invoice_number}")
        self.assertEqual(message["X-Unsent"], "1")
        self.assertIsNone(message["To"])
        self.assertEqual(message.get_body(preferencelist=("plain",)).get_content().strip(), "")
        attachments = list(message.iter_attachments())
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].get_content(), self.delivery.final_invoice_pdf(invoice))
        self.assertEqual(self.models.WorkflowNotification.query.count(), 0)
        self.mail.assert_not_called()

    def test_direct_delivery_uses_the_template_official_address_and_idempotent_request(self):
        invoice = self.rendered_invoice(with_attachments=True)
        definition = self.templates.email_template_definition("invoice_tenant_charge")
        self.settings.settings.set(definition, {"subject": "Belastung {{ invoice_number }}", "body": "An {{ recipient_name }}: {{ invoice_recipient_name }} – {{ total_gross }} {{ currency }}"})
        self.settings.settings.set(self.settings.SettingKeys.SMTP_SENDER, "Testbetrieb <app@example.invalid>")
        self.db.session.commit()
        preview = self.client.get(f"/admin/invoices/{invoice.id}/delivery/preview?official_id=901")
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertEqual(preview.json["recipient"], "office@example.invalid")
        self.assertEqual(preview.json["subject"], f"Belastung {invoice.invoice_number}")
        self.assertIn("11,90 EUR", preview.json["body"])
        token = str(uuid4())
        queued = self.queue(invoice, token)
        self.assertEqual(queued.status_code, 202, queued.data)
        self.assertEqual(self.queue(invoice, token).status_code, 200)
        self.assertEqual(self.models.WorkflowNotification.query.count(), 1)
        self.assertEqual(self.workflow.process_workflow_notification_batch(limit=1), 1)
        message = self.mail.call_args.args[0]
        self.assertEqual(str(message["From"]), "Testbetrieb <app@example.invalid>")
        self.assertEqual(str(message["To"]), "office@example.invalid")
        self.assertEqual(str(message["Subject"]), preview.json["subject"])
        self.assertIn("11,90 EUR", message.get_body(preferencelist=("plain",)).get_content())
        self.assertEqual(list(message.iter_attachments())[0].get_content(), self.delivery.final_invoice_pdf(invoice))
        status = self.client.get(f"/admin/invoices/{invoice.id}/delivery/status").json["item"]
        self.assertEqual(status["state"], "sent")
        self.assertEqual(self.models.AuditLog.query.filter_by(action="invoice.mail_sent").count(), 1)
        self.assertEqual(self.workflow.process_workflow_notification_batch(limit=1), 0)
        self.mail.assert_called_once()

    def test_delivery_requires_finalization_a_valid_official_and_a_matching_invoice_file(self):
        draft = self.draft("invoice")
        self.assertEqual(self.queue(draft).status_code, 409)
        invoice = self.rendered_invoice()
        self.assertEqual(self.queue(invoice, official_id=999).status_code, 400)
        self.official.active = False
        self.db.session.commit()
        self.assertEqual(self.queue(invoice).status_code, 400)
        self.official.active = True
        self.db.session.commit()
        self.remote.get_responsible_official.return_value["Id"] = 999
        self.assertEqual(self.queue(invoice).status_code, 400)
        self.remote.get_responsible_official.return_value["Id"] = 901
        path = self.delivery.resolve_invoice_document_path(invoice.document_filename)
        path.write_bytes(self.pdf("ANDERE RECHNUNG"))
        self.assertEqual(self.queue(invoice).status_code, 400)
        self.assertEqual(self.models.WorkflowNotification.query.count(), 0)
        self.mail.assert_not_called()

    def test_edited_mail_body_is_sent_literally_with_the_invoice_without_changing_the_template(self):
        invoice = self.rendered_invoice(with_attachments=True)
        preview_url = f"/admin/invoices/{invoice.id}/delivery/preview?official_id=901"
        preview = self.client.get(preview_url).json
        template = self.templates.get_email_template("invoice_tenant_charge")
        body = "Guten Tag,\r\n\r\nBitte mit Vorgang 42 abgleichen.\r\n<b>Zusatzinfo</b> & {{ invoice_number }}\r\n"
        normalized = body.replace("\r\n", "\n")
        queued = self.queue(invoice, body=body)
        self.assertEqual(queued.status_code, 202, queued.data)
        notification = self.db.session.get(self.models.WorkflowNotification, queued.json["id"])
        self.assertEqual(notification.payload["body"], normalized)
        self.assertEqual(notification.payload["subject"], preview["subject"])
        self.assertEqual(self.templates.get_email_template("invoice_tenant_charge"), template)
        self.assertEqual(self.client.get(preview_url).json["body"], preview["body"])
        self.settings.settings.set(self.templates.email_template_definition("invoice_tenant_charge"), {"subject": "Neuer Standardbetreff", "body": "Neue Standardnachricht"})
        self.db.session.commit()
        self.assertEqual(self.workflow.process_workflow_notification_batch(limit=1), 1)
        message = self.mail.call_args.args[0]
        self.assertEqual(str(message["Subject"]), preview["subject"])
        self.assertEqual(message.get_body(preferencelist=("plain",)).get_content(), normalized)
        html = message.get_body(preferencelist=("html",)).get_content()
        self.assertIn("&lt;b&gt;Zusatzinfo&lt;/b&gt; &amp; {{ invoice_number }}", html)
        self.assertNotIn("<b>Zusatzinfo</b>", html)
        self.assertEqual(list(message.iter_attachments())[0].get_content(), self.delivery.final_invoice_pdf(invoice))
        self.mail.assert_called_once()

    def test_mail_body_validation_rejects_invalid_values_before_queuing(self):
        invoice = self.rendered_invoice()
        for body in (None, False, 42, [], {}, "x" * 250001, "Text\x00Zusatz"):
            with self.subTest(body_type=type(body).__name__):
                response = self.queue(invoice, body=body)
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn("Mailtext", response.json["message"])
        self.assertEqual(self.models.WorkflowNotification.query.count(), 0)
        self.remote.get_responsible_official.assert_not_called()
        self.mail.assert_not_called()

    def test_explicit_empty_mail_body_is_not_replaced_by_the_template(self):
        invoice = self.rendered_invoice()
        queued = self.queue(invoice, body="")
        self.assertEqual(queued.status_code, 202, queued.data)
        self.assertEqual(self.db.session.get(self.models.WorkflowNotification, queued.json["id"]).payload["body"], "")
        self.assertEqual(self.workflow.process_workflow_notification_batch(limit=1), 1)
        self.assertEqual(self.mail.call_args.args[0].get_body(preferencelist=("plain",)).get_content().strip(), "")

    def test_edited_body_retries_keep_the_original_message_and_reject_different_content(self):
        invoice = self.rendered_invoice()
        token = str(uuid4())
        first = self.queue(invoice, token, body="Individuelle Hinweise\r\nZweite Zeile")
        self.assertEqual(first.status_code, 202, first.data)
        retry = self.queue(invoice, token, body="Individuelle Hinweise\nZweite Zeile")
        self.assertEqual(retry.status_code, 200, retry.data)
        self.assertEqual(retry.json["id"], first.json["id"])
        changed = self.queue(invoice, token, body="Nachträgliche Änderung")
        self.assertEqual(changed.status_code, 400, changed.data)
        self.assertIn("anderen Text", changed.json["message"])
        item = self.db.session.get(self.models.WorkflowNotification, first.json["id"])
        with patch.object(self.delivery, "enqueue_notification", return_value=item):
            conflict = self.queue(invoice, body="Gleichzeitig anderer Text")
        self.assertEqual(conflict.status_code, 400, conflict.data)
        self.assertEqual(self.models.WorkflowNotification.query.count(), 1)
        self.assertEqual(item.payload["body"], "Individuelle Hinweise\nZweite Zeile")
        self.assertEqual(self.models.AuditLog.query.filter_by(action="invoice.mail_queued").count(), 1)
        self.mail.assert_not_called()

    def test_a_queued_invoice_is_not_sent_after_its_document_changes(self):
        invoice = self.rendered_invoice()
        self.assertEqual(self.queue(invoice).status_code, 202)
        invoice.document_sha256 = "f" * 64
        self.db.session.commit()
        self.workflow.process_workflow_notification_batch(limit=1)
        self.assertEqual(self.models.WorkflowNotification.query.one().state, "discarded")
        self.mail.assert_not_called()

    def test_uncertain_smtp_results_are_visible_and_are_not_sent_twice(self):
        invoice = self.rendered_invoice()
        self.queue(invoice)
        self.mail.side_effect = RuntimeError("SMTP-Ergebnis unklar")
        with self.assertLogs(self.app.logger, level="ERROR"):
            self.workflow.process_workflow_notification_batch(limit=1)
        status = self.client.get(f"/admin/invoices/{invoice.id}/delivery/status").json["item"]
        self.assertEqual(status["state"], "uncertain")
        self.assertIn("SMTP-Ergebnis unklar", status["message"])
        self.workflow.process_workflow_notification_batch(limit=1)
        self.mail.assert_called_once()

    def test_wizard_direct_delivery_closes_the_invoice_after_smtp_and_blocks_duplicate_dispatch(self):
        invoice = self.rendered_invoice()
        self.assertEqual(self.queue(invoice, complete_invoice=True).status_code, 202)
        self.db.session.refresh(invoice)
        self.assertIsNone(invoice.completed_at)
        self.assertEqual(self.queue(invoice, complete_invoice=True).status_code, 409)
        url = f"/admin/invoices/{invoice.id}/delivery/complete"
        self.assertEqual(self.client.post(url, data={"mode": "print"}).status_code, 409)
        self.workflow.process_workflow_notification_batch(limit=1)
        self.db.session.refresh(invoice)
        self.assertIsNotNone(invoice.completed_at)
        self.assertEqual(invoice.completion_mode, "direct")
        self.assertEqual(self.models.AuditLog.query.filter_by(action="invoice.complete").count(), 1)
        self.assertEqual(self.queue(invoice, complete_invoice=True).status_code, 409)
        self.mail.assert_called_once()

    def test_wizard_debug_delivery_keeps_the_invoice_open(self):
        invoice = self.rendered_invoice()
        self.mail.return_value = "debug@example.invalid"
        self.assertEqual(self.queue(invoice, complete_invoice=True).status_code, 202)
        self.workflow.process_workflow_notification_batch(limit=1)
        self.db.session.refresh(invoice)
        self.assertIsNone(invoice.completed_at)
        self.assertTrue(self.client.get(f"/admin/invoices/{invoice.id}/delivery/status?complete=1").json["item"]["test_delivery"])

    def test_wizard_outlook_download_finishes_with_the_letter_and_original_attachments(self):
        invoice = self.rendered_invoice(with_attachments=True)
        invoice.cover_letter_title = "Ausfertigung für Outlook"
        invoice.cover_letter_text = "Der Versand erfolgt über Outlook."
        self.db.session.commit()
        url = f"/admin/invoices/{invoice.id}/delivery/complete"
        response = self.client.post(url, data={"mode": "outlook"})
        self.assertEqual(response.status_code, 200, response.data[:500])
        message = BytesParser(policy=policy.default).parsebytes(response.data)
        self.assertEqual(message["X-Unsent"], "1")
        reader = PdfReader(BytesIO(next(message.iter_attachments()).get_content()))
        self.assertEqual(len(reader.pages), 5)
        self.assertIn("Ausfertigung für Outlook", reader.pages[0].extract_text())
        self.assertIn("ANHANG B", reader.pages[-1].extract_text())
        self.assertIn("factur-x.xml", reader.attachments)
        self.db.session.refresh(invoice)
        self.assertEqual(invoice.completion_mode, "outlook")
        self.assertEqual(self.client.post(url, data={"mode": "outlook"}).status_code, 200)
        self.assertEqual(self.models.AuditLog.query.filter_by(action="invoice.complete").count(), 1)
        self.mail.assert_not_called()

    def test_invoice_editor_renders_attachment_and_mail_controls_without_nested_forms(self):
        invoice = self.rendered_invoice()
        with self.app.test_request_context():
            html = render_template("admin/invoice_draft.html", item=invoice, editable=False, task_templates=[], craft_activities=[], catalogue_items=[], position_source_labels={}, work_report_counts={}, has_billed_work_reports=False, has_any_work_reports=False)
        self.assertIn("Rechnung abschließen und versenden", html)
        self.assertIn("Rechnungs-PDF aufrufen", html)
        self.assertIn('data-bs-target="#invoiceCancelModal"', html)
        self.assertNotIn("PDF neu erzeugen", html)
        self.assertNotIn("In d.velop archivieren", html)
        self.assertNotIn('id="invoiceAttachmentForm"', html)
        delivery = self.client.get(f"/admin/invoices/{invoice.id}/delivery/complete").get_data(as_text=True)
        self.assertIn("Direktversand", delivery)
        self.assertIn("Versand per Outlook", delivery)
        self.assertIn("Kein Versand (nur Druck)", delivery)
        self.assertIn('<textarea class="form-control mb-2" id="invoiceDeliveryBody"', delivery)
        self.assertIn("Die gespeicherte Vorlage bleibt unverändert.", delivery)
        invoice.status = "draft"
        with self.app.test_request_context():
            html = render_template("admin/invoice_draft.html", item=invoice, editable=True, task_templates=[], craft_activities=[], catalogue_items=[], position_source_labels={}, work_report_counts={}, has_billed_work_reports=False, has_any_work_reports=False)
        self.assertIn('id="invoiceAttachmentForm"', html)
        self.assertNotIn('id="invoiceDeliveryModal"', html)
        class FormParser(HTMLParser):
            depth = 0
            maximum_depth = 0

            def handle_starttag(self, tag, attributes):
                if tag == "form":
                    self.depth += 1
                    self.maximum_depth = max(self.maximum_depth, self.depth)

            def handle_endtag(self, tag):
                if tag == "form":
                    self.depth -= 1

        parser = FormParser()
        parser.feed(html)
        self.assertEqual(parser.maximum_depth, 1)
        self.assertEqual(parser.depth, 0)
