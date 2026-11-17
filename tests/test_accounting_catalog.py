import ast
from datetime import datetime, timezone
from decimal import Decimal
from functools import wraps
from html.parser import HTMLParser
import importlib
import importlib.util
import os
from io import BytesIO
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

from alembic.migration import MigrationContext
from alembic.operations import Operations
from flask import Flask, abort, g, redirect, request, url_for
from flask_login import LoginManager, current_user
from flask_sqlalchemy import SQLAlchemy
from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader
import sqlalchemy as sa
from sqlalchemy.engine import make_url


class TokenParser(HTMLParser):
    token = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "input" and values.get("name") == "token":
            self.token = values.get("value")


class AccountingCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.app = Flask(__name__, template_folder=str(cls.root / "app/templates"))
        cls.app.config.update(TESTING=True, SECRET_KEY="accounting-test-secret", SQLALCHEMY_DATABASE_URI="sqlite://")
        cls.db = SQLAlchemy(cls.app)
        cls.package_name = "_accounting_catalog_test_app"
        package = types.ModuleType(cls.package_name)
        package.__path__ = [str(cls.root / "app")]
        extensions = types.ModuleType(cls.package_name + ".extensions")
        extensions.db = cls.db
        auth = types.ModuleType(cls.package_name + ".auth")
        source = ast.parse((cls.root / "app/auth.py").read_text())
        guard = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == "accounting_required")
        namespace = dict(F=object, Any=object, wraps=wraps, current_user=current_user, abort=abort, redirect=redirect, request=request, url_for=url_for)
        exec(compile(ast.Module(body=[guard], type_ignores=[]), "auth.py", "exec"), namespace)
        auth.accounting_required = namespace["accounting_required"]
        blacklist = types.ModuleType(cls.package_name + ".blacklist")
        blacklist.is_blacklisted = lambda *args: False
        modules = {cls.package_name: package, extensions.__name__: extensions, auth.__name__: auth, blacklist.__name__: blacklist}
        module_patch = patch.dict(sys.modules, modules)
        module_patch.start()
        cls.addClassCleanup(module_patch.stop)
        cls.models = importlib.import_module(cls.package_name + ".models")
        cls.catalog = importlib.import_module(cls.package_name + ".local_catalog")
        cls.articles = importlib.import_module(cls.package_name + ".document_articles")
        cls.app.register_blueprint(cls.articles.document_articles_bp)
        cls.app.add_url_rule("/login", endpoint="auth.login", view_func=lambda: "Anmeldung")
        manager = LoginManager(cls.app)
        manager.user_loader(lambda identity: cls.db.session.get(cls.models.User, int(identity)))
        cls.app.before_request(lambda: g.pop("_login_user", None) and None)
        cls.app.jinja_loader = ChoiceLoader([
            DictLoader({"base.html": "{% for category, message in get_flashed_messages(with_categories=true) %}<p>{{ message }}</p>{% endfor %}{% block content %}{% endblock %}"}),
            FileSystemLoader(str(cls.root / "app/templates")),
        ])
        cls.app.jinja_env.globals["csrf_token"] = lambda: "test-token"
        cls.tables = [getattr(cls.models, name).__table__ for name in (
            "User", "AuditLog", "LocalCatalogItem", "CraftActivity", "TimeTaskTemplate", "WowiCompanyCode",
            "Invoice", "Offer", "InvoicePosition", "OfferPosition", "InventoryItem", "InventoryItemType", "InventoryItemGroup",
        )]

    def setUp(self):
        context = self.app.app_context()
        context.push()
        self.addCleanup(context.pop)
        self.addCleanup(self.db.session.remove)
        self.db.metadata.drop_all(self.db.engine, tables=self.tables)
        self.db.metadata.create_all(self.db.engine, tables=self.tables)
        self.user = self.models.User(email="accounting@example.invalid", display_name="Rechnungswesen", can_manage_invoices=True)
        self.company = self.models.WowiCompanyCode(erp_id=1, code="1", name="Test", active=True)
        self.db.session.add_all([self.user, self.company])
        self.db.session.commit()
        self.client = self.app.test_client()
        self.login(self.user)

    def login(self, user):
        with self.client.session_transaction() as session:
            session["_user_id"] = str(user.id)
            session["_fresh"] = True

    def values(self, **changes):
        return {"item_number": "A-100", "name": "Beispielartikel", "business_area": "Installation", "product_group": "Material",
                "unit": "Stück", "price_net": "12,92", "active": "on", "tax_label": "Normalsteuersatz", "vat_rate": "19,00", **changes}

    def article(self, **changes):
        item = self.models.LocalCatalogItem(**self.catalog.catalog_values(self.values(**changes)))
        self.db.session.add(item)
        self.db.session.commit()
        return item

    def import_preview(self, text, default_unit="Stk"):
        response = self.client.post("/admin/local-catalog/import", data={"file": (BytesIO(text.encode("utf-8")), "export.txt"), "default_unit": default_unit})
        self.assertEqual(response.status_code, 200)
        parser = TokenParser()
        parser.feed(response.get_data(as_text=True))
        return response, parser.token

    def test_catalog_access_uses_existing_offer_or_invoice_permissions(self):
        for offers, invoices, admin, expected in [(False, False, False, 403), (True, False, False, 200), (False, True, False, 200), (False, False, True, 200)]:
            with self.subTest(offers=offers, invoices=invoices, admin=admin):
                self.user.can_manage_offers, self.user.can_manage_invoices, self.user.is_admin = offers, invoices, admin
                self.db.session.commit()
                self.assertEqual(self.client.get("/admin/local-catalog").status_code, expected)
                self.assertEqual(self.client.get("/admin/local-catalog/import").status_code, expected)
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get("/admin/local-catalog").status_code, 302)

    def test_denied_users_cannot_write_or_import_catalog_entries(self):
        self.user.can_manage_invoices = False
        self.db.session.commit()
        for path in ("/admin/local-catalog/new", "/admin/local-catalog/import", "/admin/local-catalog/1/edit", "/admin/local-catalog/1/active"):
            self.assertEqual(self.client.post(path, data=self.values()).status_code, 403)
        self.assertEqual(self.models.LocalCatalogItem.query.count(), 0)

    def test_create_edit_and_activation_do_not_change_inventory(self):
        response = self.client.post("/admin/local-catalog/new", data=self.values())
        self.assertEqual(response.status_code, 302)
        item = self.models.LocalCatalogItem.query.one()
        self.assertEqual(item.price_net, Decimal("12.9200"))
        response = self.client.post(f"/admin/local-catalog/{item.id}/edit", data=self.values(name="Neue Bezeichnung", price_net="15,1234"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(item.name, "Neue Bezeichnung")
        self.assertEqual(item.price_net, Decimal("15.1234"))
        self.client.post(f"/admin/local-catalog/{item.id}/active", data={"active": "false"})
        self.assertFalse(item.active)
        self.client.post(f"/admin/local-catalog/{item.id}/active", data={"active": "true"})
        self.assertTrue(item.active)
        self.assertEqual(self.models.InventoryItem.query.count(), 0)
        self.assertEqual(self.models.AuditLog.query.count(), 4)

    def test_variants_share_numbers_but_identical_variants_are_rejected(self):
        self.client.post("/admin/local-catalog/new", data=self.values())
        self.client.post("/admin/local-catalog/new", data=self.values(name="Andere Variante"))
        self.client.post("/admin/local-catalog/new", data=self.values(tax_label="Kein Steuersatz", vat_rate="0,00"))
        response = self.client.post("/admin/local-catalog/new", data=self.values())
        self.assertEqual(response.status_code, 200)
        self.assertIn("bereits vorhanden", response.get_data(as_text=True))
        self.assertEqual(self.models.LocalCatalogItem.query.count(), 3)

    def test_search_filters_and_inactive_items(self):
        self.article(name="Suchbarer Artikel")
        self.article(item_number="B-100", name="Inaktiver Artikel", active=False)
        response = self.client.get("/admin/local-catalog?q=Suchbar&business_area=Installation")
        self.assertIn("Suchbarer Artikel", response.get_data(as_text=True))
        self.assertNotIn("Inaktiver Artikel", response.get_data(as_text=True))
        response = self.client.get("/admin/local-catalog?state=inactive")
        self.assertIn("Inaktiver Artikel", response.get_data(as_text=True))

    def test_invalid_values_do_not_create_an_article(self):
        for values in (self.values(name=""), self.values(price_net="-1"), self.values(vat_rate="101"), self.values(price_net="NaN"), self.values(price_net="1,12345")):
            self.assertEqual(self.client.post("/admin/local-catalog/new", data=values).status_code, 200)
            self.assertEqual(self.models.LocalCatalogItem.query.count(), 0)

    def test_import_preview_shows_values_and_writes_only_after_apply(self):
        text = '"A-100";"Artikel mit \"\"Zitat\"\"";"Installation";"Material";"Stück";"2.235,51";"true";"Normalsteuersatz";"19,00"\n'
        response, token = self.import_preview(text)
        self.assertIsNotNone(token)
        self.assertEqual(self.models.LocalCatalogItem.query.count(), 0)
        self.assertIn("Artikel mit", response.get_data(as_text=True))
        self.assertIn("2235,5100", response.get_data(as_text=True))
        response = self.client.post("/admin/local-catalog/import", data={"action": "apply", "token": token})
        self.assertEqual(response.status_code, 302)
        item = self.models.LocalCatalogItem.query.one()
        self.assertEqual(item.name, 'Artikel mit "Zitat"')
        self.assertEqual(item.price_net, Decimal("2235.5100"))
        self.assertEqual(self.models.InventoryItem.query.count(), 0)

    def test_repeat_import_updates_prices_without_duplicates_or_deleting_other_items(self):
        self.article(item_number="KEEP", name="Nicht in der Datei")
        text = 'A-100;Beispielartikel;Installation;Material;Stück;12,92;true;Normalsteuersatz;19,00\n'
        for value in (text, text, text.replace("12,92", "15,00")):
            _, token = self.import_preview(value)
            self.assertIsNotNone(token)
            self.assertEqual(self.client.post("/admin/local-catalog/import", data={"action": "apply", "token": token}).status_code, 302)
        self.assertEqual(self.models.LocalCatalogItem.query.count(), 2)
        self.assertEqual(self.models.LocalCatalogItem.query.filter_by(item_number="A-100").one().price_net, Decimal("15.0000"))

    def test_import_preserves_variants_and_collapses_exact_duplicates(self):
        text = 'A-100;Variante eins;Installation;Material;Stück;12,92;true;Normalsteuersatz;19,00\nA-100;Variante zwei;Installation;Material;Stück;15,00;false;Kein Steuersatz;0,00\n'
        response, token = self.import_preview(text + text.splitlines()[0] + "\n")
        self.assertIn("1 identische Wiederholungen", response.get_data(as_text=True))
        self.assertIsNotNone(token)
        self.client.post("/admin/local-catalog/import", data={"action": "apply", "token": token})
        items = self.models.LocalCatalogItem.query.order_by(self.models.LocalCatalogItem.id).all()
        self.assertEqual(len(items), 2)
        self.assertTrue(items[0].active)
        self.assertFalse(items[1].active)

    def test_missing_units_are_visible_in_preview_and_use_the_selected_default(self):
        response, token = self.import_preview('A-100;Artikel;;; ;12,92;true;Normalsteuersatz;19,00\n', default_unit="Stück")
        self.assertIn("Fehlende Einheit wird mit Stück ergänzt", response.get_data(as_text=True))
        self.client.post("/admin/local-catalog/import", data={"action": "apply", "token": token})
        self.assertEqual(self.models.LocalCatalogItem.query.one().unit, "Stück")

    def test_import_errors_prevent_partial_writes(self):
        response, token = self.import_preview('A-100;Gültig;Installation;Material;Stück;12,92;true;Normalsteuersatz;19,00\nB-100;Ungültig;;;Stück;kein Preis;true;Normalsteuersatz;19,00\n')
        self.assertIsNone(token)
        self.assertIn("Zeile 2", response.get_data(as_text=True))
        self.assertEqual(self.models.LocalCatalogItem.query.count(), 0)

    def test_tampered_expired_and_other_users_tokens_cannot_import(self):
        _, token = self.import_preview('A-100;Artikel;;;Stück;12,92;true;Normalsteuersatz;19,00\n')
        self.assertIsNotNone(token)
        self.client.post("/admin/local-catalog/import", data={"action": "apply", "token": token + "tampered"})
        with patch("itsdangerous.timed.time.time", return_value=datetime.now(timezone.utc).timestamp() + 1900):
            self.client.post("/admin/local-catalog/import", data={"action": "apply", "token": token})
        other = self.models.User(email="other@example.invalid", display_name="Andere Person", can_manage_offers=True)
        self.db.session.add(other)
        self.db.session.commit()
        self.login(other)
        self.assertEqual(self.client.post("/admin/local-catalog/import", data={"action": "apply", "token": token}).status_code, 403)
        self.assertEqual(self.models.LocalCatalogItem.query.count(), 0)

    def test_active_catalog_entries_remain_selectable_in_both_document_types(self):
        active = self.article()
        self.article(item_number="INACTIVE", name="Inaktiver Artikel", active=False)
        self.user.can_manage_offers = True
        invoice = self.models.Invoice(company_code=self.company, created_by=self.user, company_code_erp_id=1, company_code_code="1", status="draft")
        offer = self.models.Offer(company_code=self.company, created_by=self.user, company_code_erp_id=1, company_code_code="1", status="draft")
        self.db.session.add_all([invoice, offer])
        self.db.session.commit()
        for kind, document in (("offer", offer), ("invoice", invoice)):
            response = self.client.get(f"/admin/document-articles/{kind}/{document.id}?source=local_catalog")
            self.assertEqual(response.status_code, 200)
            self.assertEqual([item["id"] for item in response.json["items"]], [active.id])

    def test_default_article_search_combines_sources_without_confusing_overlapping_ids(self):
        local = self.article(name="Armatur lokal")
        material = self.models.InventoryItem(item_number="L-1", name="Armatur Lager", unit="Stk", active=True, billing_base_price_net=10, billing_vat_rate=19)
        self.db.session.add(material)
        invoice = self.models.Invoice(company_code=self.company, created_by=self.user, company_code_erp_id=1, company_code_code="1", status="draft")
        self.db.session.add(invoice)
        self.db.session.commit()
        response = self.client.get(f"/admin/document-articles/invoice/{invoice.id}?q=Armatur")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([(item["source_type"], item["id"]) for item in response.json["items"]], [("material", material.id), ("local_catalog", local.id)])
        for index in range(55):
            self.article(item_number=f"P-{index}", name=f"Pagination {index:02}")
        first = self.client.get(f"/admin/document-articles/invoice/{invoice.id}?source=all&q=Pagination").json
        second = self.client.get(f"/admin/document-articles/invoice/{invoice.id}?source=all&q=Pagination&page=2").json
        self.assertEqual(len(first["items"]), 50)
        self.assertTrue(first["has_more"])
        self.assertEqual(len(second["items"]), 5)
        self.assertFalse(second["has_more"])
        self.assertFalse({item["id"] for item in first["items"]} & {item["id"] for item in second["items"]})

    def test_parser_accepts_windows_encoding_and_optional_header(self):
        raw = ('Artikelnummer;Bezeichnung;Geschäftsbereich;Warengruppe;Einheit;Nettopreis;Aktiv;Steuersatz;MwSt.\nA-100;Tür;;;Stück;12,92;false;Kein Steuersatz;0,00\n').encode("cp1252")
        result = self.catalog.parse_catalog_import(raw)
        self.assertFalse(result["errors"])
        self.assertEqual(result["rows"][0]["values"]["name"], "Tür")
        self.assertFalse(result["rows"][0]["values"]["active"])

    def test_conflicting_duplicates_and_invalid_csv_are_reported(self):
        for text in ('A-100;Artikel;;;Stück;12,92;true;Normalsteuersatz;19,00\nA-100;Artikel;;;Stück;15,00;true;Normalsteuersatz;19,00\n', 'A-100;"Nicht geschlossen\n', 'A-100;nur zwei Spalten\n'):
            with self.subTest(text=text):
                self.assertTrue(self.catalog.parse_catalog_import(text.encode())["errors"])

    def test_import_limits_and_precision_are_enforced(self):
        with self.assertRaises(ValueError):
            self.catalog.parse_catalog_import(b"x" * (self.catalog.MAX_IMPORT_BYTES + 1))
        for number in ("NaN", "-1,00", "12,12345", "1.2,34", "1e2"):
            result = self.catalog.parse_catalog_import(f'A-100;Artikel;;;Stück;{number};true;Normalsteuersatz;19,00\n'.encode())
            self.assertTrue(result["errors"])

    def test_navigation_separates_accounting_and_preserves_document_permissions(self):
        source = (self.root / "app/templates/base.html").read_text()
        for offers, invoices, admin, foreman in [(True, False, False, False), (False, True, False, False), (False, False, True, False), (False, False, False, True), (False, False, False, False)]:
            with self.subTest(offers=offers, invoices=invoices, admin=admin, foreman=foreman):
                self.user.can_manage_offers, self.user.can_manage_invoices, self.user.is_admin, self.user.is_foreman = offers, invoices, admin, foreman
                with self.app.test_request_context("/admin/local-catalog"):
                    html = self.app.jinja_env.from_string(source).render(current_user=self.user, url_for=lambda endpoint, **kwargs: "/" + endpoint)
                permitted = offers or invoices or admin
                self.assertEqual('id="navAccounting"' in html, permitted)
                self.assertEqual('id="navOrders"' in html, foreman or admin)
                if permitted:
                    start = html.index('id="navAccounting"')
                    section = html[start:html.index("</div>", start)]
                    self.assertIn("Lokaler Katalog", section)
                    self.assertEqual("Angebote" in section, offers or admin)
                    self.assertEqual("Rechnungen" in section, invoices or admin)
                if foreman or admin:
                    start = html.index('id="navOrders"')
                    section = html[start:html.index("</div>", start)]
                    self.assertNotIn("Angebote", section)
                    self.assertNotIn("Rechnungen", section)

    def test_document_contexts_supply_only_active_positions_of_enabled_crafts(self):
        first = self.models.CraftActivity(name="Installation", active=True, app_enabled=True)
        second = self.models.CraftActivity(name="Schreiner", active=True, app_enabled=True)
        disabled = self.models.CraftActivity(name="Ausgeblendet", active=True, app_enabled=False)
        self.db.session.add_all([first, second, disabled])
        self.db.session.flush()
        tasks = [self.models.TimeTaskTemplate(code=code, title=code, active=active, craft_activity=craft) for code, craft, active in (
            ("I-1", first, True), ("S-1", second, True), ("D-1", disabled, True), ("I-2", first, False), ("UNASSIGNED", None, True),
        )]
        self.db.session.add_all(tasks)
        self.db.session.commit()
        source = ast.parse((self.root / "app/admin.py").read_text())
        functions = [node for node in source.body if isinstance(node, ast.FunctionDef) and node.name in {"offer_edit_context", "invoice_edit_context"}]
        namespace = {name: getattr(self.models, name) for name in dir(self.models)}
        namespace.update(Any=object, OFFER_STATUS_LABELS={}, work_report_counts=lambda item: {}, WORK_REPORT_SCOPE_BILLED="billed")
        exec(compile(ast.Module(body=functions, type_ignores=[]), "admin.py", "exec"), namespace)
        for context in (namespace["offer_edit_context"](self.models.Offer(status="draft")), namespace["invoice_edit_context"](self.models.Invoice(status="draft"))):
            self.assertEqual({item["code"] for item in context["catalogue_items"]}, {"I-1", "S-1"})
            self.assertEqual({item["craft_activity_id"] for item in context["catalogue_items"]}, {first.id, second.id})
            self.assertEqual({craft.id for craft in context["craft_activities"]}, {first.id, second.id})

    def test_local_catalog_variant_migration_preserves_existing_articles(self):
        self.check_variant_migration(sa.create_engine("sqlite://"))

    @unittest.skipUnless(os.environ.get("DATLAEUFT_TEST_DATABASE_URL"), "MariaDB-Testdatenbank nicht konfiguriert.")
    def test_local_catalog_variant_migration_with_mariadb(self):
        database_url = os.environ["DATLAEUFT_TEST_DATABASE_URL"]
        database = make_url(database_url)
        if not (database.database or "").startswith("datlaeuft_test_"):
            raise ValueError("Für diese Tests ist eine eigene Testdatenbank erforderlich.")
        self.check_variant_migration(sa.create_engine(database_url))

    def check_variant_migration(self, engine):
        self.addCleanup(engine.dispose)
        if sa.inspect(engine).has_table("local_catalog_item"):
            self.skipTest("Die Migration benötigt eine eigene leere Katalogtabelle.")
        self.addCleanup(lambda: self.drop_migration_table(engine))
        with engine.begin() as connection:
            connection.execute(sa.text("CREATE TABLE local_catalog_item (id INTEGER PRIMARY KEY, item_number VARCHAR(100) NOT NULL, name VARCHAR(255) NOT NULL, business_area VARCHAR(100) NOT NULL, tax_label VARCHAR(80) NOT NULL, vat_rate NUMERIC(5, 2) NOT NULL)"))
            connection.execute(sa.text("CREATE UNIQUE INDEX ix_local_catalog_item_item_number ON local_catalog_item (item_number)"))
            connection.execute(sa.text("INSERT INTO local_catalog_item VALUES (1, 'A-100', 'Artikel', 'Installation', 'Normalsteuersatz', 19)"))
            spec = importlib.util.spec_from_file_location("_catalog_variant_migration", self.root / "migrations/versions/c7e9a1b3d5f8_local_catalog_variants.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with Operations.context(MigrationContext.configure(connection)):
                module.upgrade()
                connection.execute(sa.text("INSERT INTO local_catalog_item VALUES (2, 'A-100', 'Andere Variante', 'Installation', 'Normalsteuersatz', 19)"))
                with self.assertRaises(RuntimeError):
                    module.downgrade()
                connection.execute(sa.text("DELETE FROM local_catalog_item WHERE id = 2"))
                module.downgrade()
            self.assertEqual(connection.execute(sa.text("SELECT name FROM local_catalog_item")).scalar_one(), "Artikel")
            self.assertTrue(next(index for index in sa.inspect(connection).get_indexes("local_catalog_item") if index["name"] == "ix_local_catalog_item_item_number")["unique"])

    def drop_migration_table(self, engine):
        with engine.begin() as connection:
            connection.execute(sa.text("DROP TABLE IF EXISTS local_catalog_item"))
