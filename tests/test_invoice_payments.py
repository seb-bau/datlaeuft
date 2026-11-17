from datetime import date
from decimal import Decimal
import importlib.util
from io import BytesIO
import json
import os
from pathlib import Path
import types
import unittest
from unittest.mock import Mock

from alembic.migration import MigrationContext
from alembic.operations import Operations
from flask import render_template
from pypdf import PdfReader
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from weasyprint import HTML

import test_document_previews as fixtures


TRANSFER_IBAN = "DE89370400440532013000"
DEBIT_IBAN = "DE12500105170648489890"


class InvoicePaymentTests(unittest.TestCase):
    login = fixtures.DocumentPreviewTests.login
    start_patch = fixtures.DocumentPreviewTests.start_patch
    image = fixtures.DocumentPreviewTests.image
    form = fixtures.DocumentPreviewTests.form
    post_order = fixtures.DocumentPreviewTests.post_order
    draft = fixtures.DocumentPreviewTests.draft
    draft_form = fixtures.DocumentPreviewTests.draft_form
    pdf_text = fixtures.DocumentPreviewTests.pdf_text

    @classmethod
    def setUpClass(cls):
        fixtures.DocumentPreviewTests.setUpClass.__func__(cls)

    def setUp(self):
        fixtures.DocumentPreviewTests.setUp(self)
        self.company.seller_iban = TRANSFER_IBAN
        self.company.seller_bic = "COBADEFFXXX"
        self.company.seller_name = "Testbetrieb GmbH"
        self.company.seller_street = "Betriebsweg 1"
        self.company.seller_postal_code = "12345"
        self.company.seller_town = "Teststadt"
        self.company.seller_country_code = "DE"
        self.company.seller_tax_number = "12/345/67890"
        self.company.seller_creditor_id = "DE98ZZZ09999999999"
        self.db.session.commit()

    def api_rows(self, method="sepa"):
        agreement = {"Id": self.agreement.erp_id, "Banking": {"VirtualIBAN": TRANSFER_IBAN, "CollectiveAccount": {"BIC": "COBADEFFXXX", "AccountHolder": "Testbetrieb GmbH"}}}
        mode = {"Id": 1, "LicenseAgreement": {"Id": self.agreement.erp_id}, "ActiveFrom": "2020-01-01", "ActiveTo": None,
                "Mode": {"Id": 3, "Name": "SEPA-Lastschrift" if method == "sepa" else "Überweisung"},
                "SepaMandate": {"Id": 11, "MandateId": "MANDAT-1", "IBAN": DEBIT_IBAN} if method == "sepa" else None,
                "BankAccount": {"IBAN": DEBIT_IBAN, "BIC": "INGDDEFFXXX", "AccountHolder": "Testperson"}}
        return agreement, mode

    def complete_draft(self, kind="invoice"):
        item = self.draft(kind)
        item.recipient_name = "Testperson"
        item.recipient_street = "Musterweg 4"
        item.recipient_postal_code = "12345"
        item.recipient_town = "Teststadt"
        item.recipient_country_code = "DE"
        if kind == "invoice":
            item.invoice_number = f"TEST-2026-{item.id}"
            item.invoice_date = date(2026, 11, 17)
            item.due_date = date(2026, 12, 1)
            item.service_date_from = item.service_date_to = date(2026, 11, 16)
            item.total_net, item.total_vat, item.total_gross = Decimal("10"), Decimal("1.90"), Decimal("11.90")
        self.db.session.commit()
        return item

    def test_live_fetch_uses_uncached_contract_and_payment_endpoints_and_keeps_account_roles_distinct(self):
        agreement, mode = self.api_rows()
        gateway = object.__new__(self.gateway.WowiportService)
        gateway.client = Mock()
        gateway.client._rest_adapter.get.side_effect = [types.SimpleNamespace(data=[agreement]), types.SimpleNamespace(data=[mode])]
        result = self.invoice_payments.payment_options_from_wowiport(gateway, self.agreement.erp_id, self.company.seller_creditor_id)
        self.assertEqual(result["selected"]["method"], "sepa_debit")
        self.assertEqual(result["selected"]["iban"], DEBIT_IBAN)
        self.assertEqual(result["selected"]["bic"], "INGDDEFFXXX")
        self.assertEqual(result["selected"]["mandate_reference"], "MANDAT-1")
        self.assertEqual(result["selected"]["creditor_id"], self.company.seller_creditor_id)
        self.assertEqual(next(item for item in result["options"] if item["method"] == "bank_transfer")["iban"], TRANSFER_IBAN)
        calls = gateway.client._rest_adapter.get.call_args_list
        self.assertEqual([call.kwargs["endpoint"] for call in calls], ["RentAccounting/LicenseAgreements", "RentAccountingPersonDetails/PaymentModes"])
        self.assertTrue(all(call.kwargs["force_refresh"] for call in calls))
        self.assertTrue(all(call.kwargs["ep_params"]["licenseAgreementId"] == self.agreement.erp_id for call in calls))

    def test_transfer_uses_virtual_account_and_ignores_expired_or_foreign_payment_modes(self):
        agreement, mode = self.api_rows("transfer")
        foreign = {**mode, "LicenseAgreement": {"Id": 999}}
        expired = {**mode, "ActiveTo": "2020-01-02", "Mode": {"Name": "SEPA"}, "SepaMandate": {"IBAN": DEBIT_IBAN}}
        service = Mock()
        service.read_all_rows.side_effect = [[agreement], [foreign, expired, mode]]
        selected = self.invoice_payments.payment_options_from_wowiport(service, self.agreement.erp_id)["selected"]
        self.assertEqual(selected["method"], "bank_transfer")
        self.assertEqual(selected["iban"], TRANSFER_IBAN)
        self.assertEqual(selected["bic"], "COBADEFFXXX")

    def test_ambiguous_active_modes_require_a_choice_and_mismatched_debit_accounts_are_rejected(self):
        agreement, mode = self.api_rows()
        service = Mock()
        service.read_all_rows.side_effect = [[agreement], [mode, {**mode, "Mode": {"Name": "Überweisung"}, "SepaMandate": None}]]
        result = self.invoice_payments.payment_options_from_wowiport(service, self.agreement.erp_id)
        self.assertTrue(result["requires_selection"])
        self.assertEqual(result["selected"]["method"], "")
        mode["BankAccount"]["IBAN"] = TRANSFER_IBAN
        service.read_all_rows.side_effect = [[agreement], [mode]]
        with self.assertRaisesRegex(ValueError, "unterschiedliche IBANs"):
            self.invoice_payments.payment_options_from_wowiport(service, self.agreement.erp_id)

    def test_fetch_checks_permissions_company_and_draft_status_without_saving(self):
        invoice = self.draft("invoice")
        agreement, mode = self.api_rows()
        self.remote.read_all_rows.side_effect = [[agreement], [mode]]
        response = self.client.get(f"/admin/invoices/{invoice.id}/payment-information?license_agreement_id={self.agreement.erp_id}")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(response.json["selected"]["iban"], DEBIT_IBAN)
        self.assertIsNone(invoice.payment_information)
        self.assertIsNone(invoice.recipient_license_agreement_erp_id)
        self.creator.can_manage_invoices = False
        self.db.session.commit()
        self.login(self.creator)
        self.assertEqual(self.client.get(f"/admin/invoices/{invoice.id}/payment-information?license_agreement_id={self.agreement.erp_id}").status_code, 403)
        self.creator.can_manage_invoices = True
        self.db.session.commit()
        self.login(self.creator)
        self.economic.company_code_erp_id = 999
        self.db.session.commit()
        self.assertEqual(self.client.get(f"/admin/invoices/{invoice.id}/payment-information?license_agreement_id={self.agreement.erp_id}").status_code, 400)
        invoice.status = "finalized"
        self.db.session.commit()
        self.assertEqual(self.client.get(f"/admin/invoices/{invoice.id}/payment-information?license_agreement_id={self.agreement.erp_id}").status_code, 409)

    def test_manual_snapshot_is_saved_validated_and_bound_to_the_selected_contract(self):
        invoice = self.draft("invoice")
        values = self.draft_form()
        information = {"method": "bank_transfer", "iban": TRANSFER_IBAN, "bic": "COBADEFFXXX", "source": "manual", "license_agreement_erp_id": self.agreement.erp_id}
        values.update(recipient_license_agreement_erp_id=str(self.agreement.erp_id), payment_information_data=json.dumps(information))
        with self.app.test_request_context(method="POST", data=values):
            self.admin.save_invoice_draft_form(invoice)
            self.db.session.commit()
        self.db.session.expire_all()
        self.assertEqual(invoice.payment_information["iban"], TRANSFER_IBAN)
        self.assertEqual(invoice.payment_information["source"], "manual")
        values["payment_information_data"] = json.dumps({**information, "license_agreement_erp_id": 999})
        with self.app.test_request_context(method="POST", data=values), self.assertRaisesRegex(ValueError, "anderen Vertrag"):
            self.admin.save_invoice_draft_form(invoice)
        self.db.session.rollback()
        for data in ({**information, "iban": "DE001234"}, {**information, "bic": "invalid"}, {**information, "method": "cash"}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.invoice_payments.normalize_payment_information(data, complete=True)

    def test_payment_texts_are_editable_and_frozen_for_the_final_document(self):
        invoice = self.complete_draft()
        self.settings.settings.set(self.settings.SettingKeys.INVOICE_TRANSFER_PAYMENT_TEXT, "Überweisung von {amount} {currency} bis {due_date}; Zweck {payment_reference}.")
        self.invoice_payments.freeze_invoice_payment_information(invoice)
        self.db.session.commit()
        self.settings.settings.set(self.settings.SettingKeys.INVOICE_TRANSFER_PAYMENT_TEXT, "Geänderter Text.")
        context = self.invoice_payments.invoice_payment_context(invoice)
        self.assertIn("11,90 EUR", context["payment_terms_text"])
        self.assertIn("01.12.2026", context["payment_terms_text"])
        self.assertIn("TEST-2026-1", context["payment_terms_text"])
        with self.assertRaises(ValueError):
            self.settings.settings.set(self.settings.SettingKeys.INVOICE_SEPA_PAYMENT_TEXT, "{unknown}")

    def test_payment_preview_does_not_save_bank_changes_and_missing_mandates_block_finalization(self):
        invoice = self.draft("invoice")
        values = self.draft_form()
        information = {"method": "sepa_debit", "iban": DEBIT_IBAN, "bic": "INGDDEFFXXX", "source": "manual",
                       "mandate_reference": "MANDAT-1", "creditor_id": self.company.seller_creditor_id}
        values["payment_information_data"] = json.dumps(information)
        text = self.pdf_text(self.client.post(f"/admin/invoices/{invoice.id}/preview", data=values))
        self.assertIn(DEBIT_IBAN, text)
        self.db.session.expire_all()
        self.assertIsNone(invoice.payment_information)
        self.assertIsNone(invoice.invoice_number)
        invoice.payment_information = {**information, "mandate_reference": ""}
        with self.assertRaisesRegex(self.invoice_service.InvoiceFinalizationError, "Mandatsreferenz"):
            self.invoice_service._validate_invoice_for_finalization(invoice)

    def test_pdf_and_zugferd_use_the_same_payment_method_and_correct_account(self):
        invoice = self.complete_draft()
        positions = self.invoice_document._prepare_positions(invoice)
        for method, iban in (("bank_transfer", TRANSFER_IBAN), ("sepa_debit", DEBIT_IBAN)):
            with self.subTest(method=method):
                invoice.payment_information = {"method": method, "iban": iban, "bic": "INGDDEFFXXX", "mandate_reference": "MANDAT-1", "creditor_id": self.company.seller_creditor_id}
                context = self.invoice_document._document_context(invoice, positions, self.invoice_document._prepare_vat_rows(positions))
                data = self.invoice_document._zugferd_data(context)
                xml = self.invoice_document.generate_cii_xml(data, level="en16931", check_xsd=True, check_schematron=True, prefixed_namespaces=True)
                self.assertIn(iban.encode(), xml)
                self.assertEqual(data["BT-81"], "59" if method == "sepa_debit" else "58")
                self.assertEqual(data.get("BT-91") if method == "sepa_debit" else data.get("BT-84"), iban)
                if method == "sepa_debit":
                    self.assertNotIn("BT-84", data)
                    self.assertEqual(data["BT-89"], "MANDAT-1")
                raw = self.document_previews.render_document_preview("invoice", invoice)
                text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(raw)).pages)
                self.assertIn(iban, text)
                self.assertIn("Einzugskonto" if method == "sepa_debit" else "Zahlungskonto", text)

    def test_pdf_article_column_is_optional_and_all_text_stays_inside_a4(self):
        for kind in ("invoice", "offer"):
            for number in ("", "ARTIKEL" * 17):
                with self.subTest(kind=kind, number=number):
                    item = self.complete_draft(kind)
                    position = item.positions[0]
                    position.source_data = {"article_number": number}
                    position.description = "Leistungsbezeichnung " + "UngetrennteBezeichnung" * 12
                    position.details = "Ergänzende Hinweise\n" + "Zusatzinformation " * 35
                    position.unit_price_net = Decimal("1234567890.12")
                    position.net_amount = Decimal("1234567890.12")
                    if kind == "invoice":
                        context = self.document_previews.invoice_preview_context(item)
                    else:
                        positions = self.offer_document.prepare_positions(item)
                        context = self.offer_document.document_context(item, positions, self.offer_document.prepare_vat_rows(positions), preview=True)
                    html = render_template(f"admin/{kind}_pdf.html", **context)
                    self.assertEqual("<th class=\"article-number\">Artikel-Nr.</th>" in html, bool(number))
                    document = HTML(string=html, base_url=self.app.root_path).render()
                    for page in document.pages:
                        self.assertAlmostEqual(page.width, 210 * 96 / 25.4, places=2)
                        self.assertAlmostEqual(page.height, 297 * 96 / 25.4, places=2)
                        for box in page._page_box.descendants():
                            if type(box).__name__ == "TextBox" and box.text.strip():
                                self.assertGreaterEqual(box.position_x, 0)
                                self.assertLessEqual(box.position_x + box.width, page.width + 0.1, (kind, box.text))


class InvoicePaymentMigrationTests(unittest.TestCase):
    def test_migration_preserves_existing_invoices_and_can_be_reversed(self):
        database_url = os.environ.get("DATLAEUFT_TEST_DATABASE_URL", "sqlite://")
        url = make_url(database_url)
        if url.get_backend_name() != "sqlite" and not (url.database or "").startswith("datlaeuft_test_"):
            self.fail("Migrationstests benötigen eine separate datlaeuft_test-Datenbank.")
        engine = sa.create_engine(database_url)
        self.addCleanup(engine.dispose)
        if any(sa.inspect(engine).has_table(name) for name in ("invoice", "wowi_company_code")):
            self.skipTest("Die Migration benötigt eine eigene leere Testdatenbank.")
        path = Path(__file__).resolve().parents[1] / "migrations/versions/e2a4c6d8f0b3_invoice_payment_information.py"
        spec = importlib.util.spec_from_file_location("invoice_payment_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        try:
            with engine.begin() as connection:
                connection.execute(sa.text("CREATE TABLE invoice (id INTEGER PRIMARY KEY, invoice_number VARCHAR(160))"))
                connection.execute(sa.text("CREATE TABLE wowi_company_code (id INTEGER PRIMARY KEY, code VARCHAR(40))"))
                connection.execute(sa.text("INSERT INTO invoice (id, invoice_number) VALUES (1, 'ALT-1')"))
                with Operations.context(MigrationContext.configure(connection)):
                    migration.upgrade()
                    self.assertEqual(tuple(connection.execute(sa.text("SELECT invoice_number, payment_information FROM invoice")).one()), ("ALT-1", None))
                    migration.downgrade()
                    self.assertEqual(connection.execute(sa.text("SELECT invoice_number FROM invoice")).scalar(), "ALT-1")
        finally:
            with engine.begin() as connection:
                connection.execute(sa.text("DROP TABLE IF EXISTS invoice"))
                connection.execute(sa.text("DROP TABLE IF EXISTS wowi_company_code"))
