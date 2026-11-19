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
        for endpoint in ("invoice_edit", "invoice_assign_agreement"):
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
        return self.client.post(f"/admin/invoices/{invoice.id}/agreement", data={
            "agreement_id": self.agreement.erp_id, "person_id": person_id,
        })

    def test_free_draft_is_saved_then_routed_to_the_company_invoice_in_place(self):
        invoice = self.create_free_invoice(self.company)
        invoice.payment_reference = "Alter Verwendungszweck"
        self.db.session.commit()
        invoice_url = f"/admin/invoices/{invoice.id}"
        assignment_url = invoice_url + "/agreement"
        self.assertIn(b"Entwurf speichern und Mietvertrag zuordnen", self.client.get(invoice_url).data)
        self.assertEqual(self.upload(invoice, [(self.pdf("BELEGANLAGE"), "beleg.pdf")]).status_code, 201)

        values = self.draft_form()
        values.update(assign_agreement="1", saved_contract_erp_id="", service_date_from=values["invoice_date"],
                      due_date="2026-12-04", introductory_text="Gespeicherter Rechnungstext")
        response = self.client.post(invoice_url, data=values)
        self.assertEqual(response.location, assignment_url)
        self.db.session.refresh(invoice)
        self.assertEqual(invoice.recipient_name, "Neue freie Empfängerin")
        self.assertEqual(invoice.payment_information["iban"], self.company.seller_iban)
        position_id = invoice.positions[0].id
        attachment_id = invoice.pdf_attachments[0].id
        due_date = invoice.due_date

        result = self.client.get(assignment_url, query_string={"q": "MV-COMMON"})
        self.assertIn(f"{assignment_url}?q=MV-COMMON&amp;agreement_id={self.agreement.erp_id}".encode(), result.data)
        selection = self.client.get(assignment_url, query_string={"agreement_id": self.agreement.erp_id})
        for value in ("Testbetrieb GmbH", "Testgenossenschaft eG", "Mietvertrag übernehmen"):
            self.assertIn(value, selection.get_data(as_text=True))
        self.assertEqual(self.assign_agreement(invoice).location, invoice_url)

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
        self.assertEqual(invoice.positions[0].id, position_id)
        self.assertEqual(invoice.pdf_attachments[0].id, attachment_id)
        self.assertEqual(invoice.due_date, due_date)
        self.assertEqual(invoice.introductory_text, "Gespeicherter Rechnungstext")
        self.assertIsNone(invoice.payment_information)
        self.assertIsNone(invoice.payment_reference)
        self.assertIsNone(invoice.invoice_number)
        self.assertNotIn(b"Entwurf speichern und Mietvertrag zuordnen", self.client.get(invoice_url).data)

        self.client.post(invoice_url + "/finalize", data={**values, "save_draft": "1"})
        self.db.session.refresh(invoice)
        self.assertEqual(invoice.status, "draft")
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
        self.assertEqual(self.assign_agreement(invoice).status_code, 302)

        self.db.session.refresh(invoice)
        self.assertEqual(invoice.company_code_id, self.company.id)
        self.assertEqual(invoice.recipient_person_erp_id, self.party["person_id"])
        self.assertEqual(invoice.recipient_name, self.party["name"])
        self.assertEqual(invoice.recipient_license_agreement_erp_id, self.agreement.erp_id)
        self.assertIsNone(invoice.tenant_billing_reference)
        self.assertIsNone(invoice.payment_information)
        invoice_url = f"/admin/invoices/{invoice.id}"
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

        response = self.client.get(f"/admin/invoices/{invoice.id}/agreement", query_string={"q": "MV-COMMON"})
        self.assertIn(f"agreement_id={self.agreement.erp_id}".encode(), response.data)
        self.assertEqual(self.assign_agreement(invoice).status_code, 302)
        self.db.session.refresh(invoice)
        self.assertEqual(invoice.company_code_id, self.company.id)
        self.assertEqual(invoice.billing_contract_erp_id, self.agreement.erp_id)

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
