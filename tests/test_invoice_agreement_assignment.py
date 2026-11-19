from io import BytesIO
import sys
import unittest
from unittest.mock import patch

from pypdf import PdfReader

import test_invoice_tenant_billing as fixtures


class InvoiceAgreementAssignmentTests(unittest.TestCase):
    login = fixtures.InvoiceTenantBillingTests.login
    start_patch = fixtures.InvoiceTenantBillingTests.start_patch
    image = fixtures.InvoiceTenantBillingTests.image
    form = fixtures.InvoiceTenantBillingTests.form
    post_order = fixtures.InvoiceTenantBillingTests.post_order
    draft_form = fixtures.InvoiceTenantBillingTests.draft_form
    pdf = fixtures.InvoiceTenantBillingTests.pdf
    upload = fixtures.InvoiceTenantBillingTests.upload
    prepare_stationery = fixtures.InvoiceTenantBillingTests.prepare_stationery
    api_rows = fixtures.InvoiceTenantBillingTests.api_rows

    @classmethod
    def setUpClass(cls):
        fixtures.InvoiceTenantBillingTests.setUpClass.__func__(cls)
        guard = sys.modules[cls.package_name + ".auth"].invoice_required
        for endpoint in ("invoice_edit", "invoice_recipient_search", "invoice_recipient_candidates"):
            view = cls.app.view_functions["admin." + endpoint]
            cls.app.view_functions["admin." + endpoint] = guard(view.__wrapped__)

    def setUp(self):
        fixtures.InvoiceTenantBillingTests.setUp(self)
        self.start_patch(patch.object(self.admin, "work_report_counts", return_value={}))

    def create_free_invoice(self, company):
        company.document_creation_disabled = False
        self.db.session.commit()
        response = self.client.post("/admin/invoices/new", data={"mode": "free", "company_code_id": company.id})
        self.assertEqual(response.status_code, 302)
        return self.models.Invoice.query.one()

    def assign_agreement(self, invoice, person_id=5000):
        return self.client.post(f"/admin/invoices/{invoice.id}", data=self.assignment_values(person_id))

    def assignment_values(self, person_id=5000):
        return {
            "saved_contract_erp_id": "", "pending_agreement_erp_id": self.agreement.erp_id,
            "pending_agreement_person_id": person_id,
        }

    def test_normal_save_routes_a_free_draft_to_the_company_invoice_in_place(self):
        invoice = self.create_free_invoice(self.company)
        invoice.payment_reference = "Alter Verwendungszweck"
        self.db.session.commit()
        invoice_url = f"/admin/invoices/{invoice.id}"
        html = self.client.get(invoice_url).get_data(as_text=True)
        self.assertIn('id="tenantSearch"', html)
        self.assertRegex(html, r'id="selectedAgreement">[^<]*</div>\s+<p[^>]+id="invoiceAgreementSaveNotice"[^>]+hidden>')
        self.assertNotIn('name="assign_agreement"', html)
        self.assertEqual(self.upload(invoice, [(self.pdf("BELEGANLAGE"), "beleg.pdf")]).status_code, 201)
        attachment_id = invoice.pdf_attachments[0].id

        values = self.draft_form()
        values.update(service_date_from=values["invoice_date"],
                      due_date="2026-12-04", introductory_text="Gespeicherter Rechnungstext")
        response = self.client.post(invoice_url, data=values)
        self.assertEqual(response.location, invoice_url)
        self.db.session.refresh(invoice)
        self.assertEqual(invoice.recipient_name, "Neue freie Empfängerin")
        self.assertEqual(invoice.payment_information["iban"], self.company.seller_iban)
        due_date = invoice.due_date

        result = self.client.get(invoice_url + "/recipients/search", query_string={"q": "MV-COMMON"})
        self.assertEqual(result.json["items"][0]["license_agreement"]["erp_id"], self.agreement.erp_id)
        selection = self.client.get(invoice_url + "/recipient-candidates", query_string={"license_agreement_id": self.agreement.erp_id})
        self.assertEqual(selection.json["assignment"], {"issuer_name": self.issuer.seller_name, "recipient_name": self.company.seller_name})
        self.assertEqual(selection.json["items"], [self.party])
        self.assertIsNone(invoice.billing_contract_erp_id)

        values.update(self.assignment_values())
        values["position_description"] = "Bearbeitete Leistung"
        preview = self.client.post(invoice_url + "/preview", data=values)
        self.assertEqual(preview.status_code, 200, preview.data[:500])
        self.assertIn("Testgenossenschaft eG", PdfReader(BytesIO(preview.data)).pages[0].extract_text())
        self.db.session.refresh(invoice)
        self.assertIsNone(invoice.billing_contract_erp_id)
        self.assertNotEqual(invoice.positions[0].description, "Bearbeitete Leistung")

        self.client.post(invoice_url + "/finalize", data={**values, "save_draft": "1"})
        self.db.session.refresh(invoice)
        self.assertEqual(invoice.status, "draft")
        self.assertIsNone(invoice.billing_contract_erp_id)
        self.assertEqual(self.client.post(invoice_url, data=values).location, invoice_url)

        self.db.session.refresh(invoice)
        self.assertEqual(self.models.Invoice.query.count(), 1)
        self.assertEqual(invoice.company_code_id, self.issuer.id)
        self.assertEqual(invoice.company_code_erp_id, self.issuer.erp_id)
        self.assertEqual(invoice.company_code_code, self.issuer.code)
        self.assertEqual(invoice.seller_name, self.issuer.seller_name)
        self.assertEqual(invoice.seller_street, self.issuer.seller_street)
        self.assertEqual(invoice.seller_iban, self.issuer.seller_iban)
        self.assertEqual(invoice.recipient_name, self.company.seller_name)
        self.assertEqual(invoice.recipient_email, self.company.tenant_billing_recipient_email)
        self.assertEqual(invoice.tenant_billing_reference["tenant_name"], self.party["name"])
        self.assertEqual(invoice.billing_contract_erp_id, self.agreement.erp_id)
        self.assertEqual([position.description for position in invoice.positions], ["Bearbeitete Leistung"])
        self.assertEqual(invoice.pdf_attachments[0].id, attachment_id)
        self.assertEqual(invoice.due_date, due_date)
        self.assertEqual(invoice.introductory_text, "Gespeicherter Rechnungstext")
        self.assertIsNone(invoice.payment_information)
        self.assertIsNone(invoice.payment_reference)
        self.assertIsNone(invoice.invoice_number)
        self.assertNotIn(b'id="pendingInvoiceAgreement"', self.client.get(invoice_url).data)

        self.client.post(invoice_url + "/finalize")
        self.db.session.refresh(invoice)
        self.assertEqual(invoice.status, "finalized")
        self.assertTrue(invoice.invoice_number.startswith("GMBH-"))
        self.assertEqual(invoice.payment_reference, invoice.invoice_number)
        pages = PdfReader(BytesIO(self.delivery.final_invoice_pdf(invoice))).pages
        self.assertIn("Testgenossenschaft eG", pages[0].extract_text())
        self.assertIn("MV-COMMON", pages[0].extract_text())
        self.assertIn("BELEGANLAGE", pages[1].extract_text())

    def test_direct_billing_uses_the_contract_book_and_reloads_its_payment_options(self):
        self.company.tenant_billing_issuer = None
        self.company.document_creation_disabled = False
        invoice = self.create_free_invoice(self.issuer)
        invoice.payment_information = {"method": "bank_transfer", "iban": self.issuer.seller_iban, "source": "manual"}
        self.db.session.commit()
        invoice_url = f"/admin/invoices/{invoice.id}"
        result = self.client.get(invoice_url + "/recipients/search", query_string={"q": "MV-COMMON"})
        self.assertEqual(result.json["items"][0]["company_code"]["erp_id"], self.company.erp_id)
        self.assertEqual(self.assign_agreement(invoice).status_code, 302)

        self.db.session.refresh(invoice)
        self.assertEqual(invoice.company_code_id, self.company.id)
        self.assertEqual(invoice.recipient_person_erp_id, self.party["person_id"])
        self.assertEqual(invoice.recipient_name, self.party["name"])
        self.assertEqual(invoice.recipient_license_agreement_erp_id, self.agreement.erp_id)
        self.assertIsNone(invoice.tenant_billing_reference)
        self.assertIsNone(invoice.payment_information)
        self.assertIn(b"unresolved", self.client.get(invoice_url).data)

        agreement, payment = self.api_rows()
        self.remote.read_all_rows.side_effect = [[agreement], [payment]]
        response = self.client.get(invoice_url + "/payment-information", query_string={"license_agreement_id": self.agreement.erp_id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["selected"]["method"], "sepa_debit")
        self.assertEqual(response.json["selected"]["creditor_id"], self.company.seller_creditor_id)
        stale = self.client.post(invoice_url, data={**self.draft_form(), "saved_contract_erp_id": ""})
        self.assertEqual(stale.status_code, 200)
        self.db.session.refresh(invoice)
        self.assertEqual(invoice.recipient_license_agreement_erp_id, self.agreement.erp_id)

    def test_unavailable_agreements_or_recipients_leave_the_draft_untouched(self):
        invoice = self.create_free_invoice(self.company)
        for vacancy, disabled, person_id in ((True, False, 5000), (False, True, 5000), (False, False, 9999)):
            with self.subTest(vacancy=vacancy, disabled=disabled, person_id=person_id):
                self.agreement.is_vacancy = vacancy
                self.issuer.document_creation_disabled = disabled
                self.db.session.commit()
                self.assertEqual(self.assign_agreement(invoice, person_id).status_code, 200)
                self.db.session.refresh(invoice)
                self.assertTrue(invoice.can_assign_agreement)
                self.assertEqual(invoice.company_code_id, self.company.id)
        invoice.status = "finalized"
        self.db.session.commit()
        self.assertEqual(self.assign_agreement(invoice).status_code, 302)
        self.db.session.refresh(invoice)
        self.assertIsNone(invoice.billing_contract_erp_id)
        self.assertEqual(invoice.company_code_id, self.company.id)

    def test_assignment_within_the_same_book_is_allowed_when_creation_is_disabled(self):
        self.company.tenant_billing_issuer = None
        invoice = self.create_free_invoice(self.company)
        self.company.document_creation_disabled = True
        self.db.session.commit()

        response = self.client.get(f"/admin/invoices/{invoice.id}/recipients/search", query_string={"q": "MV-COMMON"})
        self.assertEqual(response.json["items"][0]["license_agreement"]["erp_id"], self.agreement.erp_id)
        self.assertEqual(self.assign_agreement(invoice).status_code, 302)
        self.db.session.refresh(invoice)
        self.assertEqual(invoice.company_code_id, self.company.id)
        self.assertEqual(invoice.billing_contract_erp_id, self.agreement.erp_id)

    def test_multiple_contract_parties_must_be_selected_before_the_draft_is_saved(self):
        invoice = self.create_free_invoice(self.company)
        second_party = {**self.party, "person_id": 5001, "name": "Zweite Vertragspartei"}
        self.remote.get_invoice_recipient_candidates.return_value = [self.party, second_party]
        self.assertEqual(self.assign_agreement(invoice, person_id="").status_code, 200)
        self.db.session.refresh(invoice)
        self.assertIsNone(invoice.billing_contract_erp_id)
        self.assertEqual(self.assign_agreement(invoice, person_id=second_party["person_id"]).status_code, 302)
        self.db.session.refresh(invoice)
        self.assertEqual(invoice.tenant_billing_reference["tenant_name"], second_party["name"])

    def test_linked_order_must_match_the_contract_book(self):
        invoice = self.create_free_invoice(self.company)
        commission = self.post_order()
        commission.license_agreement_id = None
        commission.license_agreement_idnum = None
        commission.company_code_erp_id = self.issuer.erp_id
        invoice.commission_links.append(self.models.InvoiceCommission(commission=commission))
        self.db.session.commit()

        self.assertEqual(self.assign_agreement(invoice).status_code, 200)
        self.db.session.refresh(invoice)
        self.assertTrue(invoice.can_assign_agreement)
        self.assertEqual(invoice.company_code_id, self.company.id)

        commission.company_code_erp_id = self.company.erp_id
        self.db.session.commit()
        self.assertEqual(self.assign_agreement(invoice).status_code, 302)
        self.db.session.refresh(invoice)
        self.assertEqual(invoice.company_code_id, self.issuer.id)
        self.assertEqual(invoice.commission_links[0].commission_id, commission.id)
