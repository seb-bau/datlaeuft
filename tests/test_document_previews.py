import ast
from functools import wraps
import importlib
import importlib.util
from io import BytesIO
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from alembic.migration import MigrationContext
from alembic.operations import Operations
from flask import abort, g, redirect, request, url_for
from flask_login import current_user
from pypdf import PdfReader
import sqlalchemy as sa

import test_ticket_attachment_selection as fixtures


class DocumentPreviewTests(unittest.TestCase):
    login = fixtures.TicketAttachmentSelectionTests.login
    start_patch = fixtures.TicketAttachmentSelectionTests.start_patch
    image = fixtures.TicketAttachmentSelectionTests.image
    form = fixtures.TicketAttachmentSelectionTests.form
    post_order = fixtures.TicketAttachmentSelectionTests.post_order

    @classmethod
    def setUpClass(cls):
        fixtures.TicketAttachmentSelectionTests.setUpClass.__func__(cls)
        cls.app.before_request(lambda: g.pop("_login_user", None) and None)
        sys.modules[cls.package_name + ".auth"].accounting_required = lambda view: view
        for name in ("stationery_layout", "tenant_billing", "invoice_payments", "invoice_attachments", "invoice_document", "offer_document", "invoice_service", "offer_service", "document_articles", "document_previews"):
            sys.modules.pop(cls.package_name + "." + name, None)
            setattr(cls, name, importlib.import_module(cls.package_name + "." + name))
        cls.admin.render_document_preview = cls.document_previews.render_document_preview
        cls.admin.attach_article_fields = cls.document_articles.attach_article_fields
        cls.admin.save_invoice_payment_form = cls.invoice_payments.save_invoice_payment_form
        cls.admin.invoice_payment_information = cls.invoice_payments.invoice_payment_information
        cls.admin.payment_options_from_wowiport = cls.invoice_payments.payment_options_from_wowiport
        source = ast.parse((cls.root / "app/admin.py").read_text())
        for node in source.body:
            if isinstance(node, ast.ImportFrom) and node.module in {"invoice_service", "offer_service", "invoice_document", "offer_document", "tenant_billing"}:
                module = getattr(cls, node.module)
                for item in node.names:
                    setattr(cls.admin, item.asname or item.name, getattr(module, item.name))
        auth_source = ast.parse((cls.root / "app/auth.py").read_text())
        functions = [node for node in auth_source.body if isinstance(node, ast.FunctionDef) and node.name in {"invoice_required", "offer_required", "commission_creation_required"}]
        namespace = dict(F=object, Any=object, wraps=wraps, current_user=current_user, abort=abort, redirect=redirect, request=request, url_for=url_for)
        exec(compile(ast.Module(body=functions, type_ignores=[]), "auth.py", "exec"), namespace)
        sys.modules[cls.package_name + ".auth"].invoice_required = namespace["invoice_required"]
        for endpoint, guard in (("invoice_preview", "invoice_required"), ("offer_preview", "offer_required"), ("commission_create_preview", "commission_creation_required"), ("invoice_payment_information_fetch", "invoice_required")):
            view = cls.app.view_functions["admin." + endpoint]
            cls.app.view_functions["admin." + endpoint] = namespace[guard](view.__wrapped__)
        for name in ("Invoice", "InvoiceAttachment", "InvoicePosition", "InvoiceCommission", "Offer", "OfferPosition", "OfferCraftFallback", "LocalCatalogItem"):
            table = getattr(cls.models, name).__table__
            if table not in cls.tables:
                cls.tables.append(table)

    def setUp(self):
        fixtures.TicketAttachmentSelectionTests.setUp(self)
        self.creator.can_manage_invoices = True
        self.creator.can_manage_offers = True
        self.db.session.commit()

    def draft(self, kind):
        model = self.models.Invoice if kind == "invoice" else self.models.Offer
        position_model = self.models.InvoicePosition if kind == "invoice" else self.models.OfferPosition
        item = model(company_code=self.company, company_code_erp_id=self.company.erp_id, company_code_code=self.company.code, created_by=self.creator, status="draft", recipient_name="Gespeicherter Empfänger")
        item.positions.append(position_model(position_index=0, source_type="free", description="Gespeicherte Position", quantity=1,
                                             unit="Stk", unit_price_net=10, vat_rate=19, net_amount=10, vat_amount=1.9, gross_amount=11.9,
                                             source_data={"article_number": "ALT-1"}))
        self.db.session.add(item)
        self.db.session.commit()
        return item

    def draft_form(self, number="NEU-42"):
        return {"recipient_name": "Neue freie Empfängerin", "recipient_street": "Musterweg 4", "recipient_postal_code": "12345",
                "recipient_town": "Teststadt", "invoice_date": "2026-11-17", "offer_date": "2026-11-17",
                "position_source_type": "free", "position_article_number": number, "position_description": "Ungespeicherte Reparatur",
                "position_quantity": "2", "position_unit": "Stk", "position_unit_price_net": "12,50", "position_vat_rate": "19"}

    def pdf_text(self, response):
        self.assertEqual(response.status_code, 200, response.data[:1000])
        self.assertEqual(response.mimetype, "application/pdf")
        self.assertTrue(response.headers["Content-Disposition"].startswith("inline"))
        self.assertIn("no-store", response.headers["Cache-Control"])
        return "\n".join(page.extract_text() for page in PdfReader(BytesIO(response.data)).pages)

    def test_invoice_and_offer_preview_current_form_without_saving_or_numbering(self):
        for kind in ("invoice", "offer"):
            with self.subTest(kind=kind):
                item = self.draft(kind)
                position_id = item.positions[0].id
                response = self.client.post(f"/admin/{kind}s/{item.id}/preview", data=self.draft_form())
                text = self.pdf_text(response)
                for value in ("Neue freie Empfängerin", "Ungespeicherte Reparatur", "NEU-42", "29,75", "ENTWURF"):
                    self.assertIn(value, text)
                self.db.session.expire_all()
                self.assertEqual(item.recipient_name, "Gespeicherter Empfänger")
                self.assertEqual(item.positions[0].id, position_id)
                self.assertEqual(item.positions[0].description, "Gespeicherte Position")
                self.assertEqual(item.positions[0].article_number, "ALT-1")
                self.assertIsNone(getattr(item, kind + "_number"))
                self.assertIsNone(item.document_filename)
        self.assertEqual(self.models.ErpOutboxOperation.query.count(), 0)
        self.assertEqual(self.models.AuditLog.query.count(), 0)

    def test_explicit_blank_article_number_is_preserved_in_forms_and_pdf(self):
        item = self.draft("invoice")
        item.positions[0].task_template = self.task
        self.db.session.commit()
        values = self.draft_form("")
        values.update(position_source_type="catalog", position_task_template_id=str(self.task.id))
        response = self.client.post(f"/admin/invoices/{item.id}/preview", data=values)
        text = self.pdf_text(response)
        self.assertNotIn("Artikel-Nr.", text)
        self.assertNotIn(self.task.code, text)
        with self.app.test_request_context(method="POST", data=values):
            self.admin.save_invoice_positions_form(item)
            self.db.session.commit()
        self.db.session.expire_all()
        self.assertEqual(item.positions[0].article_number, "")
        prepared = self.invoice_document._prepare_positions(item)
        self.assertEqual(prepared[0]["article_number"], "")
        self.assertEqual(prepared[0]["item_code"], "")

    def test_invalid_form_and_render_failure_roll_back_the_draft(self):
        item = self.draft("invoice")
        values = self.draft_form("X" * 121)
        response = self.client.post(f"/admin/invoices/{item.id}/preview", data=values)
        self.assertEqual(response.status_code, 400)
        self.assertIn("120", response.json["error"])
        with patch.object(self.admin, "render_document_preview", side_effect=RuntimeError("PDF renderer unavailable")):
            with self.assertLogs(self.app.logger, level="ERROR") as captured:
                response = self.client.post(f"/admin/invoices/{item.id}/preview", data=self.draft_form())
        self.assertEqual(response.status_code, 502)
        self.assertIn("PDF renderer unavailable", "\n".join(captured.output))
        self.db.session.expire_all()
        self.assertEqual(item.recipient_name, "Gespeicherter Empfänger")
        self.assertEqual(item.positions[0].article_number, "ALT-1")

    def test_preview_requires_the_correct_permission_and_an_editable_draft(self):
        item = self.draft("invoice")
        self.creator.can_manage_invoices = False
        self.db.session.commit()
        self.login(self.creator)
        self.assertEqual(self.client.post(f"/admin/invoices/{item.id}/preview", data=self.draft_form()).status_code, 403)
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.post(f"/admin/invoices/{item.id}/preview", data=self.draft_form()).status_code, 302)
        self.creator.can_manage_invoices = True
        item.status = "finalized"
        self.db.session.commit()
        self.login(self.creator)
        self.assertEqual(self.client.post(f"/admin/invoices/{item.id}/preview", data=self.draft_form()).status_code, 409)

    def test_order_preview_uses_optional_article_number_without_creating_an_order(self):
        for number in ("FREI-77", ""):
            values = self.form(selected=(), position_article_number=number, execution_from="2026-11-18T09:30")
            text = self.pdf_text(self.client.post("/admin/commissions/preview", data=values))
            self.assertIn("Bitte reparieren", text)
            self.assertIn("18.11.2026 09:30", text)
            if number:
                self.assertIn(number, text)
            else:
                self.assertNotIn(self.task.code, text)
        self.assertEqual(self.models.Commission.query.count(), 0)
        self.assertEqual(self.models.ErpOutboxOperation.query.count(), 0)
        self.assertEqual(self.models.WorkflowNotification.query.count(), 0)
        self.remote.download_document_content.assert_not_called()

    def test_order_article_number_survives_saving_and_erp_readback(self):
        order = self.post_order(self.form(selected=(), position_article_number=""))
        position = order.positions[0]
        self.assertEqual(position.article_number, "")
        self.assertEqual(position.code, self.task.code)
        self.assertEqual(position.display_article_number, "")
        payload = [{"Id": 123, "Code": self.task.code, "ServiceCatalogue": {"Id": 600}, "Units": 1, "CommissionText": "Bitte reparieren"}]
        self.gateway.replace_commission_positions(order, payload)
        self.db.session.commit()
        self.assertEqual(order.positions[0].article_number, "")
        order.positions[0].article_number = "EIGEN-88"
        self.db.session.commit()
        self.gateway.replace_commission_positions(order, payload)
        self.db.session.commit()
        self.assertEqual(order.positions[0].display_article_number, "EIGEN-88")
        text = self.pdf_text(self.client.get(f"/admin/commissions/{order.id}/preview"))
        self.assertIn("EIGEN-88", text)

    def test_catalog_article_number_is_used_only_when_no_explicit_value_exists(self):
        for model in (self.models.InvoicePosition, self.models.OfferPosition):
            item = model(task_template=self.task, source_data={})
            self.assertEqual(item.article_number, self.task.code)
            item.source_data = {"article_number": ""}
            self.assertEqual(item.article_number, "")
            item.source_data = {"article_number": "FREI-9"}
            self.assertEqual(item.article_number, "FREI-9")
        position = self.models.CommissionPosition(code="ERP-9")
        self.assertEqual(position.display_article_number, "ERP-9")
        position.article_number = ""
        self.assertEqual(position.display_article_number, "")


class ArticleNumberMigrationTests(unittest.TestCase):
    def test_upgrade_preserves_existing_codes_and_accepts_empty_numbers(self):
        engine = sa.create_engine("sqlite://")
        spec = importlib.util.spec_from_file_location("article_number_migration", Path(__file__).resolve().parents[1] / "migrations/versions/d0f2a4c6e8b1_commission_article_numbers.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with engine.begin() as connection:
            connection.execute(sa.text("CREATE TABLE commission_position (id INTEGER PRIMARY KEY, code VARCHAR(120))"))
            connection.execute(sa.text("INSERT INTO commission_position (id, code) VALUES (1, 'ALT-1')"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                self.assertEqual(tuple(connection.execute(sa.text("SELECT code, article_number FROM commission_position")).one()), ("ALT-1", None))
                connection.execute(sa.text("UPDATE commission_position SET article_number = '' WHERE id = 1"))
                self.assertEqual(connection.execute(sa.text("SELECT article_number FROM commission_position")).scalar(), "")
                migration.downgrade()
                self.assertEqual(connection.execute(sa.text("SELECT code FROM commission_position")).scalar(), "ALT-1")
