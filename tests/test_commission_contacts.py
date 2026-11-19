import __future__
import ast
import importlib
import importlib.util
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from pathlib import Path
import types
import unittest
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from alembic.migration import MigrationContext
from alembic.operations import Operations
from flask import current_app, jsonify, request
from jinja2 import Environment, FileSystemLoader
import sqlalchemy as sa

import test_commission_object_levels as fixtures


def load_function(path, name, namespace):
    source = ast.parse(path.read_text())
    function = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == name)
    function.decorator_list = []
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec", flags=__future__.annotations.compiler_flag), namespace)
    return namespace[name]


class CommissionContactInformationTests(unittest.TestCase):
    setUp = fixtures.CommissionObjectLevelTests.setUp
    create_order = fixtures.CommissionObjectLevelTests.create_order
    contact_agreement = fixtures.CommissionObjectLevelTests.contact_agreement
    contact_tenant = fixtures.CommissionObjectLevelTests.contact_tenant

    @classmethod
    def setUpClass(cls):
        fixtures.CommissionObjectLevelTests.setUpClass.__func__(cls)
        cls.templates = importlib.import_module(cls.package_name + ".email_templates")
        cls.contact_lookup = Mock()
        cls.app.add_url_rule("/affected/<int:object_id>", view_func=load_function(cls.root / "app/admin.py", "commission_affected_contacts", {
            "request": request, "jsonify": jsonify, "current_app": current_app,
            "get_commission_object_choice": cls.directory.get_commission_object_choice,
            "WowiportService": lambda: cls.contact_lookup,
        }))

    def test_contact_fields_are_stored_only_for_the_selected_object_scope(self):
        for object_type, object_id in (("building", self.building.id), ("economic_unit", self.economic.id), ("use_unit", self.unit.id), ("license_agreement", self.agreement.id)):
            with self.subTest(object_type=object_type):
                order = self.create_order(object_type, object_id, reporter="  Erika\nTelefon: 123  ", other_affected="  MV-2\nEigener Hinweis  ")
                self.db.session.expire(order)
                self.assertEqual(order.reporter, "Erika\nTelefon: 123" if object_type in {"building", "economic_unit"} else None)
                self.assertEqual(order.other_affected, "MV-2\nEigener Hinweis" if object_type in {"use_unit", "license_agreement"} else None)

    def test_empty_information_has_no_headings_and_api_keeps_both_optional_fields(self):
        order = self.create_order("building", self.building.id, reporter=" \n\t")
        self.assertIsNone(order.reporter)
        self.assertEqual(order.contact_information_text, "")
        self.assertEqual(order.with_contact_information("Auftrag"), "Auftrag")
        payload = self.api.serialize_commission(order)
        self.assertIsNone(payload["reporter"])
        self.assertIsNone(payload["other_affected"])

    def test_api_details_and_list_payloads_preserve_the_edited_text(self):
        order = self.create_order("license_agreement", self.agreement.id, other_affected="Mietvertrag: MV-2\nErika\n-----\nEigener Hinweis")
        for include_details in (False, True):
            payload = self.api.serialize_commission(order, include_details=include_details)
            self.assertEqual(payload["other_affected"], order.other_affected)
            self.assertIsNone(payload["reporter"])

    def test_external_erp_output_appends_information_only_after_the_last_position(self):
        order = self.create_order("building", self.building.id, reporter="Erika\nTelefon: 123")
        order.positions.append(self.models.CommissionPosition(position_index=1, commission_text="Zweite Position", units=1, service_catalog_erp_id=600, craft_activity_erp_id=500))
        order.assignment_type = "external"
        service = self.gateway.WowiportService.__new__(self.gateway.WowiportService)
        payload = service._commission_positions_payload(order, 19)
        self.assertEqual(payload[0]["CommissionText"], "Bitte reparieren")
        self.assertEqual(payload[1]["CommissionText"], "Zweite Position\n\nMelder:\nErika\nTelefon: 123")
        self.assertEqual(order.positions[1].commission_text, "Zweite Position")
        order.assignment_type = "internal"
        self.assertEqual(service._commission_positions_payload(order, 19)[1]["CommissionText"], "Zweite Position")

    def test_erp_readback_keeps_separate_information_without_duplicating_the_order_text(self):
        order = self.create_order("license_agreement", self.agreement.id, other_affected="Mietvertrag: MV-2\nErika")
        text = order.with_contact_information("Bitte reparieren")
        self.assertEqual(order.with_contact_information(text), text)
        for attempt in range(2):
            self.gateway.replace_commission_positions(order, [{"Id": 99, "CommissionText": text, "Units": 1}])
            self.assertEqual(order.positions[0].commission_text, "Bitte reparieren")
            self.assertEqual(order.other_affected, "Mietvertrag: MV-2\nErika")

    def test_reissuing_preserves_both_fields(self):
        order = self.create_order("building", self.building.id, reporter="Erika\nTelefon: 123")
        order.other_affected = "Eigener Hinweis"
        reissue = load_function(self.root / "app/commission_followup.py", "commission_reissue_data", {
            name: getattr(self.models, name) for name in ("WowiLicenseAgreement", "WowiUseUnit", "WowiBuilding", "WowiEconomicUnit", "CraftActivity", "ExternalCraftsman")
        })
        values = reissue(order)["form_values"]
        self.assertEqual(values["reporter"], order.reporter)
        self.assertEqual(values["other_affected"], order.other_affected)

    def agreement_service(self, agreement, contractors=()):
        service = self.gateway.WowiportService.__new__(self.gateway.WowiportService)
        service.client = Mock()
        service.client.get_license_agreements.return_value = [agreement] if agreement else []
        service.client.get_contractors.return_value = list(contractors)
        return service

    def test_all_current_tenants_are_appended_with_contact_details_and_no_birth_dates(self):
        tenant = self.contact_tenant(1, "Erika")
        tenant.person.communications.extend([
            types.SimpleNamespace(content="erika@example.invalid", communication_type=types.SimpleNamespace(id_=5)),
            types.SimpleNamespace(content="02306 123", communication_type=types.SimpleNamespace(id_=1)),
            types.SimpleNamespace(content="02306 456", communication_type=types.SimpleNamespace(id_=1)),
        ])
        service = self.agreement_service(self.contact_agreement(4004), [tenant, self.contact_tenant(2, "Max"), tenant])
        text = service.get_affected_agreement_text(4004, "MV-1")
        self.assertTrue(text.startswith("Mietvertrag: MV-1"))
        for value in ("Erika", "Max", "Mobil: 0123456789", "Telefon: 02306 123", "Telefon: 02306 456", "E-Mail: erika@example.invalid"):
            self.assertIn(value, text)
        self.assertEqual(text.count("Erika"), 1)
        self.assertNotIn("Geburtsdatum", text)
        service.client.get_contractors.assert_called_once_with(license_agreement_id=4004, fetch_all=True)

    def test_former_and_future_tenants_are_excluded_and_today_is_inclusive(self):
        today = datetime.now(ZoneInfo("Europe/Berlin")).date()
        former = self.contact_tenant(1, "Ausgezogen", end=(today - timedelta(days=1)).isoformat())
        future = self.contact_tenant(2, "Zukünftig")
        future.contractual_use_valid_from = (today + timedelta(days=1)).isoformat()
        current = self.contact_tenant(3, "Aktuell", end=today.isoformat())
        current.contractual_use_valid_from = today.isoformat()
        service = self.agreement_service(self.contact_agreement(4004), [former, future, current])
        text = service.get_affected_agreement_text(4004, "MV-1")
        self.assertIn("Aktuell", text)
        self.assertNotIn("Ausgezogen", text)
        self.assertNotIn("Zukünftig", text)

    def test_future_expired_and_vacant_contracts_do_not_disclose_current_contact_details(self):
        today = datetime.now(ZoneInfo("Europe/Berlin")).date()
        for agreement in (
            self.contact_agreement(4004, start=(today + timedelta(days=1)).isoformat()),
            self.contact_agreement(4004, end=(today - timedelta(days=1)).isoformat()),
            self.contact_agreement(4004, vacancy=True),
        ):
            service = self.agreement_service(agreement, [self.contact_tenant(1, "Erika")])
            self.assertEqual(service.get_affected_agreement_text(4004, "MV-1"), "Mietvertrag: MV-1")
            service.client.get_contractors.assert_not_called()

    def test_missing_and_wrong_contracts_are_rejected(self):
        for agreement in (None, self.contact_agreement(9999)):
            with self.assertRaises(ValueError):
                self.agreement_service(agreement).get_affected_agreement_text(4004, "MV-1")

    def test_live_contract_numbers_are_used_when_the_directory_number_is_missing_or_outdated(self):
        agreement = self.contact_agreement(4004)
        agreement.id_num = "MV-AKTUELL"
        service = self.agreement_service(agreement)
        for number in (None, "MV-ALT"):
            self.assertEqual(service.get_affected_agreement_text(4004, number), "Mietvertrag: MV-AKTUELL")

    def test_a_contract_without_any_number_reports_an_error(self):
        service = self.agreement_service(self.contact_agreement(4004))
        with self.assertRaisesRegex(ValueError, "MV-Nummer"):
            service.get_affected_agreement_text(4004, None)

    def test_contact_endpoint_resolves_the_local_contract_before_fetching_contacts(self):
        self.contact_lookup.reset_mock(side_effect=True)
        self.contact_lookup.get_affected_agreement_text.return_value = "Mietvertrag: MV-COMMON\nErika"
        response = self.client.get(f"/affected/{self.agreement.id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["text"], "Mietvertrag: MV-COMMON\nErika")
        self.contact_lookup.get_affected_agreement_text.assert_called_once_with(4004, "MV-COMMON")
        self.assertEqual(self.client.get(f"/affected/{self.building.id}").status_code, 400)

    def test_contact_endpoint_reports_erp_failures(self):
        self.contact_lookup.get_affected_agreement_text.side_effect = RuntimeError("Unavailable")
        self.addCleanup(self.contact_lookup.reset_mock, side_effect=True)
        response = self.client.get(f"/affected/{self.agreement.id}")
        self.assertEqual(response.status_code, 502)
        self.assertIn("erneut versuchen", response.json["message"])

    def send_assignment(self, order):
        sent = Mock()
        sender = load_function(self.root / "app/external_craftsman_service.py", "send_external_assignment_email", {
            "EmailMessage": EmailMessage,
            "external_mail_configuration": lambda: ("https://portal.example.invalid", "smtp.example.invalid", 587, "office@example.invalid"),
            "commission_email_context": self.templates.commission_email_context,
            "apply_email_template": self.templates.apply_email_template,
            "set_email_content": self.templates.set_email_content,
            "send_external_email": sent,
        })
        sender(types.SimpleNamespace(commission=order, external_craftsman=types.SimpleNamespace(display_name="Firma", email="firma@example.invalid")))
        return sent.call_args.args[0]

    def test_external_assignment_includes_each_section_in_plain_text_and_html(self):
        order = self.create_order("building", self.building.id, reporter="Erika <Beispiel>\nTelefon: 123")
        order.other_affected = "Mietvertrag: MV-2\nMax"
        message = self.send_assignment(order)
        for body in (message.get_body(preferencelist=("plain",)).get_content(), message.get_body(preferencelist=("html",)).get_content()):
            self.assertEqual(body.count("Melder:"), 1)
            self.assertEqual(body.count("Weitere Betroffene:"), 1)
            self.assertLess(body.index("Bitte reparieren"), body.index("Melder:"))
            self.assertIn("Mietvertrag: MV-2", body)
        self.assertIn("&lt;Beispiel&gt;", message.get_body(preferencelist=("html",)).get_content())

    def test_external_assignment_omits_empty_sections(self):
        message = self.send_assignment(self.create_order("building", self.building.id))
        self.assertNotIn("Melder:", message.get_body(preferencelist=("plain",)).get_content())
        self.assertNotIn("Weitere Betroffene:", message.get_body(preferencelist=("html",)).get_content())

    def preview_context(self, **arguments):
        utilities = importlib.import_module(self.package_name + ".utils")
        namespace = {name: getattr(self.models, name) for name in ("CraftActivity", "ExternalCraftsman", "TimeTaskTemplate", "User")}
        namespace.update({
            "db": self.db, "get_commission_object_choice": self.directory.get_commission_object_choice,
            "format_local_datetime": utilities.format_local_datetime,
            "parse_local_datetime": utilities.parse_local_datetime,
            "Decimal": Decimal, "InvalidOperation": InvalidOperation,
        })
        for name in ("optional_form_decimal", "required_position_decimal"):
            load_function(self.root / "app/admin.py", name, namespace)
        return load_function(self.root / "app/admin.py", "commission_preview_context", namespace)(**arguments)

    def test_saved_order_pdf_includes_filled_sections_after_the_order_text(self):
        order = self.create_order("building", self.building.id, reporter="Erika <Beispiel>\nTelefon: 123")
        context = self.preview_context(commission=order)
        template = Environment(loader=FileSystemLoader(self.root / "app/templates"), autoescape=True).get_template("admin/commission_pdf.html")
        html = template.render(context)
        self.assertIn("Melder:", html)
        self.assertIn("Erika &lt;Beispiel&gt;", html)
        self.assertGreater(html.index("Melder:"), html.index("Bitte reparieren"))
        self.assertNotIn("Weitere Betroffene:", html)

    def test_draft_preview_uses_the_same_object_scope_as_saving(self):
        for object_type, object_id in (("building", self.building.id), ("license_agreement", self.agreement.id)):
            context = self.preview_context(data={
                "craft_activity_id": self.craft.id, "object_type": object_type, "object_id": object_id,
                "reporter": "Erika", "other_affected": "MV-2",
                "positions": [{"service_catalog_id": 600, "commission_text": "Reparatur", "units": "1"}],
            })
            self.assertEqual(context["reporter"], "Erika" if object_type == "building" else None)
            self.assertEqual(context["other_affected"], "MV-2" if object_type == "license_agreement" else None)


class CommissionContactMigrationTests(unittest.TestCase):
    def test_migration_preserves_existing_orders_and_can_be_reversed(self):
        path = Path(__file__).resolve().parents[1] / "migrations/versions/91e3c5a7b9d2_commission_contacts.py"
        spec = importlib.util.spec_from_file_location("commission_contact_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite://")
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE commission (id INTEGER PRIMARY KEY, title TEXT)")
            connection.exec_driver_sql("INSERT INTO commission VALUES (1, 'Reparatur')")
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()
            self.assertEqual(tuple(connection.exec_driver_sql("SELECT title, reporter, other_affected FROM commission").one()), ("Reparatur", None, None))
            migration.downgrade()
            self.assertEqual([column["name"] for column in sa.inspect(connection).get_columns("commission")], ["id", "title"])
        engine.dispose()
