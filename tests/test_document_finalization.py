from datetime import date
from decimal import Decimal
from html.parser import HTMLParser
from io import BytesIO
import json
import sys
import unittest
from unittest.mock import patch

from flask import render_template
from pypdf import PdfReader

import test_invoice_delivery as fixtures


class FormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms = {}
        self.inputs = []
        self.buttons = []
        self.stack = []
        self.maximum_depth = 0

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        if tag == "form":
            self.stack.append(attributes.get("id"))
            self.maximum_depth = max(self.maximum_depth, len(self.stack))
            if attributes.get("id"):
                self.forms[attributes["id"]] = attributes
        if tag in {"input", "textarea", "select", "button"}:
            owner = attributes.get("form") or (self.stack[-1] if self.stack else None)
            (self.buttons if tag == "button" else self.inputs).append((owner, attributes))

    def handle_endtag(self, tag):
        if tag == "form":
            self.stack.pop()


class DocumentFinalizationTests(unittest.TestCase):
    login = fixtures.InvoiceDeliveryTests.login
    start_patch = fixtures.InvoiceDeliveryTests.start_patch
    image = fixtures.InvoiceDeliveryTests.image
    form = fixtures.InvoiceDeliveryTests.form
    post_order = fixtures.InvoiceDeliveryTests.post_order
    draft = fixtures.InvoiceDeliveryTests.draft
    draft_form = fixtures.InvoiceDeliveryTests.draft_form
    complete_draft = fixtures.InvoiceDeliveryTests.complete_draft
    pdf = fixtures.InvoiceDeliveryTests.pdf
    pdf_text = fixtures.InvoiceDeliveryTests.pdf_text
    upload = fixtures.InvoiceDeliveryTests.upload
    prepare_stationery = fixtures.InvoiceDeliveryTests.prepare_stationery

    @classmethod
    def setUpClass(cls):
        fixtures.InvoiceDeliveryTests.setUpClass.__func__(cls)

    def setUp(self):
        fixtures.InvoiceDeliveryTests.setUp(self)
        self.prepare_stationery()
        self.company.stationery_filename = self.company.invoice_stationery_filename
        self.db.session.commit()
        self.start_patch(patch.object(sys.modules[self.package_name + ".dms_context"], "offer_context", return_value={}, create=True))

    def document(self, kind):
        item = self.complete_draft(kind)
        if kind == "invoice":
            item.invoice_number = None
        else:
            item.craft_activity = self.craft
        self.db.session.commit()
        return item

    def current_form(self):
        values = self.draft_form()
        values.update(save_draft="1", craft_activity_id=str(self.craft.id), invoice_date="2026-11-18", offer_date="2026-11-18",
                      valid_until="2026-12-20", due_date="2026-12-15", service_date_from="2026-11-17", service_date_to="2026-11-17",
                      introductory_text="Letzte Änderung vor Freigabe", concluding_text="Neue Schlussbemerkung",
                      position_description=["Geänderte Reparatur", "Zusätzliche Leistung"], position_details=["Neue Zusatzinfo", ""],
                      position_article_number=["NEU-42", ""], position_quantity=["3", "1"], position_unit=["Stk", "Std"],
                      position_unit_price_net=["12,50", "5,00"], position_vat_rate=["19", "7"], position_source_type=["free", "free"],
                      payment_information_data=json.dumps({"method": "bank_transfer", "iban": "DE12500105170648489890", "bic": "INGDDEFFXXX", "source": "manual"}))
        return values

    def finalize(self, kind, item, values):
        action = "finalize" if kind == "invoice" else "release"
        response = self.client.post(f"/admin/{kind}s/{item.id}/{action}", data=values)
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True)[:1000])
        self.db.session.expire_all()
        return response

    def assert_saved_version(self, item):
        self.assertEqual(item.recipient_name, "Neue freie Empfängerin")
        self.assertEqual(item.introductory_text, "Letzte Änderung vor Freigabe")
        self.assertEqual(item.concluding_text, "Neue Schlussbemerkung")
        self.assertEqual([position.description for position in item.positions], ["Geänderte Reparatur", "Zusätzliche Leistung"])
        self.assertEqual([position.article_number for position in item.positions], ["NEU-42", ""])
        self.assertEqual(item.positions[0].details, "Neue Zusatzinfo")
        self.assertEqual(item.positions[0].quantity, Decimal("3"))
        self.assertEqual(item.total_net, Decimal("42.50"))
        self.assertEqual(item.total_vat, Decimal("7.48"))
        self.assertEqual(item.total_gross, Decimal("49.98"))

    def assert_original_draft(self, kind, item, position_id):
        self.assertEqual(item.status, "draft")
        self.assertEqual(item.recipient_name, "Testperson")
        self.assertEqual(len(item.positions), 1)
        self.assertEqual(item.positions[0].id, position_id)
        self.assertEqual(item.positions[0].description, "Gespeicherte Position")
        self.assertIsNone(getattr(item, kind + "_number"))
        self.assertIsNone(item.document_filename)

    def test_invoice_finalization_saves_current_fields_positions_and_payment_before_pdf_and_xml(self):
        item = self.document("invoice")
        self.assertEqual(self.upload(item).status_code, 201)
        self.finalize("invoice", item, self.current_form())
        self.assertEqual(item.status, "finalized")
        self.assert_saved_version(item)
        self.assertEqual(item.invoice_date, date(2026, 11, 18))
        self.assertEqual(item.due_date, date(2026, 12, 15))
        self.assertEqual(item.payment_information["iban"], "DE12500105170648489890")
        reader = PdfReader(BytesIO(self.delivery.final_invoice_pdf(item)))
        text = "\n".join(page.extract_text() for page in reader.pages)
        for value in ("Neue freie Empfängerin", "Geänderte Reparatur", "Zusätzliche Leistung", "Neue Zusatzinfo", "49,98", "Letzte Änderung vor Freigabe", "ANLAGE 1"):
            self.assertIn(value, text)
        self.assertNotIn("Gespeicherte Position", text)
        xml = reader.attachments["factur-x.xml"][0].decode()
        for value in ("Neue freie Empfängerin", "Geänderte Reparatur", "49.98", "DE12500105170648489890"):
            self.assertIn(value, xml)
        self.assertEqual(self.models.AuditLog.query.filter_by(action="invoice.update").count(), 1)

    def test_offer_release_saves_current_fields_and_positions_before_pdf(self):
        item = self.document("offer")
        self.finalize("offer", item, self.current_form())
        self.assertEqual(item.status, "awaiting_response")
        self.assert_saved_version(item)
        self.assertEqual(item.offer_date, date(2026, 11, 18))
        self.assertEqual(item.valid_until, date(2026, 12, 20))
        data = self.offer_document.resolve_offer_document_path(item.document_filename).read_bytes()
        text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(data)).pages)
        for value in ("Neue freie Empfängerin", "Geänderte Reparatur", "Zusätzliche Leistung", "Neue Zusatzinfo", "49,98", "Letzte Änderung vor Freigabe"):
            self.assertIn(value, text)
        self.assertNotIn("Gespeicherte Position", text)
        self.assertEqual(self.models.AuditLog.query.filter_by(action="offer.update").count(), 1)

    def test_offer_layout_applies_to_previews_and_released_pdfs(self):
        item = self.document("offer")
        self.offer_document.stationery_path(item).write_bytes(self.pdf("GESCHAEFTSPAPIER"))
        self.login(self.administrator)
        url = "/admin/offer-layout"
        definition = self.settings.SettingKeys.OFFER_LAYOUT
        custom = (
            "<h1>GESTALTETES {{ document_title }}</h1><p>{{ offer_number }}</p>"
            "<p>{{ recipient_address | join(' · ') }}</p>"
            "{% for position in positions %}<p>{{ position.description }}</p>{% endfor %}"
        )
        editor = self.client.get(url)
        self.assertEqual(editor.status_code, 200)
        self.assertIn(b'data-preview-url="/admin/offer-layout/preview"', editor.data)
        sample = self.pdf_text(self.client.post(url + "/preview", data={"html": custom}))
        self.assertIn("AN-BEISPIEL-001", sample)
        self.assertNotIn("GESCHAEFTSPAPIER", sample)
        saved_offer = self.pdf_text(self.client.post(url + "/preview", data={"html": custom, "offer_id": item.id}))
        self.assertIn("GESCHAEFTSPAPIER", saved_offer)
        self.assertIn(item.recipient_name, saved_offer)
        self.assertEqual(self.settings.settings.get(definition), definition.default)
        self.assertEqual(self.client.post(url + "/preview", data={"html": "{{ invoice_number }}"}).status_code, 400)

        self.assertEqual(self.client.post(url, data={"html": custom}).status_code, 302)
        self.assertEqual(self.settings.settings.get(definition), custom)
        draft_url = f"/admin/offers/{item.id}/preview"
        self.assertIn("GESTALTETES Angebot", self.pdf_text(self.client.post(draft_url, data=self.draft_form())))
        self.finalize("offer", item, self.current_form())
        data = self.offer_document.resolve_offer_document_path(item.document_filename).read_bytes()
        text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(data)).pages)
        for value in ("GESTALTETES Angebot", "GESCHAEFTSPAPIER", item.offer_number, "Geänderte Reparatur"):
            self.assertIn(value, text)
        self.assertEqual(self.client.post(url, data={"action": "reset"}).status_code, 302)
        self.assertEqual(self.settings.settings.get(definition), definition.default)

    def test_invalid_position_rolls_back_changes_and_does_not_finalize_the_saved_version(self):
        for kind in ("invoice", "offer"):
            with self.subTest(kind=kind):
                item = self.document(kind)
                position_id = item.positions[0].id
                values = self.current_form()
                values["position_quantity"] = ["keine Zahl", "1"]
                self.finalize(kind, item, values)
                self.assert_original_draft(kind, item, position_id)
        self.assertEqual(self.models.AuditLog.query.count(), 0)
        self.assertEqual(self.models.ErpOutboxOperation.query.count(), 0)

    def test_missing_recipient_in_current_form_blocks_finalization_of_previously_complete_draft(self):
        for kind in ("invoice", "offer"):
            with self.subTest(kind=kind):
                item = self.document(kind)
                position_id = item.positions[0].id
                values = self.current_form()
                values["recipient_name"] = ""
                self.finalize(kind, item, values)
                self.assert_original_draft(kind, item, position_id)
        self.assertEqual(self.models.AuditLog.query.count(), 0)

    def test_pdf_failure_rolls_back_form_changes_and_number_allocation(self):
        for kind in ("invoice", "offer"):
            with self.subTest(kind=kind):
                item = self.document(kind)
                position_id = item.positions[0].id
                module = self.invoice_service if kind == "invoice" else self.offer_service
                error = self.invoice_document.InvoiceDocumentError if kind == "invoice" else self.offer_document.OfferDocumentError
                with patch.object(module, f"create_{kind}_document", side_effect=error("PDF-Testfehler")):
                    self.finalize(kind, item, self.current_form())
                self.assert_original_draft(kind, item, position_id)
        self.assertEqual(self.models.AuditLog.query.count(), 0)

    def test_repeated_submission_cannot_change_or_renumber_a_final_document(self):
        for kind in ("invoice", "offer"):
            with self.subTest(kind=kind):
                item = self.document(kind)
                self.finalize(kind, item, self.current_form())
                number = getattr(item, kind + "_number")
                digest = item.document_sha256
                position_id = item.positions[0].id
                values = self.current_form()
                values["recipient_name"] = "Spätere Änderung"
                self.finalize(kind, item, values)
                self.assert_saved_version(item)
                self.assertEqual(getattr(item, kind + "_number"), number)
                self.assertEqual(item.document_sha256, digest)
                self.assertEqual(item.positions[0].id, position_id)

    def test_normal_draft_save_stays_editable_and_legacy_finalization_keeps_saved_data(self):
        for kind in ("invoice", "offer"):
            with self.subTest(kind=kind):
                item = self.document(kind)
                response = self.client.post(f"/admin/{kind}s/{item.id}", data=self.current_form())
                self.assertEqual(response.status_code, 302)
                self.db.session.expire_all()
                self.assert_saved_version(item)
                self.assertEqual(item.status, "draft")
                self.assertIsNone(getattr(item, kind + "_number"))
                self.finalize(kind, item, {})
                self.assert_saved_version(item)
                self.assertIsNotNone(getattr(item, kind + "_number"))

    def test_finalize_buttons_submit_the_main_form_with_csrf_and_current_inputs(self):
        for kind, form_id, action in (("invoice", "invoice-draft-form", "finalize"), ("offer", "offerDraftForm", "release")):
            with self.subTest(kind=kind):
                item = self.document(kind)
                with self.app.test_request_context():
                    html = render_template(f"admin/{kind}_draft.html", item=item, editable=True, task_templates=[], craft_activities=[],
                                           catalogue_items=[], position_source_labels={}, status_labels={}, selected_object=None,
                                           work_report_counts={}, has_billed_work_reports=False, has_any_work_reports=False)
                parser = FormParser()
                parser.feed(html)
                buttons = [(owner, attributes) for owner, attributes in parser.buttons if "data-document-finalize" in attributes]
                self.assertEqual(len(buttons), 1)
                owner, button = buttons[0]
                self.assertEqual(owner, form_id)
                self.assertEqual(button["type"], "submit")
                self.assertEqual(button["formaction"], f"/admin/{kind}s/{item.id}/{action}")
                inputs = {attributes.get("name"): attributes for owner, attributes in parser.inputs if owner == form_id}
                self.assertEqual(inputs["save_draft"]["value"], "1")
                self.assertEqual(inputs["csrf_token"]["value"], "test-token")
                self.assertIn("recipient_name", inputs)
                self.assertIn("position_description", inputs)
                self.assertEqual(parser.maximum_depth, 1)
                self.assertEqual(parser.stack, [])
                self.assertIn("document_finalization.js", html)
