from datetime import date, timedelta
from email import policy
from email.parser import BytesParser
from io import BytesIO
import importlib
import sys
import unittest

from lxml import etree
from pypdf import PdfReader
from werkzeug.datastructures import FileStorage

import test_invoice_delivery as fixtures
import test_invoice_payments as payment_fixtures
from test_invoice_payments import DEBIT_IBAN


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
    prepare_stationery = fixtures.InvoiceDeliveryTests.prepare_stationery
    api_rows = payment_fixtures.InvoicePaymentTests.api_rows

    @classmethod
    def setUpClass(cls):
        fixtures.InvoiceDeliveryTests.setUpClass.__func__(cls)
        cls.tables.append(cls.models.InvoiceBillingRule.__table__)
        for name in ("invoice_correspondence", "tenant_charge_letter"):
            sys.modules.pop(cls.package_name + "." + name, None)
        guard = sys.modules[cls.package_name + ".auth"].invoice_required
        for endpoint in ("invoice_create", "invoice_tenant_charge", "invoice_tenant_charge_document"):
            view = cls.app.view_functions["admin." + endpoint]
            cls.app.view_functions["admin." + endpoint] = guard(view.__wrapped__)

    def setUp(self):
        fixtures.InvoiceDeliveryTests.setUp(self)
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
        self.assertEqual(self.client.get(f"/admin/invoices/{invoice.id}/tenant-charge").status_code, 409)

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

    def test_legacy_direct_draft_with_routed_contract_cannot_be_finalized(self):
        invoice = self.draft("invoice")
        invoice.recipient_license_agreement_erp_id = self.agreement.erp_id
        with self.assertRaisesRegex(self.invoice_service.InvoiceFinalizationError, "Firmenrechnung"):
            self.invoice_service.finalize_invoice(invoice, self.creator)
        self.assertIsNone(invoice.invoice_number)

    def test_letter_without_mandate_uses_the_contract_company_bank_account(self):
        invoice = self.create_invoice()
        agreement, mode = self.api_rows("transfer")
        self.remote.read_all_rows.side_effect = [[agreement], [mode]]
        letters = importlib.import_module(self.package_name + ".tenant_charge_letter")
        payment = letters.tenant_letter_payment(invoice, self.company, self.remote)
        self.assertEqual(payment["method"], "bank_transfer")
        self.assertEqual(payment["iban"], self.company.seller_iban)
        self.assertNotEqual(payment["iban"], self.issuer.seller_iban)

    def test_final_pdf_and_xml_keep_the_buyer_separate_and_the_letter_announces_sepa(self):
        invoice = self.create_invoice()
        self.admin.store_company_code_stationery(
            self.company, FileStorage(stream=BytesIO(self.pdf("BRIEFBOGEN DER EG")), filename="eg.pdf"),
            attribute="cover_letter_stationery_filename",
        )
        self.assertEqual(self.upload(invoice, [(self.pdf("ARBEITSBERICHT"), "bericht.pdf")]).status_code, 201)
        form = self.draft_form()
        form.update(save_draft="1", service_date_from=form["invoice_date"],
                    recipient_license_agreement_erp_id=str(self.agreement.erp_id),
                    payment_information_data='{"method":"sepa_debit"}')
        response = self.client.post(f"/admin/invoices/{invoice.id}/finalize", data=form)
        self.assertEqual(response.status_code, 302)
        self.db.session.refresh(invoice)
        self.assertEqual(invoice.status, "finalized")
        self.assertTrue(invoice.invoice_number.startswith("GMBH-"))
        self.assertIsNone(invoice.payment_information)

        original = self.delivery.final_invoice_pdf(invoice)
        reader = PdfReader(BytesIO(original))
        self.assertIn("Testgenossenschaft eG", reader.pages[0].extract_text())
        self.assertIn("MV-COMMON", reader.pages[0].extract_text())
        self.assertNotIn("Bitte überweisen", reader.pages[0].extract_text())
        self.assertNotIn("Fällig am", reader.pages[0].extract_text())
        xml = etree.fromstring(reader.attachments["factur-x.xml"][0])
        self.assertEqual(xml.xpath("string(//*[local-name()='BuyerTradeParty']/*[local-name()='Name'])"), "Testgenossenschaft eG")
        self.assertIn("MV-COMMON", xml.xpath("string(//*[local-name()='IncludedNote']/*[local-name()='Content'])"))
        self.assertFalse(xml.xpath("//*[local-name()='SpecifiedTradeSettlementPaymentMeans']"))

        agreement, mode = self.api_rows()
        self.remote.read_all_rows.side_effect = lambda endpoint, *_args, **_kwargs: [agreement] if endpoint.endswith("LicenseAgreements") else [mode]
        url = f"/admin/invoices/{invoice.id}/tenant-charge"
        self.assertIn(b"Einzugstermin", self.client.get(url).data)
        response = self.client.post(url, data={
            "revision": 0, "person_id": 5000, "payment_method": "sepa_debit",
            "purpose": "Austausch eines Duschkabinen-Seitenteils",
            "caseworker_name": "Sachbearbeitung Test", "due_date": (date.today() + timedelta(days=10)).isoformat(),
        })
        self.assertEqual(response.status_code, 302, response.data[:1000])
        package = self.client.get(url + "/document")
        self.assertEqual(package.status_code, 200, package.data[:1000])
        pages = PdfReader(BytesIO(package.data)).pages
        self.assertEqual(len(pages), 3)
        letter = pages[0].extract_text()
        for value in ("BRIEFBOGEN DER EG", "Mieterbelastung", "Testmieterin", "SEPA-Mandats", "29,75"):
            self.assertIn(value, letter)
        self.assertIn("Testgenossenschaft eG", pages[1].extract_text())
        self.assertIn("ARBEITSBERICHT", pages[2].extract_text())
        self.assertEqual(self.delivery.final_invoice_pdf(invoice), original)

        delivery = self.client.get(f"/admin/invoices/{invoice.id}/delivery/recipients")
        self.assertEqual(delivery.json["items"], [{"id": "company", "name": "Testgenossenschaft eG"}])
        email = BytesParser(policy=policy.default).parsebytes(
            self.client.get(f"/admin/invoices/{invoice.id}/delivery/draft.eml").data,
        )
        self.assertEqual(email["To"], "rechnungen@example.invalid")
        self.assertEqual(next(email.iter_attachments()).get_payload(decode=True), original)
