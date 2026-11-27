from datetime import date, timedelta
from email import policy
from email.parser import BytesParser
from hashlib import sha256
from io import BytesIO
import importlib
import json
import sys
import unittest
from unittest.mock import patch

from lxml import etree
from pypdf import PdfReader
from werkzeug.datastructures import FileStorage

import test_invoice_delivery as fixtures
import test_invoice_payments as payment_fixtures
from test_invoice_payments import DEBIT_IBAN, TRANSFER_IBAN


class InvoiceTenantBillingTests(unittest.TestCase):
    login = fixtures.InvoiceDeliveryTests.login
    start_patch = fixtures.InvoiceDeliveryTests.start_patch
    image = fixtures.InvoiceDeliveryTests.image
    form = fixtures.InvoiceDeliveryTests.form
    post_order = fixtures.InvoiceDeliveryTests.post_order
    draft = fixtures.InvoiceDeliveryTests.draft
    draft_form = fixtures.InvoiceDeliveryTests.draft_form
    pdf = fixtures.InvoiceDeliveryTests.pdf
    upload = fixtures.InvoiceDeliveryTests.upload
    queue = fixtures.InvoiceDeliveryTests.queue
    prepare_stationery = fixtures.InvoiceDeliveryTests.prepare_stationery
    api_rows = payment_fixtures.InvoicePaymentTests.api_rows

    @classmethod
    def setUpClass(cls):
        fixtures.InvoiceDeliveryTests.setUpClass.__func__(cls)
        cls.tables.append(cls.models.InvoiceBillingRule.__table__)
        for name in ("invoice_correspondence", "tenant_charge_letter", "commission_tenant_charge", "commission_followup"):
            sys.modules.pop(cls.package_name + "." + name, None)
        guard = sys.modules[cls.package_name + ".auth"].invoice_required
        for endpoint in (
            "invoice_create", "invoice_edit", "invoice_cover_letter", "invoice_cover_letter_preview",
            "invoice_correspondence", "invoice_complete", "invoice_tenant_charge",
            "invoice_tenant_charge_preview", "invoice_tenant_charge_document",
        ):
            view = cls.app.view_functions["admin." + endpoint]
            cls.app.view_functions["admin." + endpoint] = guard(view.__wrapped__)

    def setUp(self):
        fixtures.InvoiceDeliveryTests.setUp(self)
        self.start_patch(patch.object(self.admin, "work_report_counts", return_value={}))
        self.prepare_stationery()
        self.issuer = self.models.WowiCompanyCode(
            erp_id=99, name="Testbetrieb GmbH", code="GMBH", seller_name="Testbetrieb GmbH",
            seller_street="Werkstraße 9", seller_postal_code="12345", seller_town="Teststadt",
            seller_tax_number="12/345/67890", seller_iban=DEBIT_IBAN, seller_bic="INGDDEFFXXX",
            stationery_filename="briefbogen.pdf",
        )
        self.company.seller_name = "Testgenossenschaft eG"
        self.company.document_creation_disabled = True
        self.company.tenant_billing_issuer = self.issuer
        self.company.tenant_billing_recipient_email = "rechnungen@example.invalid"
        self.db.session.add(self.issuer)
        self.db.session.commit()
        self.party = {
            "person_id": 5000, "name": "Testmieterin", "street": "Postweg 2",
            "postal_code": "12345", "town": "Teststadt", "country_code": "DE",
            "recipient_type": "private", "email": "mieterin@example.invalid",
        }
        self.remote.get_invoice_recipient_candidates.return_value = [self.party]

    def create_invoice(self):
        response = self.client.post("/admin/invoices/new", data={
            "mode": "agreement", "agreement_id": self.agreement.erp_id, "person_id": self.party["person_id"],
        })
        self.assertEqual(response.status_code, 302, response.data[:1000])
        return self.models.Invoice.query.one()

    def test_contract_search_and_draft_creation_route_to_business_recipient_before_an_invoice_exists(self):
        response = self.client.get("/admin/invoices/new?q=MV-COMMON")
        self.assertIn(b"MV-COMMON", response.data)
        self.assertIn(f"agreement_id={self.agreement.erp_id}".encode(), response.data)
        response = self.client.get(f"/admin/invoices/new?agreement_id={self.agreement.erp_id}")
        self.assertIn(b"Testgenossenschaft eG", response.data)
        self.assertIn(b"Testbetrieb GmbH", response.data)
        self.assertEqual(self.models.Invoice.query.count(), 0)

        invoice = self.create_invoice()
        self.assertEqual(invoice.company_code_id, self.issuer.id)
        self.assertEqual(invoice.recipient_name, self.company.seller_name)
        self.assertEqual(invoice.recipient_type, "business")
        self.assertEqual(invoice.recipient_email, self.company.tenant_billing_recipient_email)
        self.assertIsNone(invoice.recipient_person_erp_id)
        self.assertIsNone(invoice.recipient_license_agreement_erp_id)
        self.assertEqual(invoice.tenant_billing_reference["agreement_erp_id"], self.agreement.erp_id)
        self.assertEqual(invoice.tenant_billing_reference["property_address"], self.unit.address)
        self.assertEqual(invoice.billing_contract_number, self.agreement.id_num)
        url = f"/admin/invoices/{invoice.id}"
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Anschreiben zur Mieterbelastung", page.data)
        self.assertNotIn(b"invoiceCoverLetterModal", page.data)
        self.assertEqual(self.client.get(url + "/tenant-charge").status_code, 409)
        self.assertEqual(self.client.get(url + "/cover-letter").status_code, 409)
        for suffix in ("/cover-letter", "/cover-letter/preview"):
            response = self.client.post(url + suffix, data={"title": "Rechnung", "text": "An {recipient_name}"})
            self.assertEqual(response.status_code, 409)
        self.assertIsNone(invoice.cover_letter_title)

    def test_inactive_issuer_and_foreign_person_are_rejected(self):
        for person_id, blocked in ((123, False), (self.party["person_id"], True)):
            with self.subTest(person_id=person_id, blocked=blocked):
                self.issuer.document_creation_disabled = blocked
                self.db.session.commit()
                self.client.post("/admin/invoices/new", data={
                    "mode": "agreement", "agreement_id": self.agreement.erp_id, "person_id": person_id,
                })
                self.assertEqual(self.models.Invoice.query.count(), 0)

    def test_default_direct_invoice_prefills_the_tenants_recipient_and_contract(self):
        self.company.tenant_billing_issuer = None
        self.company.document_creation_disabled = False
        self.db.session.commit()
        invoice = self.create_invoice()
        self.assertEqual(invoice.company_code_id, self.company.id)
        self.assertEqual(invoice.recipient_name, self.party["name"])
        self.assertEqual(invoice.recipient_license_agreement_erp_id, self.agreement.erp_id)
        self.assertIsNone(invoice.tenant_billing_reference)
        self.assertIn(b"invoiceCoverLetterModal", self.client.get(f"/admin/invoices/{invoice.id}").data)

    def test_self_billing_uses_the_tenant_recipient_and_completes_with_the_whole_print_pdf(self):
        self.company.tenant_billing_issuer = None
        self.company.document_creation_disabled = False
        self.company.tenant_charge_letter_enabled = True
        self.db.session.commit()
        invoice = self.create_invoice()
        url = f"/admin/invoices/{invoice.id}"
        self.assertEqual(invoice.company_code, self.company)
        self.assertEqual(invoice.recipient_name, self.party["name"])
        self.assertEqual(invoice.recipient_license_agreement_erp_id, self.agreement.erp_id)
        self.assertIsNone(invoice.due_date)
        draft = self.client.get(url).get_data(as_text=True)
        self.assertIn("Anschreiben zur Mieterbelastung", draft)
        self.assertNotIn('name="due_date"', draft)
        self.assertNotIn('name="payment_information_data"', draft)
        self.assertEqual(self.upload(invoice, [(self.pdf("BELEG ZUM MIETERANSCHREIBEN"), "beleg.pdf")]).status_code, 201)
        form = self.draft_form()
        form.update(save_draft="1", service_date_from=form["invoice_date"], due_date="ungültig")
        self.assertEqual(self.client.post(url + "/finalize", data=form).status_code, 302)
        self.db.session.refresh(invoice)
        self.assertEqual(invoice.status, "finalized")
        self.assertIsNone(invoice.due_date)
        self.assertIsNone(invoice.payment_information)
        original = PdfReader(BytesIO(self.delivery.final_invoice_pdf(invoice)))
        xml = etree.fromstring(original.attachments["factur-x.xml"][0])
        self.assertEqual(xml.xpath("string(//*[local-name()='BuyerTradeParty']/*[local-name()='Name'])"), self.party["name"])
        self.assertFalse(xml.xpath("//*[local-name()='DueDateDateTime' or local-name()='SpecifiedTradeSettlementPaymentMeans']"))
        self.assertNotIn(TRANSFER_IBAN, original.pages[0].extract_text())

        agreement, mode = self.api_rows()
        self.remote.read_all_rows.side_effect = lambda endpoint, *_args, **_kwargs: [agreement] if endpoint.endswith("LicenseAgreements") else [mode]
        self.assertEqual(self.client.get(url + "/abschluss").location, url + "/tenant-charge?complete=1")
        self.assertEqual(self.client.get(url + "/delivery/complete").location, url + "/tenant-charge?complete=1")
        letter_page = self.client.get(url + "/tenant-charge?complete=1").get_data(as_text=True)
        self.assertIn("Schritt 1 von 2", letter_page)
        self.assertIn("Weiter zum Versand", letter_page)
        form = {
            "purpose": "Ungespeicherter Anlass für den Mieter", "revision": 0, "payment_method": "sepa_debit",
            "person_id": self.party["person_id"], "due_date": (date.today() + timedelta(days=10)).isoformat(),
        }
        archive_count = self.archive.call_count
        preview = self.client.post(url + "/tenant-charge/preview", data=form)
        self.assertEqual(preview.status_code, 200, preview.data[:500])
        self.assertIn(form["purpose"], PdfReader(BytesIO(preview.data)).pages[0].extract_text())
        self.db.session.refresh(invoice)
        self.assertIsNone(invoice.tenant_charge_letter)
        self.assertEqual(self.archive.call_count, archive_count)

        saved = self.client.post(url + "/tenant-charge?complete=1", data=form)
        self.assertEqual(saved.location, url + "/delivery/complete")
        self.assertIn(b"Schritt 2 von 2", self.client.get(saved.location).data)
        download = self.client.post(saved.location, data={"mode": "print"})
        self.assertEqual(download.status_code, 200, download.data[:500])
        self.assertIn("attachment", download.headers["Content-Disposition"])
        package = PdfReader(BytesIO(download.data))
        self.assertEqual(len(package.pages), 3)
        self.assertIn(form["purpose"], package.pages[0].extract_text())
        self.assertIn("BELEG ZUM MIETERANSCHREIBEN", package.pages[-1].extract_text())
        self.assertEqual(package.attachments["factur-x.xml"], original.attachments["factur-x.xml"])
        self.db.session.refresh(invoice)
        self.assertEqual(invoice.completion_mode, "print")
        self.assertIn(b"Versendet", self.client.get(url).data)
        self.mail.assert_not_called()

    def test_opening_the_invoice_pdf_creates_and_archives_the_default_tenant_letter_once(self):
        invoice = self.create_invoice()
        url = f"/admin/invoices/{invoice.id}"
        self.assertEqual(self.upload(invoice, [(self.pdf("ZUSÄTZLICHER BELEG"), "beleg.pdf")]).status_code, 201)
        form = self.draft_form()
        form.update(save_draft="1", service_date_from=form["invoice_date"])
        self.assertEqual(self.client.post(url + "/finalize", data=form).status_code, 302)
        self.db.session.refresh(invoice)
        self.assertEqual(invoice.status, "finalized")
        page = etree.HTML(self.client.get(url).data)
        document_url = url + "/document"
        actions = page.xpath("//form[@method='post' and @action=$target]/button", target=document_url)
        self.assertEqual([action.text for action in actions], ["Rechnungs-PDF aufrufen"])

        agreement, mode = self.api_rows()
        self.remote.read_all_rows.side_effect = lambda endpoint, *_args, **_kwargs: [agreement] if endpoint.endswith("LicenseAgreements") else [mode]
        self.settings.settings.set(self.settings.SettingKeys.INVOICE_TENANT_CHARGE_PURPOSE_TEMPLATE, "Standardanschreiben für {invoice_issuer}")
        self.db.session.commit()
        archive_count = self.archive.call_count
        correspondence = importlib.import_module(self.package_name + ".invoice_correspondence")
        with patch.object(correspondence, "letter_date", return_value=date(2026, 12, 16)):
            self.assertEqual(self.client.get(document_url).mimetype, "application/pdf")
            self.db.session.refresh(invoice)
            self.assertIsNone(invoice.tenant_charge_letter)
            self.assertEqual(self.archive.call_count, archive_count)
            response = self.client.post(document_url)
        self.assertEqual(response.status_code, 200, response.data[:1000])
        self.assertEqual(response.mimetype, "application/pdf")
        self.assertIn("inline", response.headers["Content-Disposition"])
        self.assertIn("no-store", response.headers["Cache-Control"])
        package = PdfReader(BytesIO(response.data))
        self.assertEqual(len(package.pages), 3)
        self.assertIn("Standardanschreiben für Testbetrieb GmbH", package.pages[0].extract_text())
        self.assertIn("01.02.2027", package.pages[0].extract_text())
        self.assertIn("Testgenossenschaft eG", package.pages[1].extract_text())
        self.assertIn("ZUSÄTZLICHER BELEG", package.pages[2].extract_text())
        self.assertIn("factur-x.xml", package.attachments)
        self.assertEqual(self.archive.call_count, archive_count + 1)
        self.assertEqual(self.archive.call_args.args[4], response.data)
        self.db.session.refresh(invoice)
        self.assertEqual(invoice.tenant_charge_letter["revision"], 1)
        self.assertIsNone(invoice.completed_at)

        self.remote.read_all_rows.reset_mock()
        for open_document in (self.client.get, self.client.post):
            saved = open_document(document_url)
            self.assertEqual(saved.mimetype, "application/pdf")
            self.assertIn("01.02.2027", PdfReader(BytesIO(saved.data)).pages[0].extract_text())
        self.remote.read_all_rows.assert_not_called()
        self.assertEqual(self.archive.call_count, archive_count + 1)
        self.mail.assert_not_called()

    def test_tenant_due_date_uses_the_configurable_cutoff_across_the_year_boundary(self):
        module = importlib.import_module(self.package_name + ".tenant_charge_letter")
        self.assertEqual(module.tenant_letter_due_date(date(2026, 12, 15)), date(2027, 1, 1))
        self.assertEqual(module.tenant_letter_due_date(date(2026, 12, 16)), date(2027, 2, 1))
        self.assertEqual(module.tenant_letter_due_date(date(2026, 12, 31)), date(2027, 2, 1))
        key = self.settings.SettingKeys.INVOICE_TENANT_CHARGE_CUTOFF_DAY
        self.settings.settings.set(key, 20)
        self.assertEqual(module.tenant_letter_due_date(date(2026, 12, 16)), date(2027, 1, 1))
        self.assertEqual(module.tenant_letter_due_date(date(2026, 12, 20)), date(2027, 2, 1))
        for invalid_day in (0, 32):
            with self.assertRaises(ValueError):
                self.settings.settings.set(key, invalid_day)

    def test_commission_and_offer_drafts_use_the_rule_with_a_disabled_contract_book(self):
        commission = self.post_order()
        commission.local_status = self.models.CommissionLocalStatus.TENANT_CHARGE_REVIEW.value
        self.db.session.commit()
        self.assertIn(commission, self.invoice_service.available_invoice_commissions())
        invoice = self.invoice_service.create_commission_invoice_draft([commission], self.creator)
        self.assertEqual(invoice.company_code_id, self.issuer.id)
        self.assertEqual(invoice.recipient_name, self.company.seller_name)
        self.assertEqual(invoice.tenant_billing_reference["agreement_erp_id"], self.agreement.erp_id)
        self.db.session.rollback()

        offer = self.draft("offer")
        offer.commission = commission
        offer.license_agreement_erp_id = self.agreement.erp_id
        offer.recipient_name = self.party["name"]
        invoice = self.invoice_service.create_offer_invoice_draft(offer)
        self.assertEqual(invoice.company_code_id, self.issuer.id)
        self.assertEqual(invoice.recipient_name, self.company.seller_name)
        self.assertEqual(invoice.tenant_billing_reference["agreement_erp_id"], self.agreement.erp_id)

    def configure_tenant_charge_contracts(self):
        self.settings.settings.set(self.settings.SettingKeys.COMMISSION_TENANT_CHARGE_TYPE_IDS, [100])
        self.agreement.start_date = date(2026, 11, 1)
        self.db.session.commit()
        former = {**self.party, "person_id": 8101, "name": "Ehemalige Mieterin", "street": "Neue Adresse 3"}
        agreements = {
            9000: {"number": "MV-ALT", "start": date(2024, 1, 1), "end": date(2026, 10, 31)},
            self.agreement.erp_id: {"number": self.agreement.id_num, "start": self.agreement.start_date, "end": None},
            9002: {"number": "MV-KUENFTIG", "start": date(2027, 1, 1), "end": None},
        }
        self.remote.get_tenant_charge_agreements.side_effect = lambda unit_id, agreement_id=None: {
            key: value for key, value in agreements.items()
            if unit_id == self.unit.erp_id and (agreement_id is None or agreement_id == key)
        }
        self.remote.get_invoice_recipient_candidates.side_effect = {
            9000: [former], self.agreement.erp_id: [self.party],
            9002: [{**self.party, "person_id": 8102, "name": "Künftiger Mieter"}],
        }.get
        self.start_patch(patch.object(
            sys.modules[self.package_name + ".wowiport"], "WowiportService", return_value=self.remote,
        ))
        return former

    def test_commission_keeps_its_contact_and_bills_the_selected_former_tenant(self):
        former = self.configure_tenant_charge_contracts()
        review_service = importlib.import_module(self.package_name + ".commission_followup")
        self.start_patch(patch.object(self.admin, "review_commission", review_service.review_commission))
        url = "/admin/commission-objects/tenant-charge-recipients"
        response = self.client.get(url, query_string={
            "object_type": "license_agreement", "object_id": self.agreement.id, "commission_type_id": 100,
        })
        self.assertEqual(response.status_code, 200, response.data[:350])
        self.assertTrue(response.json["current_available"])
        self.assertEqual([item["value"] for item in response.json["items"] if item["suggested"]], ["9000:8101"])
        self.assertTrue(any("Künftiger Mieter" in item["label"] for item in response.json["items"]))
        self.assertTrue(all("street" not in item for item in response.json["items"]))

        commission = self.post_order(self.form((), tenant_charge_current="on", notify_tenants="true"))
        self.assertEqual(commission.license_agreement_id, self.agreement.erp_id)
        self.assertEqual(commission.tenant_name, self.tenant.display_name)
        self.assertTrue(commission.notify_tenants)
        self.assertEqual(commission.tenant_charge_target["person_id"], self.party["person_id"])
        commission.local_status = "tenant_charge_review"
        self.db.session.commit()
        review_url = f"/admin/commissions/{commission.id}/tenant-charge"
        page = self.client.get(review_url)
        self.assertEqual(page.status_code, 200, page.data[:350])
        self.assertIn("Ehemalige Mieterin", page.get_data(as_text=True))
        self.client.post(review_url, data={"tenant_charge_recipient": "9999:8101"})
        self.assertEqual(commission.tenant_charge_target["person_id"], self.party["person_id"])
        self.client.post(review_url, data={"tenant_charge_recipient": "9000:8101"})
        self.assertEqual(commission.tenant_charge_target, {
            "agreement_id": 9000, "agreement_number": "MV-ALT", "person_id": 8101, "name": former["name"],
        })

        with patch.dict(former, {"street": ""}):
            with self.assertRaisesRegex(ValueError, "vollständige Postanschrift"):
                self.invoice_service.create_commission_invoice_draft([commission], self.creator)
        self.assertEqual(self.models.Invoice.query.count(), 0)
        self.assertEqual(commission.local_status, "tenant_charge_review")
        response = self.client.post("/admin/invoices/new", data={
            "mode": "selected", "commission_ids": str(commission.id),
        })
        self.assertEqual(response.status_code, 302, response.data[:350])
        invoice = self.models.Invoice.query.one()
        self.assertEqual(invoice.company_code_id, self.issuer.id)
        self.assertEqual(invoice.recipient_name, self.company.seller_name)
        self.assertEqual(invoice.billing_contract_number, "MV-ALT")
        self.assertEqual(invoice.tenant_billing_reference["tenant_address"]["street"], former["street"])
        self.assertEqual(invoice.tenant_billing_reference["person_erp_id"], former["person_id"])
        self.assertFalse(invoice.can_edit_agreement)
        letter = importlib.import_module(self.package_name + ".tenant_charge_letter")
        self.assertEqual(letter.tenant_letter_recipients(invoice, self.remote)[0]["name"], former["name"])
        self.client.post(review_url, data={"tenant_charge_recipient": "current"})
        self.assertEqual(commission.tenant_charge_target["person_id"], former["person_id"])

    def test_offer_keeps_quoted_positions_when_tenant_charge_waits_for_office_review(self):
        former = self.configure_tenant_charge_contracts()
        commission = self.post_order(self.form((), tenant_charge_recipient="9000:8101"))
        commission.local_status = "tenant_charge_review"
        offer = self.draft("offer")
        offer.commission = commission
        offer.status = "in_progress"
        self.db.session.commit()

        self.offer_service.sync_offer_from_commission(commission)
        self.assertEqual(offer.status, "review_required")
        self.assertIsNone(offer.invoice)
        invoice = self.invoice_service.create_commission_invoice_draft([commission], self.creator)
        self.assertEqual(offer.invoice, invoice)
        self.assertEqual(offer.status, "ready_for_invoice")
        self.assertEqual(invoice.tenant_billing_reference["person_erp_id"], former["person_id"])
        self.assertEqual(invoice.positions[0].unit_price_net, offer.positions[0].unit_price_net)

    def test_legacy_direct_draft_with_routed_contract_cannot_be_finalized(self):
        invoice = self.draft("invoice")
        invoice.recipient_license_agreement_erp_id = self.agreement.erp_id
        with self.assertRaisesRegex(self.invoice_service.InvoiceFinalizationError, "Firmenrechnung"):
            self.invoice_service.finalize_invoice(invoice, self.creator)
        self.assertIsNone(invoice.invoice_number)

    def test_remove_positions_across_invoice_flows_and_sources_including_the_last_one(self):
        self.start_patch(patch.object(self.admin, "work_report_counts", return_value={}))
        free = self.invoice_service.create_free_invoice_draft(self.issuer, self.creator)
        routed = self.invoice_service.create_agreement_invoice_draft(self.agreement, self.party, self.creator)
        commission = self.post_order()
        commission.local_status = self.models.CommissionLocalStatus.TENANT_CHARGE_REVIEW.value
        self.db.session.commit()
        from_commission = self.invoice_service.create_commission_invoice_draft([commission], self.creator)
        offer = self.draft("offer")
        offer.commission = commission
        offer.license_agreement_erp_id = self.agreement.erp_id
        offer.recipient_name = self.party["name"]
        from_offer = self.invoice_service.create_offer_invoice_draft(offer)
        self.company.tenant_billing_issuer = None
        self.company.document_creation_disabled = False
        self.db.session.flush()
        direct = self.invoice_service.create_agreement_invoice_draft(self.agreement, self.party, self.creator)

        material = self.models.InventoryItem(item_number="L-01", name="Lagerartikel", unit="Stk")
        article = self.models.LocalCatalogItem(item_number="K-01", name="Katalogartikel")
        self.db.session.add_all([material, article])
        self.db.session.flush()
        rows = [
            {"source_type": source, "description": f"Testposition {source}", "quantity": 1, "unit_price_net": 10, "vat_rate": 19, **reference}
            for source, reference in (
                ("time", {"source_data": {"source_key": "zeit-1"}}),
                ("material", {"inventory_item_id": material.id}),
                ("catalog", {"task_template_id": self.task.id}),
                ("local_catalog", {"local_catalog_item_id": article.id}),
                ("free", {}),
            )
        ]
        fields = (
            "source_type", "description", "quantity", "unit", "unit_price_net", "vat_rate",
            "task_template_id", "inventory_item_id", "local_catalog_item_id", "article_number",
        )
        for name, invoice in (("free", free), ("direct", direct), ("routed", routed), ("commission", from_commission), ("offer", from_offer)):
            with self.subTest(flow=name):
                self.invoice_service.replace_invoice_positions(invoice, rows)
                self.db.session.commit()
                if name == "offer":
                    self.assertEqual(self.client.post(f"/admin/local-catalog/{article.id}/delete").status_code, 302)
                    self.db.session.expire_all()
                    self.assertIsNone(invoice.positions[3].local_catalog_item_id)
                url = f"/admin/invoices/{invoice.id}"
                page = self.client.get(url)
                self.assertEqual(page.status_code, 200)
                self.assertEqual(page.data.count(b'title="Position entfernen"'), len(rows) + 1)
                commission_ids = [link.commission_id for link in invoice.commission_links]
                values = self.draft_form()
                values.update(
                    recipient_license_agreement_erp_id=str(invoice.recipient_license_agreement_erp_id or ""),
                    saved_contract_erp_id=str((invoice.tenant_billing_reference or {}).get("agreement_erp_id") or invoice.recipient_license_agreement_erp_id or ""),
                )
                while invoice.positions:
                    remaining = invoice.positions[1:]
                    sources = [position.source_type for position in remaining]
                    for field in fields:
                        values[f"position_{field}"] = [str(getattr(position, field) or "") for position in remaining]
                    values["position_source_data"] = [json.dumps(position.source_data or {}) for position in remaining]
                    response = self.client.post(url, data=values)
                    self.assertEqual(response.status_code, 302, response.data[:1000])
                    self.db.session.refresh(invoice)
                    self.assertEqual([position.source_type for position in invoice.positions], sources)
                self.assertEqual(invoice.total_gross, 0)
                self.assertEqual([link.commission_id for link in invoice.commission_links], commission_ids)

        self.invoice_service.replace_invoice_positions(routed, rows[:1])
        routed.status = "finalized"
        self.db.session.commit()
        url = f"/admin/invoices/{routed.id}"
        self.assertNotIn(b"Position entfernen", self.client.get(url).data)
        self.client.post(url, data={})
        self.db.session.refresh(routed)
        self.assertEqual(len(routed.positions), 1)

    def test_positions_can_be_removed_when_an_existing_contract_needs_correction(self):
        self.start_patch(patch.object(self.admin, "work_report_counts", return_value={}))
        invoice = self.draft("invoice")
        invoice.recipient_license_agreement_erp_id = self.agreement.erp_id
        invoice.recipient_license_agreement_idnum = self.agreement.id_num
        self.db.session.commit()
        url = f"/admin/invoices/{invoice.id}"
        form = {"recipient_license_agreement_erp_id": self.agreement.erp_id, "saved_contract_erp_id": self.agreement.erp_id}
        self.assertEqual(self.client.post(url, data=form).status_code, 302)
        self.db.session.refresh(invoice)
        self.assertEqual(invoice.positions, [])
        self.assertEqual(invoice.recipient_license_agreement_erp_id, self.agreement.erp_id)
        self.client.post(url + "/finalize", data={**form, "save_draft": "1", "position_description": "Test"})
        with self.client.session_transaction() as session:
            self.assertIn("Rechnung mit Mietvertrag", session.get("_flashes", [])[-1][1])
        self.assertEqual(invoice.status, "draft")

    def test_tenant_charge_purpose_prefills_the_invoice_issuer_and_gross_total(self):
        invoice = self.create_invoice()
        form = self.draft_form()
        form.update(save_draft="1", service_date_from=form["invoice_date"])
        invoice_url = f"/admin/invoices/{invoice.id}"
        self.client.post(invoice_url + "/finalize", data=form)
        self.db.session.refresh(invoice)
        self.assertEqual(invoice.status, "finalized")

        agreement, mode = self.api_rows()
        self.remote.read_all_rows.side_effect = lambda endpoint, *_args, **_kwargs: [agreement] if endpoint.endswith("LicenseAgreements") else [mode]
        url = invoice_url + "/tenant-charge"

        def purpose_value():
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            return etree.HTML(response.get_data(as_text=True)).xpath("string(//textarea[@name='purpose'])")

        self.assertIn("Für die ausgeführten Arbeiten berechnen wir Ihnen die Kosten", purpose_value())
        definition = self.settings.SettingKeys.INVOICE_TENANT_CHARGE_PURPOSE_TEMPLATE
        self.settings.settings.set(definition, "Rechnung von {invoice_issuer}\nBruttobetrag {gross_amount}\n{work_description}")
        self.issuer.seller_name = "Späterer Firmenname"
        self.db.session.commit()
        expected = f"Rechnung von Testbetrieb GmbH\nBruttobetrag 29,75 EUR\n{form['position_description']}"
        self.assertEqual(purpose_value(), expected)

        purpose = expected + "\nIndividuell ergänzter Anlass."
        response = self.client.post(url, data={
            "purpose": purpose, "revision": 0, "payment_method": "sepa_debit",
            "person_id": self.party["person_id"], "due_date": (date.today() + timedelta(days=10)).isoformat(),
        })
        self.assertEqual(response.location, url)
        self.db.session.refresh(invoice)
        self.assertEqual(invoice.tenant_charge_letter["purpose"], purpose)
        self.settings.settings.set(definition, "Neue Vorlage mit {gross_amount}")
        self.db.session.commit()
        self.assertEqual(purpose_value(), purpose)

        archive_count = self.archive.call_count
        pdf = self.client.get(url + "/document")
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(self.archive.call_count, archive_count)
        downloaded = PdfReader(BytesIO(pdf.data))
        archived = PdfReader(BytesIO(self.archive.call_args.args[4]))
        self.assertEqual(
            [page.extract_text() for page in archived.pages],
            [page.extract_text() for page in downloaded.pages],
        )
        self.assertEqual(archived.attachments["factur-x.xml"], downloaded.attachments["factur-x.xml"])
        text = downloaded.pages[0].extract_text()
        for marker in ("Rechnung von Testbetrieb GmbH", "Bruttobetrag 29,75 EUR", "Individuell ergänzter Anlass."):
            self.assertIn(marker, text)

    def test_tenant_charge_closing_can_be_changed_per_letter_and_in_the_settings(self):
        invoice = self.create_invoice()
        form = self.draft_form()
        form.update(save_draft="1", service_date_from=form["invoice_date"])
        invoice_url = f"/admin/invoices/{invoice.id}"
        self.client.post(invoice_url + "/finalize", data=form)
        self.db.session.refresh(invoice)
        self.assertEqual(invoice.status, "finalized")

        definition = self.settings.SettingKeys.INVOICE_TENANT_CHARGE_CLOSING_TEMPLATE
        agreement, mode = self.api_rows()
        self.remote.read_all_rows.side_effect = lambda endpoint, *_args, **_kwargs: [agreement] if endpoint.endswith("LicenseAgreements") else [mode]
        url = invoice_url + "/tenant-charge"

        def closing_value():
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            return etree.HTML(response.get_data(as_text=True)).xpath("string(//textarea[@name='closing'])")

        self.assertEqual(closing_value(), "Mit freundlichen Grüßen\nTestgenossenschaft eG\n{sig}")
        self.settings.settings.set(definition, "Beste Grüße\n{seller_name}\n{sig}")
        self.db.session.commit()
        self.assertEqual(closing_value(), "Beste Grüße\nTestgenossenschaft eG\n{sig}")
        closing = "Viele Grüße\n<b>Ihr Serviceteam</b>\nTestgenossenschaft eG\n{sig}"
        data = {
            "purpose": "Reparatur", "person_id": self.party["person_id"], "payment_method": "sepa_debit",
            "due_date": (date.today() + timedelta(days=10)).isoformat(),
        }
        self.assertEqual(self.client.post(url, data={**data, "revision": 0, "closing": closing}).location, url)
        self.settings.settings.set(definition, "Neuer Standard")
        self.db.session.commit()
        self.assertEqual(closing_value(), closing)
        pdf = self.client.get(url + "/document")
        self.assertEqual(pdf.status_code, 200)
        for content in (pdf.data, self.archive.call_args.args[4]):
            reader = PdfReader(BytesIO(content))
            text = reader.pages[0].extract_text()
            self.assertIn("Viele Grüße", text)
            self.assertIn("<b>Ihr Serviceteam</b>", text)
            self.assertIn("Testgenossenschaft eG\n{sig}", text)
            self.assertIn("factur-x.xml", reader.attachments)

        self.assertEqual(self.client.post(url, data={**data, "revision": 1}).location, url)
        self.assertEqual(closing_value(), closing)
        legacy_letter = dict(invoice.tenant_charge_letter)
        legacy_letter.pop("closing")
        invoice.tenant_charge_letter = legacy_letter
        self.db.session.commit()
        self.assertEqual(closing_value(), "Mit freundlichen Grüßen\nTestgenossenschaft eG")
        self.assertEqual(self.client.post(url, data={**data, "revision": 2, "closing": ""}).location, url)
        self.assertEqual(closing_value(), "")

    def test_letter_without_mandate_uses_the_contract_iban(self):
        invoice = self.create_invoice()
        self.company.seller_iban = DEBIT_IBAN
        agreement, mode = self.api_rows("transfer")
        self.remote.read_all_rows.side_effect = [[agreement], [mode]]
        letters = importlib.import_module(self.package_name + ".tenant_charge_letter")
        payment = letters.tenant_letter_payment(invoice, self.company, self.remote)
        self.assertEqual(payment["method"], "bank_transfer")
        self.assertEqual(payment["iban"], TRANSFER_IBAN)
        self.assertNotEqual(payment["iban"], self.company.seller_iban)
        agreement["Banking"]["VirtualIBAN"] = None
        self.remote.read_all_rows.side_effect = [[agreement], [mode]]
        with self.assertRaisesRegex(ValueError, "keine IBAN für Überweisungen"):
            letters.tenant_letter_payment(invoice, self.company, self.remote)

    def test_letter_pdf_uses_current_payment_data_and_rejects_an_outdated_snapshot(self):
        self.start_patch(patch.object(self.admin, "WorkReportDocumentError", RuntimeError))
        invoice = self.create_invoice()
        form = self.draft_form()
        form.update(save_draft="1", service_date_from=form["invoice_date"])
        self.client.post(f"/admin/invoices/{invoice.id}/finalize", data=form)
        self.db.session.refresh(invoice)
        self.assertEqual(invoice.status, "finalized")

        agreement, mode = self.api_rows()
        mode["SepaMandate"]["CreditorIdentifier"] = self.company.seller_creditor_id
        self.company.seller_iban = DEBIT_IBAN
        self.db.session.commit()
        self.remote.read_all_rows.side_effect = lambda endpoint, *_args, **_kwargs: [agreement] if endpoint.endswith("LicenseAgreements") else [mode]

        url = f"/admin/invoices/{invoice.id}/tenant-charge"
        due_date = date.today() + timedelta(days=10)
        data = {"person_id": self.party["person_id"], "purpose": "Reparatur", "due_date": due_date.isoformat()}
        response = self.client.post(url, data={**data, "revision": 0, "payment_method": "sepa_debit"})
        self.assertEqual(response.status_code, 302)
        pdf = self.client.get(url + "/document")
        self.assertEqual(pdf.status_code, 200)
        letter = PdfReader(BytesIO(pdf.data)).pages[0].extract_text()
        for value in ("Mandatsreferenz: MANDAT-1", self.company.seller_creditor_id, due_date.strftime("%d.%m.%Y"), "29,75", "DE12 **** **** **** 9890"):
            self.assertIn(value, letter)
        self.assertNotIn(DEBIT_IBAN, letter)
        self.assertNotIn(TRANSFER_IBAN, letter)

        mode["ActiveTo"] = "2020-12-31"
        self.assertEqual(self.client.get(url + "/document").location, url)
        response = self.client.post(url, data={**data, "revision": 1, "payment_method": "bank_transfer"})
        self.assertEqual(response.status_code, 302)
        pdf = self.client.get(url + "/document")
        self.assertEqual(pdf.status_code, 200)
        letter = PdfReader(BytesIO(pdf.data)).pages[0].extract_text()
        self.assertIn("Bitte überweisen", letter)
        self.assertIn(TRANSFER_IBAN, letter)
        self.assertIn("COBADEFFXXX", letter)
        self.assertNotIn(DEBIT_IBAN, letter)
        self.assertNotIn("Mandatsreferenz", letter)

    def test_final_pdf_and_xml_keep_the_buyer_separate_and_the_letter_announces_sepa(self):
        invoice = self.create_invoice()
        invoice.cover_letter_title = "Altes Rechnungsanschreiben"
        invoice.cover_letter_text = "An {recipient_name}, mit freundlichen Grüßen, {seller_name}"
        self.admin.store_company_code_stationery(
            self.company, FileStorage(stream=BytesIO(self.pdf("BRIEFBOGEN DER EG")), filename="eg.pdf"),
            attribute="cover_letter_stationery_filename",
        )
        self.admin.store_company_code_stationery(
            self.issuer, FileStorage(stream=BytesIO(self.pdf("BRIEFBOGEN DER GMBH")), filename="gmbh.pdf"),
            attribute="cover_letter_stationery_filename",
        )
        self.assertEqual(self.upload(invoice, [(self.pdf("ARBEITSBERICHT"), "bericht.pdf")]).status_code, 201)
        form = self.draft_form()
        form.update(save_draft="1", service_date_from=form["invoice_date"],
                    recipient_license_agreement_erp_id=str(self.agreement.erp_id),
                    insurance_claim_number="  VS-2026-74  ",
                    payment_information_data='{"method":"sepa_debit"}')
        response = self.client.post(f"/admin/invoices/{invoice.id}/finalize", data=form)
        self.assertEqual(response.status_code, 302)
        self.db.session.refresh(invoice)
        self.assertEqual(invoice.status, "finalized")
        self.assertTrue(invoice.invoice_number.startswith("GMBH-"))
        self.assertIsNone(invoice.payment_information)
        self.assertEqual(invoice.insurance_claim_number, "VS-2026-74")

        original = self.delivery.final_invoice_pdf(invoice)
        reader = PdfReader(BytesIO(original))
        invoice_text = reader.pages[0].extract_text()
        self.assertIn("Testgenossenschaft eG", invoice_text)
        self.assertIn(
            f"Leistungsbezug\n{self.party['name']}\n{self.unit.address}\nMietvertrag MV-COMMON\nVersicherungsfall-Nr. VS-2026-74",
            invoice_text,
        )
        self.assertNotIn("Bitte überweisen", invoice_text)
        self.assertNotIn("Fällig am", invoice_text)
        xml = etree.fromstring(reader.attachments["factur-x.xml"][0])
        self.assertEqual(xml.xpath("string(//*[local-name()='BuyerTradeParty']/*[local-name()='Name'])"), "Testgenossenschaft eG")
        note = xml.xpath("string(//*[local-name()='IncludedNote']/*[local-name()='Content'])")
        self.assertIn("MV-COMMON", note)
        self.assertIn("Versicherungsfall-Nr. VS-2026-74", note)
        self.assertFalse(xml.xpath("//*[local-name()='SpecifiedTradeSettlementPaymentMeans']"))

        agreement, mode = self.api_rows()
        self.remote.read_all_rows.side_effect = lambda endpoint, *_args, **_kwargs: [agreement] if endpoint.endswith("LicenseAgreements") else [mode]
        invoice_url = f"/admin/invoices/{invoice.id}"
        url = invoice_url + "/tenant-charge"
        page = self.client.get(invoice_url)
        self.assertIn(b"Rechnung abschlie", page.data)
        self.assertNotIn(b"Mieteranschreiben erstellen", page.data)
        self.assertNotIn(b"Mieteranschreiben bearbeiten", page.data)
        self.assertNotIn(b"invoiceCoverLetterModal", page.data)
        self.assertEqual(self.client.get(invoice_url + "/correspondence").status_code, 404)
        charge_page = self.client.get(url)
        self.assertIn(b"Einzugstermin", charge_page.data)
        self.assertNotIn(b"Mieterunterlagen als PDF", charge_page.data)
        self.assertIn(b"<strong>Absender</strong> Testgenossenschaft eG", charge_page.data)
        self.assertIn(b"Postweg 2", charge_page.data)
        response = self.client.post(url, data={
            "revision": 0, "person_id": 5000, "payment_method": "sepa_debit",
            "purpose": "Austausch eines Duschkabinen-Seitenteils",
            "caseworker_name": "Sachbearbeitung Test", "due_date": (date.today() + timedelta(days=10)).isoformat(),
        })
        self.assertEqual(response.status_code, 302, response.data[:1000])
        self.assertNotIn(b"PDF mit Mieteranschreiben aufrufen", self.client.get(invoice_url).data)
        self.assertNotIn(b"Mieterunterlagen als PDF", self.client.get(url).data)
        package = self.client.get(url + "/document")
        self.assertEqual(package.status_code, 200, package.data[:1000])
        pages = PdfReader(BytesIO(package.data)).pages
        self.assertEqual(len(pages), 3)
        letter = pages[0].extract_text()
        for value in ("BRIEFBOGEN DER EG", "Mieterbelastung", "Testmieterin", "Postweg 2", "SEPA-Mandats", "29,75"):
            self.assertIn(value, letter)
        self.assertNotIn("BRIEFBOGEN DER GMBH", letter)
        self.assertIn("Testgenossenschaft eG", pages[1].extract_text())
        self.assertIn("ARBEITSBERICHT", pages[2].extract_text())
        self.assertEqual(self.delivery.final_invoice_pdf(invoice), original)

        delivery = self.client.get(invoice_url + "/delivery/recipients")
        self.assertEqual(delivery.json["items"], [{"id": "company", "name": "Testgenossenschaft eG"}])
        email = BytesParser(policy=policy.default).parsebytes(
            self.client.get(invoice_url + "/delivery/draft.eml").data,
        )
        self.assertEqual(email["To"], "rechnungen@example.invalid")
        self.assertEqual(next(email.iter_attachments()).get_payload(decode=True), original)

        self.assertEqual(self.queue(invoice, official_id="company").status_code, 202)
        queued = self.models.WorkflowNotification.query.filter_by(kind="invoice_delivery").one()
        self.assertNotIn("correspondence_filename", queued.payload)
        legacy_filename = "invoice_documents/correspondence/altes-anschreiben.pdf"
        legacy_pdf = self.pdf("ALTES RECHNUNGSANSCHREIBEN")
        path = self.invoice_document.resolve_invoice_document_path(legacy_filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(legacy_pdf)
        queued.payload = {**queued.payload, "correspondence_filename": legacy_filename, "correspondence_sha256": sha256(legacy_pdf).hexdigest()}
        self.db.session.commit()
        self.workflow.process_workflow_notification_batch()
        sent = self.mail.call_args.args[0]
        self.assertEqual(sent["To"], "rechnungen@example.invalid")
        self.assertEqual(next(sent.iter_attachments()).get_payload(decode=True), original)

        self.login(self.administrator)
        preview = self.client.post("/admin/invoice-cover-letter-layout/preview", data={
            "invoice_id": invoice.id,
            "html": "<h1>Absender {{ seller_name }}</h1><p>{{ sender_line }}</p><p>An {{ recipient_name }}</p><p>{{ recipient_street }}</p>",
        })
        self.assertEqual(preview.status_code, 200, preview.data[:1000])
        text = PdfReader(BytesIO(preview.data)).pages[0].extract_text()
        self.assertIn("Absender Testgenossenschaft eG", text)
        self.assertIn("An Testmieterin", text)
        self.assertIn("Postweg 2", text)
        self.assertIn("BRIEFBOGEN DER EG", text)
        self.assertNotIn("Testbetrieb GmbH", text)
