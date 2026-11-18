from email import policy
from email.parser import BytesParser
import ast
from functools import wraps
import importlib
from io import BytesIO
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from flask import abort, redirect, render_template, request, url_for
from flask_login import current_user
from pypdf import PdfReader
from werkzeug.datastructures import FileStorage

import test_invoice_delivery as fixtures


class InvoiceWorkflowTests(unittest.TestCase):
    login = fixtures.InvoiceDeliveryTests.login
    start_patch = fixtures.InvoiceDeliveryTests.start_patch
    image = fixtures.InvoiceDeliveryTests.image
    form = fixtures.InvoiceDeliveryTests.form
    post_order = fixtures.InvoiceDeliveryTests.post_order
    draft = fixtures.InvoiceDeliveryTests.draft
    draft_form = fixtures.InvoiceDeliveryTests.draft_form
    pdf_text = fixtures.InvoiceDeliveryTests.pdf_text
    complete_draft = fixtures.InvoiceDeliveryTests.complete_draft
    pdf = fixtures.InvoiceDeliveryTests.pdf
    upload = fixtures.InvoiceDeliveryTests.upload
    queue = fixtures.InvoiceDeliveryTests.queue
    rendered_invoice = fixtures.InvoiceDeliveryTests.rendered_invoice
    prepare_stationery = fixtures.InvoiceDeliveryTests.prepare_stationery

    @classmethod
    def setUpClass(cls):
        fixtures.InvoiceDeliveryTests.setUpClass.__func__(cls)
        cls.tables.append(cls.models.InvoiceBillingRule.__table__)
        for name in ("invoice_responsibility", "invoice_correspondence"):
            sys.modules.pop(cls.package_name + "." + name, None)
            setattr(cls, name, importlib.import_module(cls.package_name + "." + name))
        guard = sys.modules[cls.package_name + ".auth"].invoice_required
        for endpoint in ("invoices", "invoice_edit", "invoice_responsibility", "invoice_cover_letter", "invoice_cover_letter_preview", "invoice_correspondence"):
            view = cls.app.view_functions["admin." + endpoint]
            cls.app.view_functions["admin." + endpoint] = guard(view.__wrapped__)
        source = ast.parse((cls.root / "app/auth.py").read_text())
        function = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == "admin_required")
        namespace = dict(F=object, Any=object, wraps=wraps, current_user=current_user, abort=abort, redirect=redirect, request=request, url_for=url_for)
        exec(compile(ast.Module(body=[function], type_ignores=[]), "auth.py", "exec"), namespace)
        for endpoint in ("invoice_cover_letter_layout", "invoice_cover_letter_layout_preview", "settings_page"):
            view = cls.app.view_functions["admin." + endpoint]
            cls.app.view_functions["admin." + endpoint] = namespace["admin_required"](view.__wrapped__)

    def setUp(self):
        fixtures.InvoiceDeliveryTests.setUp(self)
        self.reviewer.can_manage_invoices = True
        self.creator.phone = "02306 123456"
        self.db.session.commit()

    def assign(self, invoice, user=None, revision=None):
        return self.client.post(f"/admin/invoices/{invoice.id}/responsibility", data={
            "responsible_user_id": (user or self.reviewer).id,
            "revision": invoice.responsibility_revision if revision is None else revision,
        })

    def save_letter(self, invoice, title="Rechnung {invoice_number}", text="Guten Tag,\n\n{amount} {currency}.\n{payment_terms}"):
        return self.client.post(f"/admin/invoices/{invoice.id}/cover-letter", data={"title": title, "text": text})

    def test_new_invoices_default_to_the_creator_without_sending_a_notification(self):
        invoice = self.draft("invoice")
        self.assertEqual(invoice.responsible_user_id, self.creator.id)
        self.assertEqual(invoice.responsible_user, self.creator)
        free = self.invoice_service.create_free_invoice_draft(self.company, self.creator)
        self.db.session.commit()
        self.assertEqual(free.responsible_user_id, self.creator.id)
        self.assertEqual(self.models.WorkflowNotification.query.count(), 0)

    def test_invoice_filters_combine_creator_responsibility_and_status(self):
        own = self.draft("invoice")
        own.recipient_name = "Eigene Rechnung"
        delegated = self.draft("invoice")
        delegated.recipient_name = "Delegierte Rechnung"
        delegated.responsible_user = self.reviewer
        foreign = self.draft("invoice")
        foreign.created_by = self.reviewer
        foreign.recipient_name = "Fremd angelegt"
        foreign.status = "finalized"
        self.db.session.commit()
        response = self.client.get("/admin/invoices")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Eigene Rechnung", response.data)
        self.assertIn(b"Fremd angelegt", response.data)
        self.assertNotIn(b"Delegierte Rechnung", response.data)
        response = self.client.get(f"/admin/invoices?responsible=mine&creator={self.reviewer.id}&status=finalized")
        self.assertIn(b"Fremd angelegt", response.data)
        self.assertNotIn(b"Eigene Rechnung", response.data)
        response = self.client.get(f"/admin/invoices?responsible={self.reviewer.id}&creator=mine&status=draft")
        self.assertIn(b"Delegierte Rechnung", response.data)
        self.assertNotIn(b"Fremd angelegt", response.data)
        response = self.client.get("/admin/invoices?responsible=all")
        self.assertTrue(all(name in response.data for name in (b"Eigene Rechnung", b"Delegierte Rechnung", b"Fremd angelegt")))

    def test_cancellation_starts_with_its_creator_and_without_the_original_letter(self):
        invoice = self.rendered_invoice()
        invoice.responsible_user = self.administrator
        self.assertEqual(self.save_letter(invoice).status_code, 200)
        cancellation, result = self.invoice_service.create_cancellation_invoice(invoice, "Korrektur", self.reviewer)
        self.db.session.commit()
        self.assertEqual(cancellation.created_by_user_id, self.reviewer.id)
        self.assertEqual(cancellation.responsible_user_id, self.reviewer.id)
        self.assertIsNone(cancellation.cover_letter_title)
        self.assertIsNone(cancellation.cover_letter_text)
        self.assertEqual(invoice.responsible_user_id, self.administrator.id)

    def test_assignment_queues_one_email_and_keeps_the_creator(self):
        invoice = self.draft("invoice")
        self.assertEqual(self.assign(invoice).status_code, 302)
        self.db.session.expire_all()
        self.assertEqual(invoice.responsible_user_id, self.reviewer.id)
        self.assertEqual(invoice.created_by_user_id, self.creator.id)
        self.assertEqual(invoice.responsibility_revision, 1)
        self.assertEqual(self.models.WorkflowNotification.query.count(), 1)
        self.assign(invoice)
        self.assertEqual(self.models.WorkflowNotification.query.count(), 1)
        self.assertEqual(self.workflow.process_workflow_notification_batch(), 1)
        self.assertEqual(self.mail.call_args.args[0]["To"], self.reviewer.email)
        self.assertIn(f"/admin/invoices/{invoice.id}", self.mail.call_args.args[0].get_body(preferencelist=("plain",)).get_content())
        self.assertEqual(self.models.WorkflowNotification.query.one().state, "sent")

    def test_stale_changes_and_unavailable_users_do_not_overwrite_assignments(self):
        invoice = self.draft("invoice")
        self.assign(invoice)
        self.assign(invoice, self.creator, revision=0)
        self.db.session.expire_all()
        self.assertEqual(invoice.responsible_user_id, self.reviewer.id)
        for user in (self.craftsman, self.administrator):
            user.is_active_flag = False
            self.db.session.commit()
            self.assign(invoice, user)
            self.db.session.expire_all()
            self.assertEqual(invoice.responsible_user_id, self.reviewer.id)
        self.assertEqual(self.models.WorkflowNotification.query.count(), 1)

    def test_superseded_assignment_emails_are_discarded(self):
        invoice = self.draft("invoice")
        self.assign(invoice)
        self.assign(invoice, self.creator)
        self.workflow.process_workflow_notification_batch()
        items = self.models.WorkflowNotification.query.order_by(self.models.WorkflowNotification.id).all()
        self.assertEqual([item.state for item in items], ["discarded", "sent"])
        self.assertEqual(self.mail.call_args.args[0]["To"], self.creator.email)

    def test_finalized_assignments_and_letters_leave_original_document_unchanged(self):
        invoice = self.rendered_invoice(with_attachments=True)
        original = self.delivery.final_invoice_pdf(invoice)
        digest = invoice.document_sha256
        self.assign(invoice)
        self.assertEqual(self.save_letter(invoice).status_code, 200)
        response = self.client.get(f"/admin/invoices/{invoice.id}/correspondence")
        self.assertEqual(response.status_code, 200)
        reader = PdfReader(BytesIO(response.data))
        self.assertEqual(len(reader.pages), 5)
        first = reader.pages[0].extract_text()
        self.assertIn(invoice.invoice_number, first)
        self.assertIn("11,90 EUR", first)
        self.assertIn(self.creator.display_name, first)
        self.assertIn(self.creator.phone, first)
        self.assertIn(self.creator.email, first)
        self.assertIn("ANHANG A 1", reader.pages[2].extract_text())
        self.assertEqual(reader.attachments["factur-x.xml"][0], PdfReader(BytesIO(original)).attachments["factur-x.xml"][0])
        self.assertEqual(self.delivery.final_invoice_pdf(invoice), original)
        self.assertEqual(invoice.document_sha256, digest)

    def test_overlay_preview_does_not_save_and_placeholders_follow_finalization(self):
        invoice = self.draft("invoice")
        self.assertEqual(self.models.WorkflowNotification.query.count(), 0)
        self.assertEqual(self.client.get(f"/admin/invoices/{invoice.id}/cover-letter").json["title"], "Ihre Rechnung {invoice_number}")
        response = self.client.post(f"/admin/invoices/{invoice.id}/cover-letter/preview", data={"title": "Kosten {invoice_number}", "text": "An {recipient_name}: {amount} {currency}"})
        self.assertIn("Kosten Entwurf", self.pdf_text(response))
        self.db.session.expire_all()
        self.assertIsNone(invoice.cover_letter_title)
        self.assertEqual(self.save_letter(invoice).status_code, 200)
        self.assertIn("{invoice_number}", invoice.cover_letter_title)
        invoice.invoice_number = "NEUE-NUMMER-42"
        self.db.session.commit()
        response = self.client.get(f"/admin/invoices/{invoice.id}/correspondence")
        self.assertIn("NEUE-NUMMER-42", self.pdf_text(response))
        self.assertEqual(self.client.delete(f"/admin/invoices/{invoice.id}/cover-letter").status_code, 200)
        self.assertEqual(self.client.get(f"/admin/invoices/{invoice.id}/correspondence").status_code, 404)

    def test_settings_and_per_invoice_texts_reject_invalid_placeholders_and_lengths(self):
        invoice = self.draft("invoice")
        self.settings.settings.set(self.settings.SettingKeys.INVOICE_COVER_LETTER_TITLE, "Mieterbelastung {contract_number}")
        self.db.session.commit()
        response = self.client.get(f"/admin/invoices/{invoice.id}/cover-letter")
        self.assertEqual(response.json["title"], "Mieterbelastung {contract_number}")
        for title, text in (("", "Text"), ("Titel", ""), ("X" * 201, "Text"), ("Titel", "{unknown}"), ("Titel", "{recipient_name.__class__}"), ("Titel", "X" * 20001)):
            self.assertEqual(self.save_letter(invoice, title, text).status_code, 400)
            self.db.session.expire_all()
            self.assertIsNone(invoice.cover_letter_title)
        with self.assertRaises(ValueError):
            self.settings.settings.set(self.settings.SettingKeys.INVOICE_COVER_LETTER_TEXT, "{unknown}")

    def test_email_draft_and_worker_include_a_frozen_letter_and_embedded_xml(self):
        invoice = self.rendered_invoice()
        self.assertEqual(self.save_letter(invoice, text="ERSTE FASSUNG {amount} {currency}").status_code, 200)
        response = self.client.get(f"/admin/invoices/{invoice.id}/delivery/draft.eml")
        message = BytesParser(policy=policy.default).parsebytes(response.data)
        reader = PdfReader(BytesIO(next(message.iter_attachments()).get_payload(decode=True)))
        self.assertIn("ERSTE FASSUNG", reader.pages[0].extract_text())
        self.assertIn("factur-x.xml", reader.attachments)
        self.assertEqual(self.queue(invoice).status_code, 202)
        item = self.models.WorkflowNotification.query.filter_by(kind="invoice_delivery").one()
        self.assertIn("correspondence_filename", item.payload)
        self.assertEqual(self.save_letter(invoice, text="ZWEITE FASSUNG").status_code, 200)
        self.workflow.process_workflow_notification_batch()
        sent = self.mail.call_args.args[0]
        reader = PdfReader(BytesIO(next(sent.iter_attachments()).get_payload(decode=True)))
        self.assertIn("ERSTE FASSUNG", reader.pages[0].extract_text())
        self.assertNotIn("ZWEITE FASSUNG", reader.pages[0].extract_text())
        self.assertIn("factur-x.xml", reader.attachments)
        self.assertEqual(item.state, "sent")

    def test_custom_stationery_and_saved_letter_errors_do_not_change_the_original(self):
        invoice = self.rendered_invoice()
        original = self.delivery.final_invoice_pdf(invoice)
        upload = FileStorage(stream=BytesIO(self.pdf("BRIEFBOGEN ANSCHREIBEN")), filename="briefbogen.pdf")
        self.admin.store_company_code_stationery(self.company, upload, attribute="cover_letter_stationery_filename")
        self.db.session.commit()
        self.assertEqual(self.save_letter(invoice).status_code, 200)
        response = self.client.get(f"/admin/invoices/{invoice.id}/correspondence")
        reader = PdfReader(BytesIO(response.data))
        self.assertIn("BRIEFBOGEN ANSCHREIBEN", reader.pages[0].extract_text())
        self.assertNotIn("BRIEFBOGEN ANSCHREIBEN", reader.pages[1].extract_text())
        self.assertEqual(self.delivery.final_invoice_pdf(invoice), original)
        previous_title = invoice.cover_letter_title
        path = Path(self.app.instance_path) / "invoice_stationery" / self.company.cover_letter_stationery_filename
        path.unlink()
        self.assertEqual(self.save_letter(invoice, "Neuer Titel").status_code, 400)
        self.db.session.expire_all()
        self.assertEqual(invoice.cover_letter_title, previous_title)
        with self.assertRaises(ValueError):
            self.admin.validate_company_code_stationery(FileStorage(stream=BytesIO(b"%PDF-broken"), filename="defekt.pdf"))

    def test_dashboard_count_matches_the_linked_invoice_filter(self):
        self.draft("invoice")
        canceled = self.draft("invoice")
        canceled.status = "canceled"
        foreign = self.draft("invoice")
        foreign.responsible_user = self.reviewer
        self.db.session.commit()
        with patch.object(self.admin, "render_template", side_effect=lambda template, **values: str(values["my_invoice_count"])):
            response = self.client.get("/admin")
        self.assertEqual(response.data, b"1")
        with self.app.test_request_context():
            html = render_template("admin/dashboard.html", my_invoice_count=1)
        self.assertIn("responsible=mine", html)
        self.assertIn("status=active", html)

    def test_invoice_permissions_cover_all_new_routes(self):
        invoice = self.draft("invoice")
        self.creator.can_manage_invoices = False
        self.db.session.commit()
        self.login(self.creator)
        for path in ("/admin/invoices", f"/admin/invoices/{invoice.id}/cover-letter", f"/admin/invoices/{invoice.id}/correspondence"):
            self.assertEqual(self.client.get(path).status_code, 403)
        self.assertEqual(self.assign(invoice).status_code, 403)
        self.assertEqual(self.save_letter(invoice).status_code, 403)
        self.assertEqual(self.client.post(f"/admin/invoices/{invoice.id}/cover-letter/preview").status_code, 403)
        self.assertEqual(self.client.delete(f"/admin/invoices/{invoice.id}/cover-letter").status_code, 403)
        self.assertEqual(self.models.WorkflowNotification.query.count(), 0)

    def test_layout_editor_is_admin_only_and_can_save_validate_and_reset(self):
        url = "/admin/invoice-cover-letter-layout"
        definition = self.settings.SettingKeys.INVOICE_COVER_LETTER_LAYOUT
        custom = '<style>@page {margin:35mm}</style><h1>{{ title }}</h1><p>{{ text }}</p>'
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, data={"html": custom}).status_code, 403)
        self.assertEqual(self.client.post(url + "/preview", data={"html": custom}).status_code, 403)
        self.login(self.administrator)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"HTML- und CSS-Layout", response.data)
        self.assertEqual(self.client.post(url, data={"html": custom, "action": "save"}).status_code, 302)
        self.assertEqual(self.settings.settings.get(definition), custom)
        invalid = "{{ unknown_layout_field }}"
        response = self.client.post(url, data={"html": invalid, "action": "save"})
        self.assertEqual(response.status_code, 200)
        self.assertIn(invalid.encode(), response.data)
        self.assertEqual(self.settings.settings.get(definition), custom)
        self.assertEqual(self.client.post(url, data={"action": "reset"}).status_code, 302)
        self.assertEqual(self.settings.settings.get(definition), definition.default)

    def test_layout_preview_uses_unsaved_html_and_actual_stationery_without_saving(self):
        invoice = self.rendered_invoice()
        upload = FileStorage(stream=BytesIO(self.pdf("GESCHAEFTSPAPIER VORSCHAU")), filename="briefbogen.pdf")
        self.admin.store_company_code_stationery(self.company, upload, attribute="cover_letter_stationery_filename")
        self.db.session.commit()
        self.login(self.administrator)
        layout = '<style>@page {size:A4; margin:55mm 20mm}</style><h1>NEUER AUFBAU</h1><p>{{ recipient_address }}</p><p>{{ caseworker_phone }}</p><div>{{ text }}</div>'
        response = self.client.post("/admin/invoice-cover-letter-layout/preview", data={"html": layout, "invoice_id": invoice.id})
        text = self.pdf_text(response)
        for value in ("NEUER AUFBAU", "GESCHAEFTSPAPIER VORSCHAU", invoice.recipient_name, self.creator.phone):
            self.assertIn(value, text)
        self.db.session.expire_all()
        self.assertIsNone(invoice.cover_letter_title)
        self.assertEqual(self.settings.settings.get(self.settings.SettingKeys.INVOICE_COVER_LETTER_LAYOUT), self.settings.SettingKeys.INVOICE_COVER_LETTER_LAYOUT.default)
        response = self.client.post("/admin/invoice-cover-letter-layout/preview", data={"html": "<h1>{{ title }}</h1><div>{{ text }}</div>"})
        self.assertIn("RE-BEISPIEL-001", self.pdf_text(response))
        self.assertEqual(self.client.post("/admin/invoice-cover-letter-layout/preview", data={"html": "{{ secret }}"}).status_code, 400)

    def test_custom_layout_changes_new_pdfs_and_preserves_queued_mail_documents(self):
        invoice = self.rendered_invoice()
        self.assertEqual(self.save_letter(invoice).status_code, 200)
        original = self.delivery.final_invoice_pdf(invoice)
        before = self.client.get(f"/admin/invoices/{invoice.id}/correspondence").data
        self.assertNotIn("Betriebsweg 1", PdfReader(BytesIO(before)).pages[0].extract_text())
        self.assertEqual(self.queue(invoice).status_code, 202)
        delivery = self.models.WorkflowNotification.query.filter_by(kind="invoice_delivery").one()
        queued = self.invoice_document.resolve_invoice_document_path(delivery.payload["correspondence_filename"]).read_bytes()
        self.settings.settings.set(self.settings.SettingKeys.INVOICE_COVER_LETTER_LAYOUT, '<h1>GEAENDERTES LAYOUT</h1><p>{{ text }}</p>')
        self.db.session.commit()
        current = self.client.get(f"/admin/invoices/{invoice.id}/correspondence")
        self.assertIn("GEAENDERTES LAYOUT", self.pdf_text(current))
        self.assertEqual(self.delivery.final_invoice_pdf(invoice), original)
        self.workflow.process_workflow_notification_batch()
        sent = next(self.mail.call_args.args[0].iter_attachments()).get_payload(decode=True)
        self.assertEqual(sent, queued)
        self.assertNotIn("GEAENDERTES LAYOUT", PdfReader(BytesIO(sent)).pages[0].extract_text())

    def test_general_settings_do_not_overwrite_the_layout_and_fetches_are_limited_to_data(self):
        definition = self.settings.SettingKeys.INVOICE_COVER_LETTER_LAYOUT
        custom = "<p>{{ text }}</p>"
        self.settings.settings.set(definition, custom)
        self.db.session.commit()
        self.login(self.administrator)
        with patch.object(self.admin, "all_definitions", return_value=[definition]), patch.object(self.admin, "configure_logging"):
            self.assertEqual(self.client.post("/admin/settings", data={definition.key: "<p>Unerwartete Änderung</p>"}).status_code, 302)
        self.assertEqual(self.settings.settings.get(definition), custom)
        with patch.object(self.invoice_correspondence, "default_url_fetcher") as fetch:
            for url in ("file:///etc/passwd", "https://example.invalid/logo.png", "http://127.0.0.1/private", "ftp://example.invalid/logo.png"):
                with self.assertRaises(ValueError):
                    self.invoice_correspondence.cover_letter_url_fetcher(url)
            fetch.assert_not_called()
            self.invoice_correspondence.cover_letter_url_fetcher("data:image/png;base64,AA==")
            fetch.assert_called_once()
