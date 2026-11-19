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
from unittest.mock import Mock, patch
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
    contact_agreement = fixtures.CommissionObjectLevelTests.contact_agreement
    contact_tenant = fixtures.CommissionObjectLevelTests.contact_tenant

    def setUp(self):
        fixtures.CommissionObjectLevelTests.setUp(self)
        self.reporter_contact = Mock()
        self.reporter_contact.get_reporter_contact_item.return_value = {
            "text": "Erika\nTelefon: 123", "name": "Erika", "agreement_number": "MV-COMMON",
        }
        service_patch = patch.object(self.directory, "WowiportService", return_value=self.reporter_contact)
        service_patch.start()
        self.addCleanup(service_patch.stop)

    def create_order(self, object_type, object_id, **extra):
        reporter = extra.get("reporter")
        if reporter and reporter.strip() and object_type in {"building", "economic_unit"}:
            extra.update(reporter_agreement_id=self.agreement.id, reporter_contractor_id=self.tenant.erp_id)
            self.reporter_contact.get_reporter_contact_item.return_value = {
                "text": reporter.strip(), "name": "Erika", "agreement_number": "MV-COMMON",
            }
        return fixtures.CommissionObjectLevelTests.create_order(self, object_type, object_id, **extra)

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
        cls.app.add_url_rule("/reporter/search", view_func=load_function(cls.root / "app/admin.py", "commission_reporter_search", {
            "request": request, "jsonify": jsonify, "search_commission_reporters": cls.directory.search_commission_reporters,
        }))
        cls.app.add_url_rule("/reporter/<int:agreement_id>/<int:contractor_id>", view_func=load_function(cls.root / "app/admin.py", "commission_reporter_contact", {
            "jsonify": jsonify, "current_app": current_app, "resolve_commission_reporter": cls.directory.resolve_commission_reporter,
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
        self.assertEqual(values["reporter_agreement_id"], self.agreement.id)
        self.assertEqual(values["reporter_contractor_id"], self.tenant.erp_id)

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
            "resolve_commission_reporter": self.directory.resolve_commission_reporter,
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
                "reporter_agreement_id": self.agreement.id, "reporter_contractor_id": self.tenant.erp_id,
                "positions": [{"service_catalog_id": 600, "commission_text": "Reparatur", "units": "1"}],
            })
            self.assertEqual(context["reporter"], "Erika" if object_type == "building" else None)
            self.assertEqual(context["other_affected"], "MV-2" if object_type == "license_agreement" else None)

    def add_reporter_tenant(self, name, *, start=None, end=None, active=True, identity=6001):
        tenant = self.models.WowiContractor(
            erp_id=identity, license_agreement_erp_id=self.agreement.erp_id, license_agreement=self.agreement,
            display_name=name, contractual_use_from=start, contractual_use_to=end, active=active,
        )
        self.db.session.add(tenant)
        self.db.session.commit()
        return tenant

    def test_reporter_search_finds_contracts_by_number_address_and_tenant_name(self):
        for query in ("MV-COMMON", "Erika", "COMMON Straße 1", "erika COMMON"):
            with self.subTest(query=query):
                items = self.directory.search_commission_reporters(query)
                self.assertEqual([item["object_type"] for item in items], ["license_agreement"])
                self.assertEqual(items[0]["object_id"], self.agreement.id)
                self.assertEqual(items[0]["tenants"], [{"contractor_id": self.tenant.erp_id, "name": self.tenant.display_name}])

    def test_reporter_search_lists_only_current_tenants_including_today(self):
        today = self.directory.directory_today()
        current = self.add_reporter_tenant("Max Aktuell", start=today, end=today)
        self.add_reporter_tenant("Ausgezogen", end=today - timedelta(days=1), identity=6002)
        self.add_reporter_tenant("Zukünftig", start=today + timedelta(days=1), identity=6003)
        self.add_reporter_tenant("Inaktiv", active=False, identity=6004)
        items = self.directory.search_commission_reporters("MV-COMMON")
        self.assertEqual({tenant["contractor_id"] for tenant in items[0]["tenants"]}, {self.tenant.erp_id, current.erp_id})
        for name in ("Ausgezogen", "Zukünftig", "Inaktiv"):
            self.assertEqual(self.directory.search_commission_reporters(name), [])

    def test_reporter_search_excludes_noncurrent_or_unavailable_contracts(self):
        today = self.directory.directory_today()
        for record, attribute, value in (
            (self.agreement, "start_date", today + timedelta(days=1)),
            (self.agreement, "end_date", today - timedelta(days=1)),
            (self.agreement, "is_vacancy", True),
            (self.agreement, "active", False),
            (self.unit, "active", False),
            (self.building, "active", False),
            (self.economic, "active", False),
        ):
            with self.subTest(attribute=attribute, value=value):
                previous = getattr(record, attribute)
                setattr(record, attribute, value)
                self.db.session.flush()
                self.assertEqual(self.directory.search_commission_reporters("COMMON"), [])
                setattr(record, attribute, previous)

    def test_reporter_search_has_a_two_character_minimum_and_escapes_wildcards(self):
        for query in ("", "a", "%%", "__"):
            self.assertEqual(self.directory.search_commission_reporters(query), [])
        response = self.client.get("/reporter/search?q=Erika")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["items"][0]["tenants"][0]["name"], "Erika COMMON")

    def test_reporter_contact_endpoint_returns_exactly_the_selected_tenant(self):
        self.add_reporter_tenant("Max COMMON")
        response = self.client.get(f"/reporter/{self.agreement.id}/{self.tenant.erp_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["item"]["text"], "Erika\nTelefon: 123")
        self.assertEqual(response.json["item"]["contractor_id"], self.tenant.erp_id)
        self.assertEqual(response.json["item"]["agreement_id"], self.agreement.id)
        self.reporter_contact.get_reporter_contact_item.assert_called_once_with(4004, 5005, on_date=self.directory.directory_today())

    def test_reporter_contact_rejects_other_or_expired_tenants_before_loading_personal_data(self):
        former = self.add_reporter_tenant("Ausgezogen", end=self.directory.directory_today() - timedelta(days=1))
        for agreement_id, contractor_id in ((self.building.id, self.tenant.erp_id), (self.agreement.id, 999999), (self.agreement.id, former.erp_id)):
            response = self.client.get(f"/reporter/{agreement_id}/{contractor_id}")
            self.assertEqual(response.status_code, 400)
        self.reporter_contact.get_reporter_contact_item.assert_not_called()

    def test_reporter_contact_errors_are_reported_without_a_partial_selection(self):
        self.reporter_contact.get_reporter_contact_item.side_effect = RuntimeError("Connection lost")
        response = self.client.get(f"/reporter/{self.agreement.id}/{self.tenant.erp_id}")
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("item", response.json)

    def test_reporter_selection_rejects_free_text_incomplete_or_multiple_identities(self):
        for data in (
            {"reporter": "Erika und Max"}, {"reporter_agreement_id": self.agreement.id},
            {"reporter_agreement_id": self.agreement.id, "reporter_contractor_id": [5005, 6001]},
            {"reporter_agreement_id": self.agreement.id, "reporter_contractor_id": True},
            {"reporter_agreement_id": self.agreement.id, "reporter_contractor_id": 5005.0},
            {"reporter_agreement_id": 0, "reporter_contractor_id": 5005},
        ):
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.directory.resolve_commission_reporter(data)
        self.reporter_contact.get_reporter_contact_item.assert_not_called()

    def test_saving_preserves_edited_text_and_the_validated_reporter_selection(self):
        edited = "Erika Beispiel\nTelefon: 987\nBitte erst nach 17 Uhr anrufen"
        order = fixtures.CommissionObjectLevelTests.create_order(self, "building", self.building.id,
            reporter=edited, reporter_agreement_id=str(self.agreement.id), reporter_contractor_id=str(self.tenant.erp_id))
        self.db.session.expire(order)
        self.assertEqual(order.reporter, edited)
        self.assertEqual(order.raw_payload["reporter_selection"]["contractor_id"], 5005)
        self.assertEqual(order.raw_payload["reporter_selection"]["agreement_id"], self.agreement.id)
        self.assertEqual(self.api.serialize_commission(order)["reporter"], edited)
        self.assertEqual(self.preview_context(commission=order)["reporter"], edited)
        message = self.send_assignment(order)
        self.assertIn(edited, message.get_body(preferencelist=("plain",)).get_content())
        self.reporter_contact.get_reporter_contact_item.assert_called_once_with(4004, 5005, on_date=self.directory.directory_today())

    def test_a_deliberately_empty_reporter_text_is_not_replaced_with_automatic_contacts(self):
        for value in ("", " \n\t", None):
            with self.subTest(value=value):
                order = fixtures.CommissionObjectLevelTests.create_order(self, "building", self.building.id,
                    reporter=value, reporter_agreement_id=self.agreement.id, reporter_contractor_id=self.tenant.erp_id)
                self.assertIsNone(order.reporter)
                self.assertIsNone(self.api.serialize_commission(order)["reporter"])
                self.assertEqual(order.contact_information_text, "")
                self.assertEqual(order.raw_payload["reporter_selection"]["contractor_id"], self.tenant.erp_id)

    def test_reporter_text_must_be_a_text_value_after_selecting_a_valid_tenant(self):
        for value in (["Erika", "Max"], {"name": "Erika"}, 123, True):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "als Text"):
                self.directory.resolve_commission_reporter({
                    "reporter_agreement_id": self.agreement.id, "reporter_contractor_id": self.tenant.erp_id, "reporter": value,
                })
        self.reporter_contact.get_reporter_contact_item.assert_not_called()

    def test_a_reporter_no_longer_valid_at_save_time_does_not_create_an_order(self):
        self.reporter_contact.get_reporter_contact_item.side_effect = ValueError("Mieter ist ausgezogen")
        with self.assertRaisesRegex(ValueError, "ausgezogen"):
            fixtures.CommissionObjectLevelTests.create_order(self, "building", self.building.id,
                reporter_agreement_id=self.agreement.id, reporter_contractor_id=self.tenant.erp_id)
        self.assertEqual(self.models.Commission.query.count(), 0)
        self.service.enqueue_erp_operation.assert_not_called()

    def test_erp_sync_preserves_the_reporter_selection_and_contact_snapshot(self):
        order = self.create_order("building", self.building.id, reporter="Erika\nTelefon: 123")
        order.erp_id = 100
        selection = dict(order.raw_payload["reporter_selection"])
        self.db.session.commit()
        with patch.object(self.gateway, "configured_commission_type_ids", return_value={100}), patch.object(self.gateway, "configured_commission_status_ids", return_value={12}):
            self.gateway.upsert_commission({
                "Id": 100, "IdNum": "A-100", "CommissionStatus": {"Id": 12, "Code": "WaitForCraftsmanFeedback"},
                "CommissionType": {"Id": 100}, "Craftsman": {"Id": 2020}, "RecordingDate": "2026-11-01",
            }, sync_signature="reporter-sync")
        self.db.session.commit()
        self.db.session.expire_all()
        self.assertEqual(order.raw_payload["reporter_selection"], selection)
        self.assertEqual(order.reporter, "Erika\nTelefon: 123")

    def test_reporter_live_contact_includes_all_phone_numbers_emails_and_contact_notes_for_one_person(self):
        first = self.contact_tenant(1, "Erika")
        second = self.contact_tenant(2, "Max")
        for identity, value, label, note in ((2, "erika@example.invalid", 5, None), (3, "arbeit@example.invalid", 5, "dienstlich"), (4, "02306 123", 1, "ab 17 Uhr")):
            first.person.communications.append(types.SimpleNamespace(id_=identity, content=value, explanation=note, communication_type=types.SimpleNamespace(id_=label)))
        service = self.agreement_service(self.contact_agreement(4004), [first, second])
        contact = service.get_reporter_contact_item(4004, first.id_, on_date=self.directory.directory_today())
        for value in ("Erika", "Mobil: 0123456789", "E-Mail: erika@example.invalid", "E-Mail: arbeit@example.invalid (dienstlich)", "Telefon: 02306 123 (ab 17 Uhr)"):
            self.assertIn(value, contact["text"])
        self.assertNotIn("Max", contact["text"])
        self.assertEqual(contact["text"].count("0123456789"), 1)

    def test_reporter_live_contact_rejects_expired_future_foreign_or_missing_people(self):
        today = self.directory.directory_today()
        for field, value in (("contractual_use_valid_from", today + timedelta(days=1)), ("contractual_use_valid_to", today - timedelta(days=1)), ("license_agreement_id", 9999), ("person", None)):
            tenant = self.contact_tenant(1, "Erika")
            setattr(tenant, field, value)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.agreement_service(self.contact_agreement(4004), [tenant]).get_reporter_contact_item(4004, tenant.id_, on_date=today)

    def test_reporter_live_contract_and_tenant_dates_include_today(self):
        today = self.directory.directory_today()
        tenant = self.contact_tenant(1, "Erika", end=today.isoformat())
        tenant.contractual_use_valid_from = today.isoformat()
        agreement = self.contact_agreement(4004, start=today.isoformat(), end=today.isoformat())
        contact = self.agreement_service(agreement, [tenant]).get_reporter_contact_item(4004, tenant.id_, on_date=today)
        self.assertEqual(contact["name"], "Erika")

    def test_reporter_live_contact_rejects_vacant_future_expired_or_wrong_contracts(self):
        today = self.directory.directory_today()
        for agreement in (None, self.contact_agreement(9999), self.contact_agreement(4004, vacancy=True), self.contact_agreement(4004, start=(today + timedelta(days=1)).isoformat()), self.contact_agreement(4004, end=(today - timedelta(days=1)).isoformat())):
            service = self.agreement_service(agreement, [self.contact_tenant(1, "Erika")])
            with self.assertRaises(ValueError):
                service.get_reporter_contact_item(4004, 101, on_date=today)
            service.client.get_contractors.assert_not_called()


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
