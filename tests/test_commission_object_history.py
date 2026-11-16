import ast
from functools import wraps
import importlib
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

from flask import Flask, abort, g, redirect, request, url_for
from flask_login import LoginManager, current_user, login_required
from flask_sqlalchemy import SQLAlchemy
import sqlalchemy as sa
from sqlalchemy.engine import make_url


class CommissionObjectHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.db = SQLAlchemy()
        cls.app = Flask(__name__)
        cls.app.config.update(SQLALCHEMY_DATABASE_URI="sqlite://", TESTING=True, SECRET_KEY="object-history-test")
        cls.db.init_app(cls.app)
        cls.package_name = "_commission_object_history_test_app"
        package = types.ModuleType(cls.package_name)
        package.__path__ = [str(cls.root / "app")]
        extensions = types.ModuleType(cls.package_name + ".extensions")
        extensions.db = cls.db
        extensions.jwt = Mock()
        modules = {cls.package_name: package, extensions.__name__: extensions}
        source = ast.parse((cls.root / "app/admin.py").read_text())
        for node in ast.walk(source):
            if not isinstance(node, ast.ImportFrom) or node.level != 1 or node.module in {"models", "extensions", "blacklist", "utils", "commission_object_history"}:
                continue
            name = cls.package_name + "." + node.module
            module = modules.setdefault(name, types.ModuleType(name))
            for item in node.names:
                setattr(module, item.name, login_required if node.module == "auth" else Mock())
        auth_source = ast.parse((cls.root / "app/auth.py").read_text())
        function = next(node for node in auth_source.body if isinstance(node, ast.FunctionDef) and node.name == "foreman_required")
        namespace = {"F": object, "Any": object, "wraps": wraps, "current_user": current_user, "abort": abort, "redirect": redirect, "request": request, "url_for": url_for}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "auth.py", "exec"), namespace)
        modules[cls.package_name + ".auth"].foreman_required = namespace["foreman_required"]
        module_patch = patch.dict(sys.modules, modules)
        module_patch.start()
        cls.addClassCleanup(module_patch.stop)
        cls.models = importlib.import_module(cls.package_name + ".models")
        cls.history = importlib.import_module(cls.package_name + ".commission_object_history")
        cls.admin = importlib.import_module(cls.package_name + ".admin")
        cls.app.register_blueprint(cls.admin.admin_bp)
        cls.app.add_url_rule("/login", endpoint="auth.login", view_func=lambda: "Login")
        manager = LoginManager(cls.app)
        manager.user_loader(lambda identity: cls.db.session.get(cls.models.User, int(identity)))
        cls.tables = [model.__table__ for model in (
            cls.models.User, cls.models.BlacklistEntry, cls.models.Commission, cls.models.CommissionPosition, cls.models.ExternalCraftsman,
        )]

    def setUp(self):
        context = self.app.app_context()
        context.push()
        self.addCleanup(context.pop)
        self.addCleanup(self.db.session.remove)
        self.db.metadata.drop_all(self.db.engine, tables=self.tables)
        self.db.metadata.create_all(self.db.engine, tables=self.tables)
        self.user = self.models.User(email="office@example.invalid", display_name="Sachbearbeiter", is_foreman=True)
        self.other = self.models.User(email="other@example.invalid", display_name="Andere Sachbearbeiterin")
        self.db.session.add_all([self.user, self.other])
        self.db.session.commit()
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["_user_id"] = str(self.user.id)
            session["_fresh"] = True
        self.choice = {
            "object_type": "license_agreement", "object_id": 44,
            "company_code": {"erp_id": 77, "code": "BW"},
            "economic_unit": {"erp_id": 1001, "id_num": "WE-1"},
            "building": {"erp_id": 2002, "id_num": "GE-1"},
            "use_unit": {"erp_id": 3003, "id_num": "NE-1"},
            "license_agreement": {"erp_id": 4004, "id_num": "MV-1"},
        }
        self.admin.get_commission_object_choice.reset_mock()
        self.admin.get_commission_object_choice.return_value = self.choice
        self.admin.get_commission_object_choice.side_effect = None

    def commission(self, object_type="license_agreement", *, explicit=True, **values):
        references = {}
        for name in ("economic_unit", "building", "use_unit", "license_agreement"):
            _, id_column, number_column = self.history.OBJECT_REFERENCES[name]
            references.update({id_column.key: self.choice[name]["erp_id"], number_column.key: self.choice[name]["id_num"]})
            if name == object_type:
                break
        item = self.models.Commission(
            **{**references, "raw_payload": {"commission_scope": {"object_type": object_type}} if explicit else None,
               "company_code_erp_id": 77, "company_code_code": "BW", "created_by": self.other,
               "local_status": "open", "title": "Wasserhahn reparieren", "description": "Tropft in der Küche", **values},
        )
        self.db.session.add(item)
        self.db.session.commit()
        return item

    def response(self, **parameters):
        g.pop("_login_user", None)
        return self.client.get("/admin/commission-objects/commissions", query_string={
            "object_type": self.choice["object_type"], "object_id": self.choice["object_id"], **parameters,
        })

    def test_each_selected_level_matches_only_its_exact_orders_including_legacy_scopes(self):
        orders = {name: [self.commission(name), self.commission(name, explicit=False)] for name in self.history.OBJECT_REFERENCES}
        economic = orders["economic_unit"][0]
        economic.building_erp_id = 2002
        economic.building_idnum = "GE-1"
        self.db.session.commit()
        for name, expected in orders.items():
            with self.subTest(level=name):
                self.choice["object_type"] = name
                response = self.response()
                self.assertEqual(response.status_code, 200)
                self.assertEqual({item["id"] for item in response.json["items"]}, {item.id for item in expected})
                self.assertEqual(response.json["open_count"], 2)
                self.assertEqual(response.json["total"], 2)

    def test_open_orders_include_pending_orders_and_incomplete_work_but_not_completed_work(self):
        open_items = [self.commission(local_status=status) for status in ("recorded", "open", "waiting_for_acceptance", "in_progress")]
        open_items.append(self.commission(local_status="completion_review", completion_successful=False, status_id=10))
        for values in (
            {"local_status": "done"}, {"local_status": "awaiting_invoice"}, {"local_status": "tenant_charge_review"},
            {"completed_at": self.models.utc_now()}, {"status_id": 9}, {"status_id": 10}, {"status_id": 11},
        ):
            self.commission(**values)
        self.commission(deleted_at=self.models.utc_now())
        self.commission(local_status="deleted")
        response = self.response(summary="true")
        self.assertEqual(response.json, {"total": 12, "open_count": 5})
        response = self.response(view="open")
        self.assertEqual({item["id"] for item in response.json["items"]}, {item.id for item in open_items})
        self.assertTrue(all(item["is_open"] for item in response.json["items"]))

    def test_parent_levels_are_opt_in_and_keep_the_exact_selection_counts(self):
        orders = {name: self.commission(name) for name in self.history.OBJECT_REFERENCES}
        self.commission("license_agreement", local_status="done")
        self.commission("building", building_erp_id=9999)
        self.commission("use_unit", use_unit_id=9999)
        self.commission("license_agreement", license_agreement_id=9999)
        self.commission("economic_unit", economic_unit_erp_id=9999)
        self.assertEqual(self.response(summary="true").json, {"total": 2, "open_count": 1})
        expanded = self.response(levels=",".join(self.history.OBJECT_REFERENCES)).json
        self.assertEqual((expanded["total"], expanded["open_count"]), (5, 4))
        self.assertEqual(expanded["selection_counts"], {"total": 2, "open_count": 1})
        self.assertTrue({item.id for item in orders.values()} <= {item["id"] for item in expanded["items"]})
        self.assertEqual({item["object_type"] for item in expanded["items"]}, set(self.history.OBJECT_REFERENCES))
        building_row = next(item for item in expanded["items"] if item["object_type"] == "building")
        self.assertEqual(building_row["object_label"], "Gebäude GE-1")
        self.assertEqual(self.response(summary="true", levels="building,economic_unit").json, {"total": 2, "open_count": 1})

    def test_parent_only_levels_include_legacy_orders_and_never_descendants(self):
        building = self.commission("building", explicit=False)
        economic = self.commission("economic_unit", building_erp_id=2002, building_idnum="GE-1")
        self.commission("license_agreement")
        self.commission("use_unit")
        result = self.response(levels="building,economic_unit,building").json
        self.assertEqual({item["id"] for item in result["items"]}, {building.id, economic.id})
        self.assertEqual(result["levels"], ["building", "economic_unit"])
        self.assertEqual(result["selection_counts"], {"total": 1, "open_count": 1})
        self.choice["object_type"] = "building"
        result = self.response(levels="building,economic_unit").json
        self.assertEqual({item["id"] for item in result["items"]}, {building.id, economic.id})

    def test_invalid_unrelated_or_unavailable_history_levels_are_rejected(self):
        self.choice["object_type"] = "building"
        for levels in ("", "all", "use_unit", "license_agreement", "building,", "building,other"):
            with self.subTest(levels=levels):
                self.assertEqual(self.response(levels=levels).status_code, 400)
        self.choice["economic_unit"] = None
        self.assertEqual(self.response(levels="economic_unit").status_code, 400)

    def test_expanded_levels_keep_open_filter_pagination_and_exact_counts(self):
        self.commission("license_agreement", local_status="done")
        first = self.commission("building")
        for _ in range(22):
            self.commission("economic_unit", local_status="done")
        levels = "license_agreement,building,economic_unit"
        opened = self.response(levels=levels, view="open").json
        self.assertEqual([item["id"] for item in opened["items"]], [first.id])
        self.assertEqual((opened["total"], opened["open_count"], opened["pages"]), (24, 1, 1))
        self.assertEqual(opened["selection_counts"], {"total": 1, "open_count": 0})
        second = self.response(levels=levels, page=2).json
        self.assertEqual((len(second["items"]), second["pages"]), (4, 2))

    def test_public_number_fallback_requires_the_same_company_and_cannot_override_an_erp_id(self):
        legacy = self.commission(license_agreement_id=None)
        self.commission(license_agreement_id=None, company_code_erp_id=999, company_code_code="ANDERS")
        self.commission(license_agreement_id=4999)
        code_only = self.commission(license_agreement_id=None, company_code_erp_id=None)
        response = self.response()
        self.assertEqual({item["id"] for item in response.json["items"]}, {legacy.id, code_only.id})

    def test_history_includes_other_authors_full_position_texts_and_external_craftsmen(self):
        self.db.session.add(self.models.ExternalCraftsman(craftsman_erp_id=5005, display_name="Externer Betrieb"))
        item = self.commission(assignment_type="external", craftsman_erp_id=5005, erp_idnum="01-2026-12345")
        self.db.session.add_all([
            self.models.CommissionPosition(commission=item, position_index=0, internal_description="Standardarbeit", commission_text="Wasserhahn tropft seit gestern"),
            self.models.CommissionPosition(commission=item, position_index=1, commission_text="Dichtung im Bad austauschen"),
        ])
        self.db.session.commit()
        row = self.response().json["items"][0]
        self.assertEqual(row["created_by"], "Andere Sachbearbeiterin")
        self.assertEqual(row["craftsman"], "Externer Betrieb")
        self.assertIn("Wasserhahn tropft seit gestern", row["description"])
        self.assertIn("Dichtung im Bad austauschen", row["description"])
        self.assertEqual(row["number"], "01-2026-12345")
        self.assertEqual(row["url"], f"/admin/commissions/{item.id}")

    def test_history_pages_all_orders_without_hiding_older_open_orders(self):
        first = self.commission()
        for _ in range(23):
            self.commission(local_status="done")
        response = self.response()
        self.assertEqual(response.json["items"][0]["id"], first.id)
        self.assertEqual(len(response.json["items"]), 20)
        second = self.response(page=2).json
        self.assertEqual(len(second["items"]), 4)
        self.assertFalse({item["id"] for item in second["items"]} & {item["id"] for item in response.json["items"]})
        self.assertEqual((second["total"], second["open_count"], second["pages"]), (24, 1, 2))

    def test_empty_history_and_invalid_or_unavailable_selections_are_explicit(self):
        self.assertEqual(self.response(summary="true").json, {"total": 0, "open_count": 0})
        for parameters in ({"object_type": "all"}, {"object_id": "not-an-id"}, {"object_id": 0}, {"page": "x"}, {"page": -1}, {"view": "deleted"}):
            with self.subTest(parameters=parameters):
                self.assertEqual(self.response(**parameters).status_code, 400)
        self.admin.get_commission_object_choice.side_effect = ValueError("Der Mietvertrag ist nicht verfügbar.")
        self.assertEqual(self.response().status_code, 400)

    def test_endpoint_keeps_foreman_and_admin_permissions(self):
        self.assertEqual(self.response().status_code, 200)
        self.admin.get_commission_object_choice.assert_called_with("license_agreement", 44)
        self.user.is_foreman = False
        self.db.session.commit()
        self.assertEqual(self.response().status_code, 403)
        self.user.is_admin = True
        self.db.session.commit()
        self.assertEqual(self.response().status_code, 200)
        with self.client.session_transaction() as session:
            session.clear()
        self.assertEqual(self.response().status_code, 302)

    def test_scope_predicates_match_on_the_configured_database(self):
        database_url = os.environ.get("DATLAEUFT_TEST_DATABASE_URL", "sqlite://")
        url = make_url(database_url)
        if url.get_backend_name() != "sqlite" and not (url.database or "").startswith("datlaeuft_test"):
            self.fail("Diese Prüfung benötigt eine separate datlaeuft_test-Datenbank.")
        engine = sa.create_engine(database_url)
        metadata = sa.MetaData()
        columns = [sa.Column("id", sa.Integer(), primary_key=True), sa.Column("raw_payload", sa.JSON()),
                   sa.Column("local_status", sa.String(40)), sa.Column("deleted_at", sa.DateTime()),
                   sa.Column("company_code_erp_id", sa.Integer()), sa.Column("company_code_code", sa.String(40))]
        for _, id_column, number_column in self.history.OBJECT_REFERENCES.values():
            columns.extend((sa.Column(id_column.key, sa.Integer()), sa.Column(number_column.key, sa.String(100))))
        table = sa.Table("commission", metadata, *columns)
        try:
            with engine.begin() as connection:
                metadata.create_all(connection)
                connection.execute(table.insert(), [{"use_unit_id": None, "license_agreement_id": None, **row} for row in [
                    {"id": 1, "local_status": "open", "economic_unit_erp_id": 1001, "building_erp_id": 2002, "raw_payload": {"commission_scope": {"object_type": "economic_unit"}}},
                    {"id": 2, "local_status": "open", "economic_unit_erp_id": 1001, "building_erp_id": 2002, "raw_payload": None},
                    {"id": 3, "local_status": "open", "economic_unit_erp_id": 1001, "building_erp_id": 2002, "use_unit_id": 3003, "raw_payload": None},
                    {"id": 4, "local_status": "open", "economic_unit_erp_id": 1001, "building_erp_id": 2002, "use_unit_id": 3003, "license_agreement_id": 4004, "raw_payload": None},
                    {"id": 5, "local_status": "open", "economic_unit_erp_id": 1001, "building_erp_id": 9999, "raw_payload": {"commission_scope": {"object_type": "building"}}},
                ]])
                for name, expected in (("economic_unit", [1]), ("building", [2])):
                    self.choice["object_type"] = name
                    statement = self.history.commission_object_history_query(self.choice).with_entities(self.models.Commission.id).statement
                    self.assertEqual(list(connection.scalars(statement)), expected)
                self.choice["object_type"] = "license_agreement"
                for levels, expected in ((["building", "economic_unit"], [1, 2]),
                                         (["license_agreement", "use_unit", "building", "economic_unit"], [1, 2, 3, 4])):
                    statement = self.history.commission_object_history_query(self.choice, levels).with_entities(self.models.Commission.id).order_by(self.models.Commission.id).statement
                    self.assertEqual(list(connection.scalars(statement)), expected)
                metadata.drop_all(connection)
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
