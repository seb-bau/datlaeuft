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
    create_invoice = fixtures.InvoiceTenantBillingTests.create_invoice

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
        self.assertIsNone(invoice.due_date)
        self.assertEqual(invoice.introductory_text, "Gespeicherter Rechnungstext")
        self.assertIsNone(invoice.payment_information)
        self.assertIsNone(invoice.payment_reference)
        self.assertIsNone(invoice.invoice_number)
        self.assertIn(b'id="invoiceAgreementRemove"', self.client.get(invoice_url).data)

        self.client.post(invoice_url + "/finalize")
        self.db.session.refresh(invoice)
        self.assertEqual(invoice.status, "finalized")
        self.assertNotIn(b'id="pendingInvoiceAgreement"', self.client.get(invoice_url).data)
        self.assertTrue(invoice.invoice_number.startswith("GMBH-"))
        self.assertIsNone(invoice.payment_reference)
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
                self.assertIsNone(invoice.billing_contract_erp_id)
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
        self.assertIsNone(invoice.billing_contract_erp_id)
        self.assertEqual(invoice.company_code_id, self.company.id)

        commission.company_code_erp_id = self.company.erp_id
        self.db.session.commit()
        self.assertEqual(self.assign_agreement(invoice).status_code, 302)
        self.db.session.refresh(invoice)
        self.assertEqual(invoice.company_code_id, self.issuer.id)
        self.assertEqual(invoice.commission_links[0].commission_id, commission.id)

    def test_both_entry_points_find_full_and_partial_contract_numbers(self):
        self.agreement.id_num = "00138.001.002.04"
        self.db.session.add_all([
            self.models.WowiLicenseAgreement(erp_id=4008, id_num=self.agreement.id_num + "0", use_unit=self.unit),
            self.models.WowiLicenseAgreement(erp_id=4009, id_num=self.agreement.id_num, use_unit=self.unit, is_vacancy=True),
            self.models.WowiLicenseAgreement(erp_id=4010, id_num="MV 73", use_unit=self.unit),
        ])
        self.db.session.commit()
        invoice = self.create_invoice()
        url = f"/admin/invoices/{invoice.id}/recipients/search"
        for query, agreement_id in (
            ("00138.001.002.04", self.agreement.erp_id),
            ("002.04", self.agreement.erp_id),
            ("MV 73", 4010),
            ("Erika COMMON Straße 1", self.agreement.erp_id),
        ):
            with self.subTest(query=query):
                creation = self.client.get("/admin/invoices/new", query_string={"q": query})
                self.assertIn(f"agreement_id={agreement_id}".encode(), creation.data)
                self.assertNotIn(b"agreement_id=4009", creation.data)
                results = self.client.get(url, query_string={"q": query}).json["items"]
                self.assertEqual(results[0]["license_agreement"]["erp_id"], agreement_id)
                self.assertNotIn(4009, [result["license_agreement"]["erp_id"] for result in results])

    def test_changing_a_saved_contract_rebuilds_the_issuer_and_recipient_in_both_directions(self):
        direct_company = self.models.WowiCompanyCode(
            erp_id=98, name="Direktes Buchwerk", code="DIREKT", seller_name="Direkter Aussteller",
            seller_iban=self.company.seller_iban,
        )
        direct_economic = self.models.WowiEconomicUnit(erp_id=1002, company_code_erp_id=direct_company.erp_id)
        direct_building = self.models.WowiBuilding(erp_id=2003, economic_unit=direct_economic, address="Direktweg 1")
        direct_unit = self.models.WowiUseUnit(erp_id=3004, economic_unit=direct_economic, building=direct_building)
        direct_agreement = self.models.WowiLicenseAgreement(erp_id=4008, id_num="1000.001.01", use_unit=direct_unit)
        direct_party = {**self.party, "person_id": 5001, "name": "Direkte Mieterin"}
        self.db.session.add_all([direct_company, direct_agreement])
        self.db.session.commit()
        self.remote.get_invoice_recipient_candidates.side_effect = lambda agreement_id: [
            direct_party if agreement_id == direct_agreement.erp_id else self.party
        ]
        invoice = self.create_invoice()
        invoice.tenant_charge_letter = {"person_erp_id": self.party["person_id"]}
        self.db.session.commit()

        url = f"/admin/invoices/{invoice.id}"
        assignments = self.client.get(url + "/recipients/search", query_string={"q": direct_agreement.id_num})
        self.assertEqual(assignments.json["items"][0]["license_agreement"]["erp_id"], direct_agreement.erp_id)
        candidates = self.client.get(url + "/recipient-candidates", query_string={"license_agreement_id": direct_agreement.erp_id})
        self.assertEqual(candidates.json["assignment"]["issuer_name"], direct_company.seller_name)
        values = {**self.draft_form(), "saved_contract_erp_id": self.agreement.erp_id,
                  "pending_agreement_erp_id": direct_agreement.erp_id, "pending_agreement_person_id": direct_party["person_id"],
                  "insurance_claim_number": "VS-2026-42"}
        preview = self.client.post(url + "/preview", data=values)
        self.assertEqual(preview.status_code, 200, preview.data[:500])
        preview_text = PdfReader(BytesIO(preview.data)).pages[0].extract_text()
        self.assertIn(direct_party["name"], preview_text)
        self.assertIn("Versicherungsfall-Nr. VS-2026-42", preview_text)
        self.assertNotIn(self.party["name"], preview_text)
        self.db.session.refresh(invoice)
        self.assertEqual(invoice.company_code_id, self.issuer.id)

        self.assertEqual(self.client.post(url, data=values).location, url)
        self.db.session.refresh(invoice)
        self.assertEqual((invoice.company_code_id, invoice.company_code_erp_id, invoice.company_code_code),
                         (direct_company.id, direct_company.erp_id, direct_company.code))
        self.assertEqual(invoice.seller_name, direct_company.seller_name)
        self.assertEqual(invoice.billing_contract_number, direct_agreement.id_num)
        self.assertEqual(invoice.recipient_name, direct_party["name"])
        self.assertEqual(invoice.recipient_person_erp_id, direct_party["person_id"])
        self.assertIsNone(invoice.tenant_billing_reference)
        self.assertIsNone(invoice.tenant_charge_letter)
        self.assertEqual(invoice.insurance_claim_number, "VS-2026-42")
        stale = self.client.post(url, data={**values, "pending_agreement_erp_id": "", "remove_agreement": "1"})
        self.assertEqual(stale.status_code, 200)
        with self.client.session_transaction() as session:
            self.assertIn("Mietvertrag des Entwurfs wurde geändert", session["_flashes"][-1][1])

        invoice.payment_information = {"method": "sepa_debit", "mandate_reference": "ALTER VERTRAG"}
        invoice.payment_reference = "ALT"
        self.db.session.commit()
        self.assertEqual(self.client.post(url, data={
            **self.draft_form(), **self.assignment_values(), "saved_contract_erp_id": direct_agreement.erp_id,
        }).location, url)
        self.db.session.refresh(invoice)
        self.assertEqual(invoice.company_code_id, self.issuer.id)
        self.assertEqual(invoice.recipient_name, self.company.seller_name)
        self.assertEqual(invoice.tenant_billing_reference["tenant_name"], self.party["name"])
        self.assertEqual(invoice.billing_contract_erp_id, self.agreement.erp_id)
        self.assertIsNone(invoice.recipient_license_agreement_erp_id)
        self.assertIsNone(invoice.recipient_person_erp_id)
        self.assertIsNone(invoice.payment_information)
        self.assertIsNone(invoice.payment_reference)
        self.assertEqual(invoice.insurance_claim_number, "VS-2026-42")

    def test_removal_keeps_the_issuer_and_turns_the_recipient_into_a_free_address(self):
        for routed in (True, False):
            with self.subTest(routed=routed):
                self.company.tenant_billing_issuer = self.issuer if routed else None
                self.company.document_creation_disabled = False
                invoice = self.invoice_service.create_agreement_invoice_draft(self.agreement, self.party, self.creator)
                invoice.payment_information = {"method": "sepa_debit", "mandate_reference": "ALT"}
                invoice.payment_reference = "ALTER VERWENDUNGSZWECK"
                self.db.session.commit()
                issuer_id = invoice.company_code_id
                recipient_name = invoice.recipient_name
                url = f"/admin/invoices/{invoice.id}"
                values = {
                    **self.draft_form(), "saved_contract_erp_id": self.agreement.erp_id, "remove_agreement": "1",
                    "recipient_license_agreement_erp_id": invoice.recipient_license_agreement_erp_id or "",
                    "recipient_type": invoice.recipient_type, "recipient_name": recipient_name,
                    "recipient_street": invoice.recipient_street, "payment_information_data": '{"method":"sepa_debit"}',
                    "recipient_name_addition": "Rechnungsstelle",
                }
                preview = self.client.post(url + "/preview", data=values)
                self.assertEqual(preview.status_code, 200, preview.data[:500])
                preview_text = PdfReader(BytesIO(preview.data)).pages[0].extract_text()
                self.assertIn(recipient_name, preview_text)
                self.assertNotIn("MV-COMMON", preview_text)
                self.db.session.refresh(invoice)
                self.assertEqual(invoice.billing_contract_erp_id, self.agreement.erp_id)

                self.client.post(url + "/finalize", data={**values, "save_draft": "1"})
                self.db.session.refresh(invoice)
                self.assertEqual(invoice.status, "draft")
                self.assertEqual(invoice.billing_contract_erp_id, self.agreement.erp_id)
                self.assertEqual(self.client.post(url, data=values).location, url)
                self.db.session.refresh(invoice)
                self.assertEqual(invoice.company_code_id, issuer_id)
                self.assertEqual(invoice.recipient_name, recipient_name)
                self.assertEqual(invoice.recipient_name_addition, "Rechnungsstelle")
                self.assertIsNone(invoice.billing_contract_number)
                self.assertIsNone(invoice.billing_contract_erp_id)
                self.assertIsNone(invoice.recipient_person_erp_id)
                self.assertIsNone(invoice.tenant_billing_reference)
                self.assertIsNone(invoice.payment_information)
                self.assertIsNone(invoice.payment_reference)
                self.assertIn(b"invoicePaymentInformation", self.client.get(url).data)
                self.assertNotIn(b'id="invoiceAgreementRemove"', self.client.get(url).data)

    def test_optional_insurance_claim_number_can_be_cleared_from_a_free_draft(self):
        invoice = self.create_free_invoice(self.company)
        url = f"/admin/invoices/{invoice.id}"
        self.assertIn(b'id="invoiceInsuranceClaimNumber"', self.client.get(url).data)
        values = {**self.draft_form(), "insurance_claim_number": "VS-89"}
        self.assertEqual(self.client.post(url, data=values).location, url)
        self.db.session.refresh(invoice)
        self.assertEqual(invoice.insurance_claim_number, "VS-89")

        invalid = self.client.post(url + "/preview", data={**values, "insurance_claim_number": "x" * 121})
        self.assertEqual(invalid.status_code, 400)
        preview = self.client.post(url + "/preview", data={**values, "insurance_claim_number": ""})
        self.assertEqual(preview.status_code, 200, preview.data[:500])
        self.assertNotIn("Versicherungsfall-Nr.", PdfReader(BytesIO(preview.data)).pages[0].extract_text())
        self.db.session.refresh(invoice)
        self.assertEqual(invoice.insurance_claim_number, "VS-89")
        self.assertEqual(self.client.post(url, data={**values, "insurance_claim_number": ""}).location, url)
        self.db.session.refresh(invoice)
        self.assertIsNone(invoice.insurance_claim_number)
