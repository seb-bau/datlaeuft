import ast
from decimal import Decimal
from functools import wraps
import importlib
from io import BytesIO
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from flask import abort, redirect, request, url_for
from flask_login import current_user
from pypdf import PdfReader
from weasyprint import HTML

import test_invoice_payments as fixtures


class StationeryLayoutTests(unittest.TestCase):
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
        for name in ("invoice_correspondence", "stationery_preview"):
            sys.modules.pop(cls.package_name + "." + name, None)
            setattr(cls, name, importlib.import_module(cls.package_name + "." + name))
        source = ast.parse((cls.root / "app/auth.py").read_text())
        guard = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == "admin_required")
        namespace = dict(F=object, Any=object, wraps=wraps, current_user=current_user, abort=abort, redirect=redirect, request=request, url_for=url_for)
        exec(compile(ast.Module(body=[guard], type_ignores=[]), "auth.py", "exec"), namespace)
        for endpoint in ("company_code_invoicing", "company_code_stationery_preview"):
            view = cls.app.view_functions["admin." + endpoint]
            cls.app.view_functions["admin." + endpoint] = namespace["admin_required"](view.__wrapped__)

    def setUp(self):
        fixtures.InvoicePaymentTests.setUp(self)
        self.creator.is_admin = True
        self.db.session.commit()
        self.login(self.creator)
        self.start_patch(patch.object(self.app, "instance_path", str(Path(self.directory_path.name) / "instance")))
        self.directory = Path(self.app.instance_path) / "invoice_stationery"
        self.directory.mkdir(parents=True)
        self.company.stationery_filename = "briefbogen.pdf"
        (self.directory / self.company.stationery_filename).write_bytes(self.paper("PAPIER ALT"))
        self.db.session.commit()
        self.archive = self.start_patch(patch.object(sys.modules[self.package_name + ".dms_service"], "register_generated_pdf", Mock(), create=True))
        context_module = sys.modules.setdefault(self.package_name + ".dms_context", ModuleType(self.package_name + ".dms_context"))
        for name in ("invoice_context", "offer_context"):
            self.start_patch(patch.object(context_module, name, return_value={}, create=True))

    def paper(self, label):
        return HTML(string=f'<style>@page{{size:A4;margin:0}}body{{margin:0;font:10pt Arial}}footer{{position:fixed;bottom:10mm;left:24mm}}</style><footer>{label} · Geschäftsdaten im Fußbereich</footer>').write_pdf()

    def page_text_positions(self, page):
        positions = []
        def visitor(text, matrix, text_matrix, font, font_size):
            if text.strip() and "PAPIER" not in text and "ENTWURF" not in text:
                baseline = matrix[1] * text_matrix[4] + matrix[3] * text_matrix[5] + matrix[5]
                positions.append((text.strip(), float(page.mediabox.height) - baseline))
        page.extract_text(visitor_text=visitor)
        return positions

    def assert_bottom_space(self, data, bottom_mm):
        reader = PdfReader(BytesIO(data))
        for index, page in enumerate(reader.pages):
            for text, top in self.page_text_positions(page):
                self.assertLessEqual(top, float(page.mediabox.height) - bottom_mm * 72 / 25.4 + 1, (index, text, top))
        return reader

    def configuration_form(self, **values):
        return {
            "invoice_number_mask": "<company_code>-<year>-<counter>", "invoice_counter_digits": "4",
            "offer_number_mask": "AN-<company_code>-<year>-<counter>", "offer_counter_digits": "4",
            **values,
        }

    def preview(self, kind="invoice", **values):
        return self.client.post(f"/admin/company-codes/{self.company.id}/invoicing/preview/{kind}", data=values)

    def test_spacing_is_saved_per_company_and_can_be_cleared(self):
        url = f"/admin/company-codes/{self.company.id}/invoicing"
        response = self.client.post(url, data=self.configuration_form(
            stationery_bottom_mm="55,5", stationery_first_page_top_mm="65", stationery_following_page_top_mm="35",
        ))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.stationery_layout.company_page_layout(self.company), {
            "bottom_mm": "55.5", "first_page_top_mm": "65", "following_page_top_mm": "35",
        })
        self.assertEqual(self.stationery_layout.company_page_layout(SimpleNamespace(id=999, code="ANDERES")), {})
        html = self.client.get(url).get_data(as_text=True)
        self.assertIn('value="55.5"', html)
        self.assertIn("Rechnungsvorschau", html)
        self.assertIn("Angebotsvorschau", html)
        self.assertIn("Anschreibenvorschau", html)
        self.assertIn('data-preview-files="stationery"', html)
        response = self.client.post(url, data=self.configuration_form(
            stationery_bottom_mm="", stationery_first_page_top_mm="", stationery_following_page_top_mm="",
        ))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.stationery_layout.company_page_layout(self.company), {})

    def test_old_forms_preserve_existing_spacing_and_zero_is_a_valid_value(self):
        self.stationery_layout.save_layouts(self.company, {"stationery_bottom_mm": "0"})
        self.db.session.commit()
        self.stationery_layout.save_layouts(self.company, {})
        self.db.session.commit()
        self.assertEqual(self.stationery_layout.company_page_layout(self.company), {"bottom_mm": "0"})

    def test_cover_letters_inherit_shared_paper_spacing_and_keep_their_own_overrides(self):
        self.stationery_layout.save_layouts(self.company, {
            "stationery_bottom_mm": "55", "stationery_following_page_top_mm": "35",
            "cover_letter_stationery_first_page_top_mm": "60",
        })
        self.db.session.commit()
        self.assertEqual(self.stationery_layout.company_page_layout(self.company, "cover_letter_stationery"), {
            "bottom_mm": "55", "following_page_top_mm": "35", "first_page_top_mm": "60",
        })
        self.company.cover_letter_stationery_filename = "eigenes-papier.pdf"
        self.assertEqual(self.stationery_layout.company_page_layout(self.company, "cover_letter_stationery"), {"first_page_top_mm": "60"})

    def test_invalid_spacing_is_rejected_before_saving_or_rendering(self):
        definition = self.stationery_layout.layout_definition(self.company)
        for values in (
            {"stationery_bottom_mm": "-1"}, {"stationery_bottom_mm": "151"},
            {"stationery_bottom_mm": "NaN"}, {"stationery_bottom_mm": "Infinity"},
            {"stationery_bottom_mm": "12.34"}, {"stationery_bottom_mm": "30mm; color:red"},
            {"stationery_bottom_mm": "150", "stationery_first_page_top_mm": "150"},
        ):
            with self.subTest(values=values):
                response = self.preview(**values)
                self.assertEqual(response.status_code, 400, response.data[:1000])
                self.assertIn("error", response.json)
                self.assertIsNone(self.models.AppSetting.query.filter_by(key=definition.key).one_or_none())
        response = self.preview("cover_letter", stationery_bottom_mm="140", cover_letter_stationery_first_page_top_mm="120")
        self.assertEqual(response.status_code, 400)
        response = self.client.post(f"/admin/company-codes/{self.company.id}/invoicing", data=self.configuration_form(stationery_bottom_mm="-1"))
        self.assertEqual(response.status_code, 200)
        self.assertIn('value="-1"', response.get_data(as_text=True))
        self.assertIsNone(self.models.AppSetting.query.filter_by(key=definition.key).one_or_none())

    def test_preview_uses_unsaved_spacing_and_uploaded_paper_without_persisting_anything(self):
        before = (self.models.AppSetting.query.count(), self.models.Invoice.query.count(), self.models.Offer.query.count(), self.models.AuditLog.query.count())
        for kind in ("invoice", "offer", "cover_letter"):
            with self.subTest(kind=kind):
                response = self.preview(kind, stationery_bottom_mm="55", stationery_following_page_top_mm="35",
                    stationery=(BytesIO(self.paper("PAPIER NEU")), "neu.pdf"))
                text = self.pdf_text(response)
                self.assertIn("PAPIER NEU", text)
                self.assertNotIn("PAPIER ALT", text)
                reader = self.assert_bottom_space(response.data, 55)
                self.assertGreater(len(reader.pages), 1)
        self.assertEqual(before, (self.models.AppSetting.query.count(), self.models.Invoice.query.count(), self.models.Offer.query.count(), self.models.AuditLog.query.count()))
        self.assertEqual(self.company.stationery_filename, "briefbogen.pdf")
        self.assertEqual([path.name for path in self.directory.iterdir()], ["briefbogen.pdf"])
        self.archive.assert_not_called()

    def test_preview_can_remove_the_background_and_rejects_invalid_uploads(self):
        response = self.preview(remove_stationery="on", stationery_bottom_mm="45")
        self.assertNotIn("PAPIER ALT", self.pdf_text(response))
        self.assertEqual(self.company.stationery_filename, "briefbogen.pdf")
        response = self.preview(stationery=(BytesIO(b"kein PDF"), "defekt.pdf"))
        self.assertEqual(response.status_code, 400)
        self.assertIn("PDF", response.json["error"])
        self.assertEqual([path.name for path in self.directory.iterdir()], ["briefbogen.pdf"])

    def test_new_cover_letter_paper_uses_its_own_spacing_before_saving(self):
        response = self.preview("cover_letter", stationery_bottom_mm="90", cover_letter_stationery_bottom_mm="40",
            cover_letter_stationery=(BytesIO(self.paper("PAPIER ANSCHREIBEN")), "anschreiben.pdf"))
        self.assertIn("PAPIER ANSCHREIBEN", self.pdf_text(response))
        self.assert_bottom_space(response.data, 40)
        self.assertIsNone(self.company.cover_letter_stationery_filename)

    def test_page_boundaries_override_custom_css_and_following_pages_do_not_change_the_first_page(self):
        context = self.stationery_preview.sample_document_context(self.company, {})
        context["preview"] = False
        layout = '<style>@page{size:A4;margin:5mm 20mm!important;@bottom-right{content:"ALTE FUSSZEILE"!important}}@page:first{margin-top:65mm!important}p{margin:0;height:15mm}</style>{% for position in positions %}<p>{{ position.description }}</p>{% endfor %}<p>ZAHLUNGSANWEISUNGEN</p>'
        default = self.invoice_document.render_invoice_content_pdf(context, layout=layout)
        content = self.invoice_document.render_invoice_content_pdf(context, layout=layout, page_layout={"following_page_top_mm": "35", "bottom_mm": "55"})
        reader = self.assert_bottom_space(content, 55)
        self.assertGreater(len(reader.pages), len(PdfReader(BytesIO(default)).pages))
        self.assertGreater(min(top for _, top in self.page_text_positions(reader.pages[0])), 65 * 72 / 25.4)
        for page in reader.pages[1:]:
            first_top = min(top for _, top in self.page_text_positions(page))
            self.assertGreater(first_top, 35 * 72 / 25.4)
            self.assertLess(first_top, 50 * 72 / 25.4)
        text = "\n".join(page.extract_text() for page in reader.pages)
        self.assertIn("ZAHLUNGSANWEISUNGEN", text)
        self.assertNotIn("ALTE FUSSZEILE", text)
        self.assertIn("ALTE FUSSZEILE", "\n".join(page.extract_text() for page in PdfReader(BytesIO(default)).pages))

    def test_saved_spacing_applies_to_actual_invoice_offer_and_cover_letter_pdfs(self):
        self.stationery_layout.save_layouts(self.company, {"stationery_bottom_mm": "65", "stationery_following_page_top_mm": "40"})
        self.db.session.commit()
        for kind in ("invoice", "offer"):
            with self.subTest(kind=kind):
                item = self.complete_draft(kind)
                model = self.models.InvoicePosition if kind == "invoice" else self.models.OfferPosition
                for index in range(1, 35):
                    item.positions.append(model(position_index=index, source_type="free", description=f"POSITION {index:02d}", quantity=1, unit="Stk", unit_price_net=10, vat_rate=19, net_amount=10, vat_amount=Decimal("1.90"), gross_amount=Decimal("11.90")))
                item.total_net, item.total_vat, item.total_gross = Decimal("350"), Decimal("66.50"), Decimal("416.50")
                if kind == "offer":
                    from datetime import date
                    item.offer_date = date(2026, 11, 20)
                    item.offer_number = "AN-TEST-001"
                self.db.session.commit()
                module = self.invoice_document if kind == "invoice" else self.offer_document
                result = module.create_invoice_document(item) if kind == "invoice" else module.create_offer_document(item)
                data = (self.invoice_document.resolve_invoice_document_path(result.pdf_storage_name) if kind == "invoice" else Path(self.app.instance_path) / result.pdf_storage_name).read_bytes()
                self.assertGreater(len(self.assert_bottom_space(data, 65).pages), 1)
                preview = self.document_previews.render_document_preview(kind, item)
                self.assert_bottom_space(preview, 65)
        content = self.invoice_correspondence.render_letter_pdf({"title": "Anschreiben", "text": "Langer Text\n" * 100, "preview": False}, self.company)
        self.assertGreater(len(self.assert_bottom_space(content, 65).pages), 1)

    def test_stationery_preview_requires_an_administrator_and_a_known_document_type(self):
        self.assertEqual(self.preview("unbekannt").status_code, 404)
        self.creator.is_admin = False
        self.db.session.commit()
        self.assertEqual(self.preview().status_code, 403)
        response = self.app.test_client().post(f"/admin/company-codes/{self.company.id}/invoicing/preview/invoice")
        self.assertEqual(response.status_code, 302)


if __name__ == "__main__":
    unittest.main()
