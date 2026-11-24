import ast
import importlib
import json
import os
import sys
import types
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token
from flask_sqlalchemy import SQLAlchemy
import sqlalchemy as sa
from sqlalchemy.engine import make_url


class CommissionObjectLevelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.db = SQLAlchemy()
        cls.app = Flask(__name__)
        cls.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite://", TESTING=True,
            JWT_SECRET_KEY="object-level-test-secret-at-least-thirty-two-characters",
        )
        cls.db.init_app(cls.app)
        cls.package_name = "_commission_object_level_test_app"
        package = types.ModuleType(cls.package_name)
        package.__path__ = [str(cls.root / "app")]
        extensions = types.ModuleType(cls.package_name + ".extensions")
        extensions.db = cls.db
        extensions.jwt = JWTManager(cls.app)
        modules = {cls.package_name: package, extensions.__name__: extensions}
        sources = ("object_directory", "commission_service", "wowiport", "tenant_notifications", "api")
        real_modules = {"models", "extensions", "settings", "utils", "blacklist", "email_templates", *sources}
        for source_name in sources:
            source = ast.parse((cls.root / "app" / (source_name + ".py")).read_text())
            for node in ast.walk(source):
                if not isinstance(node, ast.ImportFrom) or node.level != 1 or node.module in real_modules:
                    continue
                module_name = cls.package_name + "." + node.module
                module = modules.setdefault(module_name, types.ModuleType(module_name))
                for item in node.names:
                    setattr(module, item.name, Mock())
        modules[cls.package_name + ".completion_notifications"].resolve_completion_notification_choice.return_value = False
        modules[cls.package_name + ".commission_approval"].prepare_commission_approval.return_value = False
        modules[cls.package_name + ".commission_priority"].parse_commission_priority.return_value = False
        module_patch = patch.dict(sys.modules, modules)
        module_patch.start()
        cls.addClassCleanup(module_patch.stop)
        cls.models = importlib.import_module(cls.package_name + ".models")
        cls.settings = importlib.import_module(cls.package_name + ".settings")
        cls.directory = importlib.import_module(cls.package_name + ".object_directory")
        cls.service = importlib.import_module(cls.package_name + ".commission_service")
        cls.gateway = importlib.import_module(cls.package_name + ".wowiport")
        cls.notifications = importlib.import_module(cls.package_name + ".tenant_notifications")
        cls.api = importlib.import_module(cls.package_name + ".api")
        cls.api.serialize_accepted_appointment_from.return_value = None
        cls.app.register_blueprint(cls.api.api_bp)
        cls.tables = [model.__table__ for model in (
            cls.models.AppSetting, cls.models.BlacklistEntry, cls.models.User,
            cls.models.UserJurisdictionPersonMapping, cls.models.WowiResponsibleOfficial,
            cls.models.WowiCompanyCode, cls.models.WowiEconomicUnit,
            cls.models.WowiEconomicUnitJurisdiction, cls.models.WowiBuilding,
            cls.models.WowiUseUnit, cls.models.WowiLicenseAgreement, cls.models.WowiContractor,
            cls.models.WowiDepartment, cls.models.CraftActivity,
            cls.models.CraftActivityDepartmentMapping, cls.models.TimeTaskTemplate,
            cls.models.Commission, cls.models.CommissionPosition, cls.models.CommissionTenantNotification,
            cls.models.ExternalCraftsman,
        )]

    def setUp(self):
        context = self.app.app_context()
        context.push()
        self.addCleanup(context.pop)
        self.addCleanup(self.db.session.remove)
        date_patch = patch.object(self.directory, "directory_today", return_value=date(2026, 11, 13))
        date_patch.start()
        self.addCleanup(date_patch.stop)
        self.db.metadata.drop_all(self.db.engine, tables=self.tables)
        self.db.metadata.create_all(self.db.engine, tables=self.tables)
        self.creator = self.models.User(email="creator@example.invalid", display_name="Sachbearbeitung", person_erp_id=101)
        self.craftsman = self.models.User(
            email="craftsman@example.invalid", display_name="Handwerker", person_erp_id=202, craftsman_erp_id=2020,
        )
        self.company = self.models.WowiCompanyCode(erp_id=77, code="BW", name="Buchwerk")
        self.economic = self.models.WowiEconomicUnit(
            id=1, erp_id=1001, id_num="WE-COMMON", name="COMMON Siedlung", location="Lünen",
            company_code_erp_id=77, company_code_code="BW", universal_responsibility=True,
            universal_responsible_official_erp_id=901, universal_responsible_person_erp_id=202,
        )
        self.empty_economic = self.models.WowiEconomicUnit(
            id=10, erp_id=1010, id_num="WE-EMPTY", name="Freifläche", location="Innenhof",
            company_code_erp_id=77, company_code_code="BW", universal_responsibility=True,
            universal_responsible_official_erp_id=901, universal_responsible_person_erp_id=202,
        )
        self.building = self.models.WowiBuilding(
            id=2, erp_id=2002, id_num="GE-COMMON", address="COMMON Straße 1", economic_unit=self.economic,
        )
        self.empty_building = self.models.WowiBuilding(
            id=5, erp_id=2005, id_num="GE-EMPTY", address="Nebengebäude", economic_unit=self.economic,
        )
        self.unit = self.models.WowiUseUnit(
            id=3, erp_id=3003, id_num="NE-COMMON", address="COMMON Straße 1", floor_name="1. OG",
            building=self.building, economic_unit=self.economic,
        )
        self.agreement = self.models.WowiLicenseAgreement(
            id=4, erp_id=4004, id_num="MV-COMMON", use_unit=self.unit, is_vacancy=False,
        )
        self.tenant = self.models.WowiContractor(
            erp_id=5005, license_agreement_erp_id=4004, license_agreement=self.agreement,
            display_name="Erika COMMON", active=True,
        )
        self.department = self.models.WowiDepartment(erp_id=88, name="Technik")
        self.craft = self.models.CraftActivity(erp_id=500, name="Sanitär", active=True, app_enabled=True)
        self.mapping = self.models.CraftActivityDepartmentMapping(craft_activity=self.craft, department=self.department)
        self.task = self.models.TimeTaskTemplate(
            code="STANDARD", title="Reparatur", active=True, craft_activity=self.craft,
            erp_service_catalogue_id=600, commission_type_erp_ids=[100],
        )
        self.db.session.add_all([
            self.creator, self.craftsman, self.company, self.economic, self.empty_economic,
            self.building, self.empty_building, self.unit, self.agreement, self.tenant,
            self.department, self.craft, self.mapping, self.task,
            self.models.WowiResponsibleOfficial(erp_id=900, person_erp_id=101, active=True),
            self.models.WowiResponsibleOfficial(erp_id=901, person_erp_id=202, active=True),
            self.models.AppSetting(
                key="commissions.tenant_notifications_start_at", value_text="2020-01-01T00:00:00+00:00",
                value_type="string", label="Beginn", group="Test", is_secret=False,
            ),
        ])
        for definition, value in (
            (self.settings.SettingKeys.WOWI_ALLOWED_COMMISSION_TYPE_IDS, [100]),
            (self.settings.SettingKeys.WOWI_DEFAULT_CRAFTS_PROCESS_TYPE_CODE, "Reparatur"),
            (self.settings.SettingKeys.WOWI_INITIAL_CRAFTS_PROCESS_STATUS_CODE, "Offen"),
            (self.settings.SettingKeys.WOWI_INTERNAL_SALES_TAX_CODE, "19"),
        ):
            self.settings.settings.set(definition, value)
        self.db.session.commit()
        self.service.enqueue_erp_operation.reset_mock()
        self.client = self.app.test_client()
        self.headers = {"Authorization": "Bearer " + create_access_token(identity=str(self.creator.id))}

    def create_order(self, object_type, object_id, **extra):
        commission = self.service.create_pending_commission({
            "object_type": object_type, "object_id": object_id,
            "craft_activity_id": self.craft.id, "commission_type_id": 100, "notify_tenants": "true",
            "positions": [{"service_catalog_id": 600, "commission_text": "Bitte reparieren", "units": 1}],
            **extra,
        }, self.creator, self.models.CommissionSource.WEB)
        self.db.session.commit()
        return commission

    def test_economic_search_exposes_the_first_address_without_changing_the_order_scope(self):
        choice = self.directory.get_commission_object_choice("economic_unit", self.economic.id)
        self.assertEqual(choice["economic_unit"]["address"], self.building.address)
        self.assertEqual(choice["object_type"], "economic_unit")
        self.assertIsNone(choice["building"])
        self.assertIsNone(choice["use_unit"])
        empty = self.directory.get_commission_object_choice("economic_unit", self.empty_economic.id)
        self.assertEqual(empty["economic_unit"]["address"], self.empty_economic.location)

    def test_unit_search_keeps_an_occupied_unit_separate_from_its_contract(self):
        choices = self.directory.search_commission_objects("COMMON", "use_unit")
        self.assertEqual([(item["object_type"], item["object_id"]) for item in choices], [("use_unit", self.unit.id), ("license_agreement", self.agreement.id)])
        self.assertIsNone(choices[0]["license_agreement"])
        self.assertIsNone(choices[0]["tenant_name"])
        self.assertFalse(choices[0]["is_vacancy"])

    def test_tenant_and_contract_search_still_return_the_contract(self):
        for context in ("tenant", "agreement"):
            choices = self.directory.search_commission_objects("COMMON", context)
            self.assertEqual([item["object_type"] for item in choices], ["license_agreement"])
            self.assertEqual(choices[0]["tenant_name"], "Erika COMMON")

    def prepare_partial_address_search(self):
        self.building.address = "Wehrenboldstraße 94, 44534 Lünen"
        self.unit.address = self.building.address
        self.tenant.display_name = "Erika Müller"
        other_building = self.models.WowiBuilding(
            erp_id=2006, id_num="GE-OTHER", address="Wehrenboldstraße 95, 44534 Lünen", economic_unit=self.economic,
        )
        other_unit = self.models.WowiUseUnit(
            erp_id=3006, id_num="NE-OTHER", address=other_building.address, building=other_building, economic_unit=self.economic,
        )
        other_agreement = self.models.WowiLicenseAgreement(erp_id=4006, id_num="MV-OTHER", use_unit=other_unit)
        other_agreement.contractors.append(self.models.WowiContractor(
            erp_id=5006, license_agreement_erp_id=4006, display_name="Anderer Mieter",
        ))
        self.db.session.add_all([other_building, other_unit, other_agreement])
        self.db.session.commit()

    def test_partial_streets_and_house_numbers_match_independent_terms(self):
        self.prepare_partial_address_search()
        for text in ("Wehrenb 94", "94 Wehrenb", "wehrenb   94", "Wehrenboldstraße 94"):
            for context, expected in (("address", "building"), ("use_unit", "use_unit"), ("tenant", "license_agreement"), ("all", "license_agreement")):
                with self.subTest(text=text, context=context):
                    choices = self.directory.search_commission_objects(text, context)
                    self.assertTrue(any(item["object_type"] == expected for item in choices))
                    self.assertTrue(all(item["building"]["erp_id"] == 2002 for item in choices))
                    self.assertEqual(len({(item["object_type"], item["object_id"]) for item in choices}), len(choices))

    def test_partial_address_search_requires_all_terms_and_uses_the_building_as_a_unit_fallback(self):
        self.prepare_partial_address_search()
        self.unit.address = None
        self.unit.position_name = "links"
        self.db.session.commit()
        choices = self.directory.search_commission_objects("Wehrenb 94 links", "use_unit")
        self.assertEqual([item["object_id"] for item in choices], [self.unit.id, self.agreement.id])
        for context in ("address", "use_unit", "tenant", "all"):
            self.assertEqual(self.directory.search_commission_objects("Wehrenb 999", context), [])

    def test_tenant_search_combines_name_parts_with_address_parts(self):
        self.prepare_partial_address_search()
        for text in ("Müll Erika", "Erika Wehrenb 94"):
            choices = self.directory.search_commission_objects(text, "tenant")
            self.assertEqual([item["object_id"] for item in choices], [self.agreement.id])

    def test_vacancy_keywords_can_be_combined_with_partial_addresses(self):
        self.prepare_partial_address_search()
        self.agreement.is_vacancy = True
        self.db.session.delete(self.tenant)
        self.db.session.commit()
        choices = self.directory.search_commission_objects("Leerstand Wehrenb 94", "tenant")
        self.assertEqual([(item["object_type"], item["object_id"]) for item in choices], [("license_agreement", self.agreement.id)])
        self.agreement.active = False
        self.db.session.commit()
        choices = self.directory.search_commission_objects("Leer Wehrenb 94", "tenant")
        self.assertEqual([(item["object_type"], item["object_id"]) for item in choices], [("use_unit", self.unit.id)])

    def test_search_treats_percent_and_underscore_as_literal_characters(self):
        for text in ("%%", "__"):
            for context in ("tenant", "agreement", "use_unit", "address", "economic_unit", "all"):
                self.assertEqual(self.directory.search_commission_objects(text, context), [])
        self.building.id_num = "GE_94"
        self.empty_building.id_num = "GEA94"
        self.db.session.commit()
        choices = self.directory.search_commission_objects("GE_94", "address")
        self.assertEqual({item["building"]["erp_id"] for item in choices}, {2002})

    def test_search_combines_economic_unit_name_and_location_terms(self):
        self.economic.name = "Siedlung Altstadt"
        self.db.session.commit()
        choices = self.directory.search_commission_objects("Alt Lün", "economic_unit")
        self.assertEqual([item["object_id"] for item in choices], [self.economic.id, self.building.id, self.agreement.id, self.empty_building.id])

    def test_partial_address_search_is_available_through_the_existing_api(self):
        self.prepare_partial_address_search()
        response = self.client.get("/api/commission-objects/search?q=Wehrenb+94&context=address", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual({item["object_type"] for item in response.json["items"]}, {"building", "license_agreement"})
        self.assertEqual({item["building"]["erp_id"] for item in response.json["items"]}, {2002})

    def test_house_numbers_cannot_match_postcodes_longer_numbers_or_suffixes(self):
        self.building.street = "Tulpenweg 9"
        self.building.address = "Tulpenweg 9, 44539 Lünen"
        self.unit.address = self.building.address
        for index, number in enumerate(("1", "19", "90", "9a", "9 b", "9-11", "9/11"), start=10):
            building = self.models.WowiBuilding(
                erp_id=2000 + index, id_num=f"GE-{index}", street=f"Tulpenweg {number}",
                address=f"Tulpenweg {number}, 44539 Lünen", economic_unit=self.economic,
            )
            unit = self.models.WowiUseUnit(erp_id=3000 + index, id_num=f"NE-{index}", building=building, economic_unit=self.economic)
            agreement = self.models.WowiLicenseAgreement(erp_id=4000 + index, id_num=f"MV-{index}", use_unit=unit)
            agreement.contractors.append(self.models.WowiContractor(
                erp_id=5000 + index, license_agreement_erp_id=agreement.erp_id, display_name=f"Mieter {index}",
            ))
            self.db.session.add_all([building, unit, agreement])
        self.db.session.commit()
        for text, expected in (("Tulpen 9", 2002), ("Tulpenweg 9", 2002), ("9 Tulpen", 2002),
                               ("Tulpen 9a", 2013), ("Tulpen 9 A", 2013), ("Tulpen 9b", 2014),
                               ("Tulpen 9-11", 2015), ("Tulpen 9 / 11", 2016)):
            for context in ("address", "use_unit", "tenant", "all"):
                with self.subTest(text=text, context=context):
                    choices = self.directory.search_commission_objects(text, context)
                    self.assertTrue(choices)
                    self.assertEqual({item["building"]["erp_id"] for item in choices}, {expected})
        choices = self.directory.search_commission_objects("Tulpenweg", "address")
        self.assertGreater(len({item["building"]["erp_id"] for item in choices}), 1)
        for text in ("Tulpenweg 9, 44539 Lünen", "Tulpenweg 9,44539 Lünen"):
            choices = self.directory.search_commission_objects(text, "address")
            self.assertEqual({item["building"]["erp_id"] for item in choices}, {2002})

    def test_object_numbers_with_numeric_suffixes_remain_searchable(self):
        self.building.id_num = "GE-94"
        self.unit.id_num = "NE-94"
        self.agreement.id_num = "MV-94"
        self.db.session.commit()
        for text, context, expected in (("GE-94", "address", "building"), ("NE-94", "use_unit", "use_unit"),
                                        ("MV-94", "agreement", "license_agreement"), ("MV-94", "all", "license_agreement")):
            with self.subTest(text=text, context=context):
                choices = self.directory.search_commission_objects(text, context)
                self.assertTrue(any(item["object_type"] == expected for item in choices))

    def test_house_numbers_are_filtered_before_the_result_limit_and_support_legacy_addresses(self):
        self.prepare_partial_address_search()
        self.building.id_num = "ZZ-LAST"
        self.building.address = "Tulpenweg 9, 44539 Lünen"
        self.unit.address = self.building.address
        other = self.models.WowiBuilding.query.filter_by(erp_id=2006).one()
        other.street = "Tulpenweg 1"
        other.address = "Tulpenweg 1, 44539 Lünen"
        self.db.session.commit()
        choices = self.directory.search_commission_objects("Tulpen 9", "address", limit=1)
        self.assertEqual([(item["object_type"], item["object_id"]) for item in choices], [("building", self.building.id)])
        response = self.client.get("/api/commission-objects/search?q=Tulpenweg+9&context=address", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual({item["building"]["erp_id"] for item in response.json["items"]}, {2002})

    def test_economic_and_unit_hierarchies_keep_current_and_future_contracts_with_their_parents(self):
        future = self.models.WowiLicenseAgreement(erp_id=4005, id_num="MV-FUTURE", use_unit=self.unit, start_date=date(2027, 1, 1))
        ended = self.models.WowiLicenseAgreement(erp_id=4006, id_num="MV-ENDED", use_unit=self.unit, end_date=date(2026, 11, 12))
        inactive = self.models.WowiLicenseAgreement(erp_id=4007, id_num="MV-INACTIVE", use_unit=self.unit, active=False)
        foreign_unit = self.models.WowiUseUnit(erp_id=3008, id_num="NE-FOREIGN", building=self.empty_building, economic_unit=self.economic)
        foreign = self.models.WowiLicenseAgreement(erp_id=4008, id_num="MV-FOREIGN", use_unit=foreign_unit)
        self.db.session.add_all([future, ended, inactive, foreign_unit, foreign])
        self.db.session.commit()
        unit_choices = self.directory.search_commission_objects("NE-COMMON", "use_unit")
        self.assertEqual([(item["object_type"], item["object_id"]) for item in unit_choices], [
            ("use_unit", self.unit.id), ("license_agreement", self.agreement.id), ("license_agreement", future.id),
        ])
        economic_choices = self.directory.search_commission_objects("WE-COMMON", "economic_unit")
        self.assertEqual([(item["object_type"], item["object_id"]) for item in economic_choices], [
            ("economic_unit", self.economic.id), ("building", self.building.id),
            ("license_agreement", self.agreement.id), ("license_agreement", future.id),
            ("building", self.empty_building.id), ("license_agreement", foreign.id),
        ])
        self.assertTrue(next(item for item in economic_choices if item["object_id"] == future.id)["is_future"])
        for context, text, limit in (("economic_unit", "WE-COMMON", 3), ("use_unit", "NE-COMMON", 2)):
            choices = self.directory.search_commission_objects(text, context, limit=limit)
            self.assertEqual(len(choices), limit)
            self.assertEqual(choices[0]["object_type"], context)

    def test_economic_hierarchy_excludes_inactive_buildings_and_units(self):
        self.building.active = False
        self.db.session.commit()
        choices = self.directory.search_commission_objects("WE-COMMON", "economic_unit")
        self.assertEqual([item["object_type"] for item in choices], ["economic_unit", "building"])
        self.assertEqual(choices[1]["object_id"], self.empty_building.id)
        self.building.active = True
        self.unit.active = False
        self.db.session.commit()
        choices = self.directory.search_commission_objects("WE-COMMON", "economic_unit")
        self.assertFalse(any(item["object_type"] == "license_agreement" for item in choices))

    def test_house_number_predicates_match_on_the_configured_database(self):
        database_url = os.environ.get("DATLAEUFT_TEST_DATABASE_URL", "sqlite://")
        url = make_url(database_url)
        if url.get_backend_name() != "sqlite" and not (url.database or "").startswith("datlaeuft_test"):
            self.fail("Diese Prüfung benötigt eine separate datlaeuft_test-Datenbank.")
        engine = sa.create_engine(database_url)
        metadata = sa.MetaData()
        table = sa.Table("wowi_building", metadata, sa.Column("id", sa.Integer(), primary_key=True),
                         sa.Column("street", sa.String(255)), sa.Column("address", sa.String(500)))
        try:
            with engine.begin() as connection:
                metadata.create_all(connection)
                connection.execute(table.insert(), [
                    {"id": 1, "street": "Tulpenweg 9", "address": "Tulpenweg 9, 44539 Lünen"},
                    {"id": 2, "street": "Tulpenweg 19", "address": "Tulpenweg 19, 44539 Lünen"},
                    {"id": 3, "street": "Tulpenweg 90", "address": "Tulpenweg 90, 44539 Lünen"},
                    {"id": 4, "street": "Tulpenweg 9 A", "address": "Tulpenweg 9 A, 44539 Lünen"},
                    {"id": 5, "street": None, "address": "Tulpenweg 9, 44539 Lünen"},
                    {"id": 6, "street": "Tulpenweg 1", "address": "Tulpenweg 1, 44539 Lünen"},
                    {"id": 7, "street": "Tulpenweg 9–11", "address": "Tulpenweg 9–11, 44539 Lünen"},
                ])
                for number, expected in (("9", [1, 5]), ("9a", [4]), ("9-11", [7])):
                    statement = sa.select(self.models.WowiBuilding.id).where(self.directory._building_house_number_filter(number)).order_by(self.models.WowiBuilding.id)
                    self.assertEqual(list(connection.scalars(statement)), expected)
                metadata.drop_all(connection)
        finally:
            engine.dispose()

    def test_building_search_returns_the_building_and_its_contracts_as_separate_choices(self):
        choices = self.directory.search_commission_objects("COMMON", "address")
        self.assertEqual([item["object_type"] for item in choices], ["building", "license_agreement"])
        self.assertIsNone(choices[0]["use_unit"])
        self.assertIsNone(choices[0]["license_agreement"])
        self.assertEqual(choices[1]["license_agreement"]["erp_id"], 4004)
        selected = self.create_order(choices[1]["object_type"], choices[1]["object_id"])
        self.assertEqual(selected.license_agreement_id, 4004)
        self.assertEqual(selected.object_type, "license_agreement")
        empty = self.directory.search_commission_objects("GE-EMPTY", "address")
        self.assertEqual(empty[0]["object_id"], self.empty_building.id)

    def test_future_contracts_show_the_tenant_and_start_date_and_can_be_commissioned(self):
        self.agreement.start_date = date(2027, 1, 1)
        self.tenant.contractual_use_from = date(2027, 1, 1)
        self.db.session.commit()
        for context, text in (("tenant", "Erika"), ("agreement", "MV-COMMON"), ("address", "GE-COMMON")):
            choices = self.directory.search_commission_objects(text, context)
            contract = next(item for item in choices if item["object_type"] == "license_agreement")
            self.assertEqual(contract["tenant_name"], "Erika COMMON")
            self.assertTrue(contract["is_future"])
            self.assertEqual(contract["license_agreement"]["start_date"], "2027-01-01")
        commission = self.create_order("license_agreement", self.agreement.id)
        self.assertEqual(commission.license_agreement_id, 4004)
        self.assertEqual(commission.tenant_name, "Erika COMMON")
        gateway = self.gateway_with_mocked_client()
        gateway.create_commission_waiting_for_acceptance(commission)
        self.assertEqual(gateway.client.create_commissioning_commission_wait_for_craftsman_feedback.call_args.kwargs["license_agreement_id"], 4004)

    def test_current_and_future_contracts_in_one_apartment_remain_distinct(self):
        future = self.models.WowiLicenseAgreement(
            erp_id=4005, id_num="MV-FUTURE", use_unit=self.unit, start_date=date(2027, 1, 1),
        )
        future.contractors.append(self.models.WowiContractor(
            erp_id=5006, license_agreement_erp_id=4005, display_name="Künftiger Mieter", contractual_use_from=date(2027, 1, 1),
        ))
        self.db.session.add(future)
        self.db.session.commit()
        choices = self.directory.search_commission_objects("GE-COMMON", "address")
        contracts = [item for item in choices if item["object_type"] == "license_agreement"]
        self.assertEqual({item["license_agreement"]["erp_id"] for item in contracts}, {4004, 4005})
        for contract in contracts:
            commission = self.create_order(contract["object_type"], contract["object_id"])
            self.assertEqual(commission.license_agreement_id, contract["license_agreement"]["erp_id"])
            self.assertEqual(commission.tenant_name, contract["tenant_name"])
        self.assertFalse(self.directory._serialize_choice("use_unit", self.unit.id)["is_vacancy"])

    def test_a_future_tenancy_does_not_hide_the_current_vacancy(self):
        self.agreement.start_date = date(2027, 1, 1)
        self.db.session.commit()
        choices = self.directory.search_commission_objects("COMMON", "tenant")
        vacancy = next(item for item in choices if item["object_type"] == "use_unit")
        self.assertTrue(vacancy["is_vacancy"])
        self.assertIn("Leerstand", vacancy["label"])
        self.assertIsNone(vacancy["license_agreement"])
        commission = self.create_order(vacancy["object_type"], vacancy["object_id"])
        self.assertEqual(commission.use_unit_id, 3003)
        self.assertIsNone(commission.license_agreement_id)

    def test_vacancy_contracts_are_searchable_without_a_tenant_and_disable_tenant_notifications(self):
        self.agreement.is_vacancy = True
        self.db.session.delete(self.tenant)
        self.db.session.commit()
        for context, text in (("tenant", "COMMON"), ("tenant", "Leerstand"), ("agreement", "MV-COMMON"), ("address", "GE-COMMON")):
            choices = self.directory.search_commission_objects(text, context)
            contract = next(item for item in choices if item["object_type"] == "license_agreement")
            self.assertEqual(contract["label"], "Leerstand")
            self.assertTrue(contract["is_vacancy"])
            self.assertIsNone(contract["tenant_name"])
            self.assertFalse(any(item["object_type"] == "use_unit" for item in choices))
        commission = self.create_order("license_agreement", self.agreement.id)
        self.assertEqual(commission.license_agreement_id, 4004)
        self.assertIsNone(commission.tenant_name)
        self.assertFalse(commission.notify_tenants)

    def test_vacancy_without_a_contract_is_selectable_in_tenant_and_building_search(self):
        self.agreement.active = False
        self.db.session.commit()
        for context, text in (("tenant", "COMMON"), ("tenant", "Leerstand"), ("address", "GE-COMMON")):
            choices = self.directory.search_commission_objects(text, context)
            vacancy = next(item for item in choices if item["object_type"] == "use_unit")
            self.assertTrue(vacancy["is_vacancy"])
            self.assertIn("Leerstand", vacancy["label"])
        commission = self.create_order(vacancy["object_type"], vacancy["object_id"])
        self.assertEqual(commission.use_unit_id, 3003)
        self.assertIsNone(commission.license_agreement_id)
        self.assertFalse(commission.notify_tenants)

    def test_ended_contracts_are_excluded_even_if_the_mirror_has_not_synced_today(self):
        self.agreement.end_date = date(2026, 11, 12)
        self.db.session.commit()
        for context in ("tenant", "agreement", "address", "all"):
            choices = self.directory.search_commission_objects("COMMON", context)
            self.assertFalse(any(item["object_type"] == "license_agreement" for item in choices))
        for values in ({"object_type": "license_agreement", "object_id": self.agreement.id}, {"license_agreement_idnum": "MV-COMMON"}):
            with self.assertRaisesRegex(ValueError, "nicht mehr verfügbar"):
                self.directory.resolve_commission_object(values)

    def test_contract_dates_are_inclusive_and_building_choices_survive_a_small_limit(self):
        self.agreement.start_date = date(2026, 11, 13)
        self.agreement.end_date = date(2026, 11, 13)
        self.db.session.commit()
        choices = self.directory.search_commission_objects("COMMON", "address", limit=2)
        self.assertEqual([item["object_type"] for item in choices], ["building", "license_agreement"])
        self.assertFalse(choices[1]["is_future"])
        self.assertEqual(choices[1]["license_agreement"]["end_date"], "2026-11-13")

    def test_object_sync_includes_future_contracts_and_vacancies_but_excludes_ended_tenancies(self):
        service = Mock()
        rows = {
            "CommercialInventory/EconomicUnits": [{"Id": 1001, "IdNum": "WE-COMMON", "CompanyCode": {"Id": 77, "Code": "BW"}}],
            "CommercialInventory/BuildingLands": [{"Id": 2002, "IdNum": "GE-COMMON", "EconomicUnit": {"Id": 1001}, "EstateAddress": {"StreetComplete": "COMMON Straße 1"}}],
            "CommercialInventory/UseUnits": [{"Id": 3003, "IdNum": "NE-COMMON", "BuildingLand": {"Id": 2002}}],
            "RentAccounting/LicenseAgreements": [
                {"Id": 4004, "IdNum": "MV-COMMON", "UseUnit": {"Id": 3003}, "EndOfContract": "2026-11-12"},
                {"Id": 4005, "IdNum": "MV-FUTURE", "UseUnit": {"Id": 3003}, "StartContract": "2027-01-01"},
                {"Id": 4006, "IdNum": "MV-VACANT", "UseUnit": {"Id": 3003}, "EndOfContract": "2026-12-31", "RestrictionOfUse": {"IsVacancy": True}},
            ],
            "RentAccountingPersonDetails/Contractors": [
                {"Id": 5005, "LicenseAgreementId": 4004, "Name": "Erika COMMON", "ContractualUseValidTo": "2026-11-12"},
                {"Id": 5006, "LicenseAgreementId": 4005, "Name": "Künftiger Mieter", "ContractualUseValidFrom": "2027-01-01"},
                {"Id": 5007, "LicenseAgreementId": 4005, "Name": "Früherer Beteiligter", "ContractualUseValidTo": "2026-11-12"},
            ],
            "CommercialInventory/ResponsibleOfficial": [{"Id": 900, "PersonId": 101}],
        }
        service.read_all_rows.side_effect = lambda endpoint, parameters: rows.get(endpoint, [])
        with patch.object(self.directory, "WowiportService", return_value=service):
            result = self.directory.sync_object_directory()
        self.db.session.commit()
        self.assertEqual(result["license_agreements"], 2)
        self.assertFalse(self.agreement.active)
        self.assertFalse(self.tenant.active)
        choices = self.directory.search_commission_objects("Künftiger", "tenant")
        self.assertEqual(len(choices), 1)
        self.assertTrue(choices[0]["is_future"])
        self.assertEqual(choices[0]["license_agreement"]["erp_id"], 4005)
        self.assertFalse(self.models.WowiContractor.query.filter_by(erp_id=5007).count())
        for call in service.read_all_rows.call_args_list:
            if call.args[0] in {"RentAccounting/LicenseAgreements", "RentAccountingPersonDetails/Contractors"}:
                self.assertNotIn("licenseAgreementActiveOn", call.args[1])
                self.assertNotIn("contractualUseActiveOn", call.args[1])

    def test_economic_unit_search_accepts_units_without_buildings(self):
        for text in ("WE-EMPTY", "Freifläche", "Innenhof"):
            choices = self.directory.search_commission_objects(text, "economic_unit")
            self.assertEqual([item["object_type"] for item in choices], ["economic_unit"])
            self.assertIsNone(choices[0]["building"])
            self.assertIsNone(choices[0]["use_unit"])
            self.assertIsNone(choices[0]["license_agreement"])
            self.assertEqual(choices[0]["economic_unit"]["erp_id"], 1010)

    def test_all_search_keeps_all_four_levels_visible_under_the_result_limit(self):
        choices = self.directory.search_commission_objects("COMMON", "all", limit=4)
        self.assertEqual({item["object_type"] for item in choices}, {
            "license_agreement", "use_unit", "building", "economic_unit",
        })
        self.assertEqual(len({(item["object_type"], item["object_id"]) for item in choices}), 4)

    def test_economic_unit_resolution_keeps_lower_levels_empty(self):
        context = self.directory.resolve_commission_object({"object_type": "economic_unit", "object_id": 10})
        self.assertEqual(context.economic_unit.erp_id, 1010)
        self.assertIsNone(context.building)
        self.assertIsNone(context.use_unit)
        self.assertIsNone(context.license_agreement)

    def test_inactive_objects_and_missing_company_codes_are_rejected(self):
        self.empty_economic.active = False
        self.db.session.commit()
        self.assertEqual(self.directory.search_commission_objects("WE-EMPTY", "economic_unit"), [])
        with self.assertRaises(ValueError):
            self.directory.resolve_commission_object({"object_type": "economic_unit", "object_id": 10})
        self.empty_economic.active = True
        self.empty_economic.company_code_erp_id = None
        self.db.session.commit()
        with self.assertRaisesRegex(ValueError, "Buchwerk"):
            self.directory.resolve_commission_object({"object_type": "economic_unit", "object_id": 10})

    def test_a_conflicting_company_code_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Buchwerk"):
            self.directory.resolve_commission_object({
                "object_type": "economic_unit", "object_id": 10, "company_code_code": "FALSCH",
            })

    def test_created_orders_preserve_the_selected_level_and_automatic_assignment(self):
        for object_type, object_id, expected in (
            ("license_agreement", 4, (1001, 2002, 3003, 4004)),
            ("use_unit", 3, (1001, 2002, 3003, None)),
            ("building", 2, (1001, 2002, None, None)),
            ("economic_unit", 1, (1001, 2002, None, None)),
        ):
            with self.subTest(object_type=object_type):
                commission = self.create_order(object_type, object_id)
                self.assertEqual((
                    commission.economic_unit_erp_id, commission.building_erp_id,
                    commission.use_unit_id, commission.license_agreement_id,
                ), expected)
                self.assertEqual(commission.user_id, self.craftsman.id)
                self.assertEqual(commission.company_code_erp_id, 77)
                self.assertEqual(commission.object_type, object_type)
                self.assertEqual(commission.notify_tenants, object_type == "license_agreement")
                self.assertEqual(self.service.enqueue_erp_operation.call_args.kwargs["operation_type"].value, "create_commission")

    def gateway_with_mocked_client(self):
        gateway = self.gateway.WowiportService.__new__(self.gateway.WowiportService)
        gateway.client = Mock()
        gateway._commission_company_code_id = Mock(return_value=77)
        gateway._resolve_sales_tax_id = Mock(return_value=19)
        gateway._resolve_creditor_id = Mock(return_value=123)
        gateway._resolve_craft_process_type_id = Mock(return_value=8)
        gateway._resolve_craft_process_status_id = Mock(return_value=9)
        gateway._find_existing_commission = Mock(return_value=None)
        gateway.client.create_commissioning_crafts_process.return_value = types.SimpleNamespace(data={"Id": 9100, "IdNum": "V-1"})
        gateway.client.create_commissioning_commission_wait_for_craftsman_feedback.return_value = types.SimpleNamespace(
            data={"Id": 9200, "IdNum": "A-1"}, status_code=200,
        )
        gateway.client.get_license_agreements.return_value = [types.SimpleNamespace(
            id_=4004, id_num="MV-COMMON", use_unit=types.SimpleNamespace(id_=3003, id_num="NE-COMMON"),
        )]
        gateway.client.get_use_units.return_value = [types.SimpleNamespace(
            id_=3003, id_num="NE-COMMON", building_land=types.SimpleNamespace(id_=2002, id_num="GE-COMMON"),
        )]
        gateway.client.get_building_lands.return_value = [types.SimpleNamespace(id_=2002, id_num="GE-COMMON")]
        return gateway

    def test_long_position_text_keeps_its_full_contents_during_creation_and_erp_sync(self):
        text = "Trocknung kontrollieren und sämtliche Messwerte dokumentieren.\r\n" * 7
        commission = self.create_order("license_agreement", self.agreement.id, positions=[{
            "service_catalog_id": 600, "commission_text": text, "units": 1,
        }])
        self.assertEqual(len(commission.title), 255)
        self.assertTrue(commission.title.endswith("…"))
        self.assertEqual(commission.description, text.strip())
        self.assertEqual(commission.positions[0].commission_text, text.strip())
        gateway = self.gateway_with_mocked_client()
        gateway.create_commission_waiting_for_acceptance(commission)
        self.assertEqual(gateway.client.create_commissioning_crafts_process.call_args.kwargs["description"], text.strip())
        for items in ([{"Id": 12, "CommissionText": text, "Units": 1}], []):
            with self.subTest(has_positions=bool(items)):
                synced = self.gateway.upsert_commission({"Id": 9200, "IdNum": "A-1", "Description": text, "CommissionItems": items})
                self.db.session.commit()
                self.assertEqual(len(synced.title), 255)
                self.assertTrue(synced.title.endswith("…"))
                self.assertEqual(synced.description.strip(), text.strip())

    def test_order_titles_respect_the_database_limit_for_initial_values_and_updates(self):
        for title in (None, "", "Kurzer Titel", "ä" * 255):
            with self.subTest(title_length=len(title or "")):
                commission = self.models.Commission(title=title)
                self.assertEqual(commission.title, title)
        commission = self.models.Commission(title="ä" * 291)
        self.assertEqual(commission.title, "ä" * 254 + "…")
        commission.title = "Neuer Titel " * 40
        self.assertEqual(len(commission.title), 255)
        self.assertTrue(commission.title.endswith("…"))

    def test_long_titles_can_be_written_to_the_configured_database_without_losing_the_description(self):
        database_url = os.environ.get("DATLAEUFT_TEST_DATABASE_URL", "sqlite://")
        url = make_url(database_url)
        if url.get_backend_name() != "sqlite" and not (url.database or "").startswith("datlaeuft_test"):
            self.fail("Diese Prüfung benötigt eine separate datlaeuft_test-Datenbank.")
        engine = sa.create_engine(database_url)
        metadata = sa.MetaData()
        constraints = [sa.CheckConstraint("length(title) <= 255")] if url.get_backend_name() == "sqlite" else []
        table = sa.Table("commission_title_regression", metadata, sa.Column("id", sa.Integer(), primary_key=True),
                         sa.Column("title", self.models.Commission.__table__.c.title.type), sa.Column("description", sa.Text()), *constraints)
        text = "Ausführlicher Auftragstext mit vollständigen Messwerten.\r\n" * 7
        commission = self.models.Commission(title=text, description=text)
        try:
            with engine.begin() as connection:
                metadata.create_all(connection)
                connection.execute(table.insert().values(id=1, title=commission.title, description=commission.description))
                commission.title = text + "Erneute Kontrolle."
                connection.execute(table.update().where(table.c.id == 1).values(title=commission.title))
                stored = connection.execute(sa.select(table.c.title, table.c.description)).one()
                self.assertLessEqual(len(stored.title), 255)
                self.assertTrue(stored.title.endswith("…"))
                self.assertEqual(stored.description, text)
        finally:
            with engine.begin() as connection:
                metadata.drop_all(connection)
            engine.dispose()

    def test_new_process_keeps_the_exact_original_description_after_an_order_update(self):
        commission = self.create_order("license_agreement", self.agreement.id)
        gateway = self.gateway_with_mocked_client()
        original = commission.description
        gateway.create_commission_waiting_for_acceptance(commission)
        self.assertEqual(gateway.client.create_commissioning_crafts_process.call_args.kwargs["description"], original)
        self.assertEqual(commission.raw_payload["initial_process_description"], {"process_erp_id": 9100, "body": original})
        self.assertEqual(commission.raw_payload["commission_scope"]["object_type"], "license_agreement")
        commission.description = "Später geänderter Auftragstext."
        self.db.session.commit()
        self.assertEqual(commission.raw_payload["initial_process_description"]["body"], original)

    def test_real_erp_json_sends_the_creator_official_id_to_the_process_and_order(self):
        from test_wowiport_client import transport

        commission = self.create_order("license_agreement", self.agreement.id)
        gateway = self.gateway_with_mocked_client()
        lookups = gateway.client
        with patch("wowipy.rest_adapter.requests_cache.install_cache"):
            client = transport.WowiportClient(hostname="", user="test", password="test", api_key="test-key")
        client._rest_adapter.access_token = "test-token"
        client.get_license_agreements = lookups.get_license_agreements
        client.get_use_units = lookups.get_use_units
        client.get_building_lands = lookups.get_building_lands
        replies = []
        for erp_id, number in ((9100, "V-1"), (9200, "A-1")):
            reply = Mock(status_code=200, reason="OK")
            reply.json.return_value = {"Id": erp_id, "IdNum": number}
            replies.append(reply)
        client._rest_adapter._request = Mock(side_effect=replies)
        gateway.client = client
        gateway.create_commission_waiting_for_acceptance(commission)
        requests = client._rest_adapter._request.call_args_list
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0].kwargs["json"]["PersonInChargeResponsibleOfficialId"], 900)
        self.assertEqual(requests[1].kwargs["json"]["ResponsibleOfficialRepairId"], 900)
        self.assertNotEqual(commission.created_by.person_erp_id, 900)

    def test_commission_readback_reports_missing_or_changed_official_for_both_creation_endpoints(self):
        for endpoint in ("CommissioningEdit/Commission", "CommissioningEdit/Commission/WaitForCraftsmanFeedback"):
            for stored_official_id in (None, 901, 900):
                with self.subTest(endpoint=endpoint, stored_official_id=stored_official_id):
                    client = Mock()
                    adapter = client._rest_adapter
                    result = types.SimpleNamespace(data={"Id": 9200, "IdNum": "A-1"})
                    adapter.post.return_value = result
                    adapter.get.return_value = types.SimpleNamespace(data=[{
                        "Id": 9200, "IdNum": "A-1", "ResponsibleOfficialRepair": (
                            {"Id": stored_official_id} if stored_official_id is not None else None
                        ),
                    }])
                    with patch.object(self.gateway, "WowiportClient", return_value=client):
                        self.gateway.WowiportService()
                    with self.assertLogs(self.app.logger, level="INFO") as logs:
                        self.assertIs(adapter.post(endpoint, data={"ResponsibleOfficialRepairId": 900}), result)
                    warnings = [record.getMessage() for record in logs.records if record.levelname == "WARNING"]
                    self.assertEqual(len(warnings), int(stored_official_id != 900))
                    if warnings:
                        self.assertIn("wowiport_commission_official_mismatch", warnings[0])
                        self.assertIn("erp_id=9200 erp_idnum='A-1'", warnings[0])
                        self.assertIn("api_version='v1.2'", warnings[0])
                        self.assertIn("sent_official_id=900", warnings[0])
                        self.assertIn(f"stored_official_id={stored_official_id}", warnings[0])
                        field_state = "null" if stored_official_id is None else "value"
                        self.assertIn(f"readback_field_state={field_state}", warnings[0])
                    adapter.get.assert_called_once_with(
                        endpoint="CommissioningRead/Commissions",
                        ep_params={"id": 9200, "includeResponsibleOfficialRepair": "true", "showNullValues": "true", "limit": 1},
                        force_refresh=True,
                    )

    def test_commission_readback_does_not_validate_a_different_order(self):
        client = Mock()
        adapter = client._rest_adapter
        result = types.SimpleNamespace(data={"Id": 9200, "IdNum": "A-1"})
        adapter.post.return_value = result
        adapter.get.return_value = types.SimpleNamespace(data=[{
            "Id": 9300, "IdNum": "A-OTHER", "ResponsibleOfficialRepair": {"Id": 900},
        }])
        with patch.object(self.gateway, "WowiportClient", return_value=client):
            self.gateway.WowiportService()
        with self.assertLogs(self.app.logger, level="WARNING") as logs:
            self.assertIs(adapter.post("CommissioningEdit/Commission", data={"ResponsibleOfficialRepairId": 900}), result)
        self.assertIn("found=False", logs.output[0])
        self.assertIn("readback_field_state=order_not_found", logs.output[0])
        self.assertIn("erp_id=9200 erp_idnum='A-1'", logs.output[0])
        self.assertNotIn("A-OTHER", logs.output[0])

    def test_commission_readback_marks_an_omitted_official_field(self):
        client = Mock()
        adapter = client._rest_adapter
        adapter.post.return_value = types.SimpleNamespace(data={"Id": 9200, "IdNum": "A-1"})
        adapter.get.return_value = types.SimpleNamespace(data=[{"Id": 9200, "IdNum": "A-1"}])
        with patch.object(self.gateway, "WowiportClient", return_value=client):
            self.gateway.WowiportService()
        with self.assertLogs(self.app.logger, level="WARNING") as logs:
            adapter.post("CommissioningEdit/Commission/WaitForCraftsmanFeedback", data={"ResponsibleOfficialRepairId": 900})
        self.assertIn("found=True", logs.output[0])
        self.assertIn("readback_field_state=field_not_returned", logs.output[0])

    def test_failed_readback_preserves_the_created_order_without_repeating_the_write(self):
        client = Mock()
        adapter = client._rest_adapter
        result = types.SimpleNamespace(data={"Id": 9200, "IdNum": "A-1"})
        post = adapter.post
        post.return_value = result
        adapter.get.side_effect = RuntimeError("Rücklesen fehlgeschlagen")
        with patch.object(self.gateway, "WowiportClient", return_value=client):
            self.gateway.WowiportService()
        with self.assertLogs(self.app.logger, level="WARNING") as logs:
            self.assertIs(adapter.post("CommissioningEdit/Commission", data={"ResponsibleOfficialRepairId": 900}), result)
        post.assert_called_once()
        self.assertIn("wowiport_commission_readback_failed", logs.output[0])
        self.assertIn("erp_id=9200 erp_idnum='A-1'", logs.output[0])

    def test_contact_resolution_prioritizes_the_explicit_future_contract_over_the_current_tenant(self):
        gateway = self.gateway_with_mocked_client()
        future = types.SimpleNamespace(
            id_=4005, restriction_of_use=types.SimpleNamespace(is_vacancy=False),
            contractors=[types.SimpleNamespace(person=types.SimpleNamespace(id_=10, name="Künftiger Mieter", communications=[]))],
        )
        current = types.SimpleNamespace(id_=4004, contractors=[])
        gateway.client.get_license_agreements.side_effect = lambda **values: [future] if values.get("add_args") == {"licenseAgreementId": 4005} else [current]
        contacts = gateway.get_commission_contact_items(9200, types.SimpleNamespace(
            license_agreement={"id": 4005}, use_unit_id=3003,
        ))
        self.assertEqual([item["name"] for item in contacts], ["Künftiger Mieter"])
        gateway.client.get_license_agreements.assert_called_once_with(
            add_args={"licenseAgreementId": 4005}, add_contractors=True, limit=1,
        )

    def test_contact_resolution_uses_scalar_references_in_the_order_context(self):
        gateway = self.gateway_with_mocked_client()
        gateway.client.get_license_agreements.return_value = [
            types.SimpleNamespace(id_=4005, contractors=[self.contact_tenant(50)]),
        ]
        commission = self.gateway.CommissioningCommission(
            id_=9200, use_unit_id=3003, license_agreement_id=4005,
        )
        with patch.object(gateway, "get_use_unit", return_value=self.contact_unit()) as get_use_unit:
            context = gateway.get_commission_context(9200, commission)
        self.assertEqual(context["license_agreement_id"], 4005)
        self.assertEqual(context["use_unit_id"], 3003)
        self.assertEqual(context["tenant_name"], "Mieter")
        get_use_unit.assert_called_once_with(3003)
        gateway.client.get_license_agreements.assert_called_once_with(
            add_args={"licenseAgreementId": 4005}, add_contractors=True, limit=1,
        )

    def test_contact_resolution_loads_contractors_for_a_contract_found_by_use_unit(self):
        gateway = self.gateway_with_mocked_client()
        gateway.client.get_license_agreements.return_value = [types.SimpleNamespace(id_=4004, contractors=[])]
        gateway.client.get_contractors.return_value = [self.contact_tenant(50)]
        contacts = gateway.get_commission_contact_items(9200, {"UseUnit": {"Id": 3003}})
        self.assertEqual([item["license_agreement_id"] for item in contacts], [4004])
        gateway.client.get_contractors.assert_called_once_with(license_agreement_id=4004)

    def test_order_without_contact_references_does_not_search_or_warn(self):
        gateway = self.gateway_with_mocked_client()
        with self.assertNoLogs(self.app.logger, level="WARNING"):
            self.assertEqual(gateway.get_commission_contact_items(9200, {"Id": 9200, "BuildingId": 2002}), [])
        gateway.client.get_license_agreements.assert_not_called()
        gateway.client.get_contractors.assert_not_called()

    def test_order_with_an_explicit_contract_preserves_the_missing_contacts_warning(self):
        gateway = self.gateway_with_mocked_client()
        gateway.client.get_license_agreements.return_value = []
        gateway.client.get_contractors.return_value = []
        with self.assertLogs(self.app.logger, level="WARNING") as logs:
            contacts = gateway.get_commission_contact_items(
                9200, types.SimpleNamespace(license_agreement_id=4005, license_agreement=None, use_unit_id=3003),
            )
        self.assertEqual(contacts, [])
        self.assertIn("no contractors found for commission id 9200, license agreement id 4005", logs.output[0])
        gateway.client.get_license_agreements.assert_called_once_with(
            add_args={"licenseAgreementId": 4005}, add_contractors=True, limit=1,
        )

    def test_a_selected_vacancy_or_missing_contract_never_falls_back_to_another_tenant(self):
        gateway = self.gateway_with_mocked_client()
        vacancy = types.SimpleNamespace(restriction_of_use=types.SimpleNamespace(is_vacancy=True))
        gateway.client.get_license_agreements.return_value = [vacancy]
        self.assertEqual(gateway.get_commission_contact_items(9200, types.SimpleNamespace(
            license_agreement={"id": 4006}, use_unit_id=3003,
        )), [])
        gateway.client.get_contractors.assert_not_called()
        gateway.client.get_license_agreements.reset_mock()
        gateway.client.get_license_agreements.return_value = []
        self.assertIsNone(gateway._get_license_agreement(license_agreement_id=4006, use_unit_id=3003))
        gateway.client.get_license_agreements.assert_called_once_with(
            add_args={"licenseAgreementId": 4006}, add_contractors=True, limit=1,
        )

    def test_erp_process_and_order_receive_the_selected_reference_ids(self):
        for object_type, object_id, expected in (
            ("license_agreement", 4, (1001, 2002, 3003, 4004)),
            ("use_unit", 3, (1001, 2002, 3003, None)),
            ("building", 2, (1001, 2002, None, None)),
            ("economic_unit", 1, (1001, 2002, None, None)),
        ):
            with self.subTest(object_type=object_type):
                commission = self.create_order(object_type, object_id)
                gateway = self.gateway_with_mocked_client()
                result = gateway.create_commission_waiting_for_acceptance(commission)
                self.assertEqual(result.data["Id"], 9200)
                for method in (
                    gateway.client.create_commissioning_crafts_process,
                    gateway.client.create_commissioning_commission_wait_for_craftsman_feedback,
                ):
                    values = method.call_args.kwargs
                    self.assertEqual(tuple(values[key] for key in (
                        "economic_unit_id", "building_id", "use_unit_id", "license_agreement_id",
                    )), expected)
                if object_type == "economic_unit":
                    gateway.client.get_license_agreements.assert_not_called()
                    gateway.client.get_use_units.assert_not_called()
                    gateway.client.get_building_lands.assert_called()

    def test_erp_reference_resolution_still_rejects_an_order_without_any_object(self):
        gateway = self.gateway_with_mocked_client()
        with self.assertRaisesRegex(RuntimeError, "Objektangabe"):
            gateway.resolve_commission_reference_ids(self.models.Commission())

    def test_retrospective_orders_are_created_directly_in_the_final_erp_status(self):
        for assignment_type, status_id, local_status in (("internal", 9, "done"), ("external", 11, "awaiting_invoice")):
            with self.subTest(assignment_type=assignment_type):
                if assignment_type == "external":
                    external = self.models.ExternalCraftsman(craftsman_erp_id=3030, display_name="Externe Firma", email="external@example.invalid")
                    self.db.session.add(external)
                    self.db.session.flush()
                    self.service.resolve_external_craftsman.return_value = external
                values = {"assignment_type": assignment_type, "is_retrospective": "true", "notify_on_completion": True}
                if assignment_type == "external":
                    values["external_craftsman_id"] = external.id
                commission = self.create_order("license_agreement", self.agreement.id, **values)
                self.assertTrue(commission.is_retrospective)
                self.assertEqual(commission.local_status, local_status)
                self.assertFalse(commission.notify_tenants)
                self.assertFalse(commission.notify_on_completion)
                self.assertTrue(commission.completion_successful)
                self.assertIsNotNone(commission.completed_at)
                gateway = self.gateway_with_mocked_client()
                gateway.client.create_commissioning_commission.return_value = types.SimpleNamespace(status_code=200, data={"Id": 9200, "IdNum": "A-1"})
                gateway.create_commission_waiting_for_acceptance(commission)
                gateway.client.create_commissioning_commission_wait_for_craftsman_feedback.assert_not_called()
                values = gateway.client.create_commissioning_commission.call_args.kwargs
                self.assertEqual(values["commission_status_id"], status_id)
                self.assertEqual(values["completion_date"], self.gateway.ensure_utc(commission.completed_at).astimezone(self.gateway.ZoneInfo("Europe/Berlin")).date())

    def test_real_erp_json_for_a_retrospective_order_has_the_final_status_without_a_feedback_request(self):
        from test_wowiport_client import transport

        commission = self.create_order("license_agreement", self.agreement.id, is_retrospective=True)
        gateway = self.gateway_with_mocked_client()
        lookups = gateway.client
        with patch("wowipy.rest_adapter.requests_cache.install_cache"):
            client = transport.WowiportClient(hostname="", user="test", password="test", api_key="test-key")
        client._rest_adapter.access_token = "test-token"
        client.get_license_agreements = lookups.get_license_agreements
        client.get_use_units = lookups.get_use_units
        client.get_building_lands = lookups.get_building_lands
        replies = []
        for erp_id, number in ((9100, "V-1"), (9200, "A-1")):
            reply = Mock(status_code=200, reason="OK")
            reply.json.return_value = {"Id": erp_id, "IdNum": number}
            replies.append(reply)
        client._rest_adapter._request = Mock(side_effect=replies)
        gateway.client = client
        gateway.create_commission_waiting_for_acceptance(commission)
        request = client._rest_adapter._request.call_args_list[-1]
        self.assertNotIn("WaitForCraftsmanFeedback", str(request))
        self.assertEqual(request.kwargs["json"]["CommissionStatusId"], 9)
        self.assertIn("CompletionDate", request.kwargs["json"])

    def test_web_execution_times_use_berlin_time_and_keep_their_precision_after_erp_sync(self):
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo

        commission = self.create_order("license_agreement", self.agreement.id,
            execution_from="2026-11-15T00:30", execution_to="2026-11-15T17:00")
        self.assertEqual(commission.execution_to.hour, 16)
        gateway = self.gateway_with_mocked_client()
        gateway.create_commission_waiting_for_acceptance(commission)
        values = gateway.client.create_commissioning_commission_wait_for_craftsman_feedback.call_args.kwargs
        self.assertEqual(values["execution_from"].isoformat(), "2026-11-15")
        restored = self.gateway.commission_execution_datetime("2026-11-15T00:00:00Z", current=commission.execution_to, end=True)
        self.assertEqual(restored, commission.execution_to)
        imported = self.gateway.commission_execution_datetime("2026-11-15T00:00:00Z", end=True).astimezone(ZoneInfo("Europe/Berlin"))
        self.assertEqual((imported.hour, imported.minute, imported.second), (23, 59, 59))
        summer = self.service.parse_local_datetime("2026-07-01T17:00")
        self.assertEqual(summer, datetime(2026, 7, 1, 15, tzinfo=timezone.utc))

    def test_contractless_orders_do_not_queue_tenant_notifications_even_with_a_legacy_opt_in(self):
        commission = self.create_order("economic_unit", 1)
        commission.notify_tenants = True
        commission.erp_id = 1234
        commission.erp_sync_state = "synced"
        commission.status_id = 12
        self.db.session.commit()
        self.assertEqual(self.notifications.ensure_tenant_notifications(), 0)

    def test_mobile_search_accepts_the_selected_context_and_rejects_unknown_contexts(self):
        for context, object_types in (
            ("tenant", {"license_agreement"}), ("agreement", {"license_agreement"}),
            ("use_unit", {"license_agreement"}), ("address", {"building", "license_agreement"}),
            ("economic_unit", {"economic_unit", "building", "license_agreement"}),
            ("all", {"economic_unit", "building", "license_agreement"}),
        ):
            with self.subTest(context=context):
                response = self.client.get(f"/api/commission-objects/search?q=COMMON&context={context}", headers=self.headers)
                self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                self.assertEqual({item["object_type"] for item in response.get_json()["items"]}, object_types)
        response = self.client.get("/api/commission-objects/search?q=COMMON&context=invalid", headers=self.headers)
        self.assertEqual(response.status_code, 400)

    def test_mobile_search_finds_the_agreement_by_apartment_number(self):
        item, = self.directory.search_mobile_commission_objects("NE-COMMON", limit=1)
        self.assertEqual((item["object_type"], item["object_id"]), ("license_agreement", self.agreement.id))
        self.assertEqual(item["use_unit"]["id_num"], "NE-COMMON")

    def test_mobile_search_does_not_offer_a_vacant_apartment_as_a_target(self):
        self.agreement.active = False
        self.db.session.commit()
        for query in (
            {"q": "NE-COMMON"},
            {"q": "NE-COMMON", "context": "use_unit"},
            {"q": "NE-COMMON", "context": "all", "hierarchy": "1"},
        ):
            with self.subTest(query=query):
                response = self.client.get("/api/commission-objects/search", query_string=query, headers=self.headers)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json["items"], [])
        choices = self.directory.search_commission_objects("NE-COMMON", search_context="use_unit")
        self.assertEqual([(item["object_type"], item["object_id"]) for item in choices], [("use_unit", self.unit.id)])

    def test_mobile_hierarchy_includes_selectable_parents_and_apartment_location(self):
        self.unit.description_of_position = "2. OG links"
        other_agreement = self.models.WowiLicenseAgreement(
            erp_id=4050, id_num="MV-ANDERE", use_unit=self.unit,
        )
        self.db.session.add(self.models.WowiContractor(
            erp_id=5050, license_agreement_erp_id=4050, license_agreement=other_agreement,
            display_name="Anderer Mieter",
        ))
        self.db.session.commit()

        response = self.client.get(
            "/api/commission-objects/search",
            query_string={"q": "Erika", "context": "all", "hierarchy": "1"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        entry, = response.json["items"]
        self.assertEqual(entry["tenant_name"], "Erika COMMON")
        self.assertEqual(entry["object_id"], self.agreement.id)
        self.assertEqual(entry["building"]["object_id"], self.building.id)
        self.assertEqual(entry["economic_unit"]["object_id"], self.economic.id)
        self.assertEqual(entry["use_unit"]["description"], "2. OG links")

        for object_type, building_state in (("building", 2002), ("economic_unit", None)):
            with self.subTest(object_type=object_type):
                context = self.directory.resolve_commission_object({
                    "object_type": object_type, "object_id": entry[object_type]["object_id"],
                })
                self.assertEqual(context.building.erp_id if context.building else None, building_state)
                self.assertIsNone(context.license_agreement)

    def test_mobile_hierarchy_prioritizes_exact_object_numbers(self):
        for text, expected in (("MV-COMMON", "license_agreement"), ("GE-COMMON", "building"), ("WE-COMMON", "economic_unit")):
            with self.subTest(text=text):
                item, = self.directory.search_mobile_commission_objects(text, limit=1)
                self.assertEqual(item["object_type"], expected)

        self.building.street = "Musterstraße 10"
        self.building.address = "Musterstraße 10, 44534 Lünen"
        self.db.session.commit()
        item, = self.directory.search_mobile_commission_objects("Musterstraße 10", limit=1)
        self.assertEqual(item["object_type"], "building")

    def prepare_app_create_options(self):
        engine = self.db.engine
        tables = [self.models.TicketCraftFallback.__table__, self.models.WowiCommissionType.__table__]
        self.db.metadata.create_all(engine, tables=tables)

        def drop_extra_tables():
            self.db.session.rollback()
            self.db.metadata.drop_all(engine, tables=tables)

        self.addCleanup(drop_extra_tables)
        self.db.session.add_all([
            self.models.WowiCommissionType(erp_id=100, code="Standard"),
            self.models.TicketCraftFallback(craft_activity_id=self.craft.id, task_template_id=self.task.id),
        ])
        self.db.session.commit()

    def test_app_creates_and_exports_orders_for_all_allowed_levels(self):
        self.prepare_app_create_options()
        agreement, = self.directory.search_mobile_commission_objects("Erika", limit=30)
        headers = {"Authorization": "Bearer " + create_access_token(identity=str(self.craftsman.id))}

        for object_type, object_id, agreement_id in (
            ("building", agreement["building"]["object_id"], None),
            ("economic_unit", agreement["economic_unit"]["object_id"], None),
            ("license_agreement", agreement["object_id"], self.agreement.erp_id),
        ):
            with self.subTest(object_type=object_type):
                body = {
                    "client_request_id": str(uuid4()), "object_type": object_type,
                    "object_id": object_id,
                    "craft_activity_id": self.craft.id, "commission_type_id": 100,
                    "craftsman_user_id": self.craftsman.id, "title": "Reparatur",
                }
                with patch.object(self.api, "serialize_commission", side_effect=lambda order, **kwargs: {"id": order.id}):
                    response = self.client.post(
                        "/api/commissions/simple", data={"data": json.dumps(body)},
                        content_type="multipart/form-data", headers=headers,
                    )
                self.assertEqual(response.status_code, 202, response.json)
                order = self.db.session.get(self.models.Commission, response.json["item"]["id"])
                self.assertEqual(order.object_type, object_type)
                self.assertEqual(order.license_agreement_id, agreement_id)
                gateway = self.gateway_with_mocked_client()
                gateway.create_commission_waiting_for_acceptance(order)
                exported = gateway.client.create_commissioning_commission_wait_for_craftsman_feedback.call_args.kwargs
                self.assertEqual((exported["economic_unit_id"], exported["building_id"]), (1001, 2002))
                self.assertEqual(exported["license_agreement_id"], agreement_id)

    def test_app_rejects_apartment_targets_in_both_creation_routes(self):
        self.prepare_app_create_options()
        headers = {"Authorization": "Bearer " + create_access_token(identity=str(self.craftsman.id))}
        body = {
            "client_request_id": str(uuid4()), "object_type": "use_unit", "object_id": self.unit.id,
            "craft_activity_id": self.craft.id, "commission_type_id": 100,
            "craftsman_user_id": self.craftsman.id, "title": "Reparatur",
            "positions": [{"service_catalog_id": 600, "commission_text": "Reparatur", "units": 1}],
        }
        legacy_body = dict(body, object_type=None, object_id=None, use_unit_idnum=self.unit.id_num)
        for endpoint, fields in (
            ("/api/commissions/simple", {"data": {"data": json.dumps(body)}, "content_type": "multipart/form-data"}),
            ("/api/commissions", {"json": body}),
            ("/api/commissions", {"json": legacy_body}),
        ):
            with self.subTest(endpoint=endpoint, fields=fields):
                response = self.client.post(endpoint, headers=headers, **fields)
                self.assertEqual(response.status_code, 400, response.json)
                self.assertEqual(response.json["message"], "Bitte eine Wirtschaftseinheit, ein Gebäude oder einen Mietvertrag auswählen.")
        self.assertEqual(self.models.Commission.query.count(), 0)
        self.service.enqueue_erp_operation.assert_not_called()

    def test_mobile_hierarchy_limits_hydration_to_the_returned_hits(self):
        self.db.session.add_all([
            self.models.WowiLicenseAgreement(
                erp_id=6000 + index, id_num=f"MV-COMMON-{index:04d}", use_unit=self.unit,
            )
            for index in range(100)
        ])
        self.db.session.commit()
        statements = []

        def track_queries(connection, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        engine = self.db.engine
        sa.event.listen(engine, "before_cursor_execute", track_queries)
        try:
            results = self.directory.search_mobile_commission_objects("COMMON", limit=30)
        finally:
            sa.event.remove(engine, "before_cursor_execute", track_queries)
        self.assertEqual(len(results), 30)
        self.assertLessEqual(len(statements), 10)

    def contact_service(self, units, agreements, contractors):
        gateway = self.gateway.WowiportService.__new__(self.gateway.WowiportService)
        gateway.client = Mock()
        gateway.client.get_use_units.return_value = units
        gateway.client.get_license_agreements.side_effect = lambda **kwargs: agreements.get(kwargs["add_args"]["useUnitId"], [])
        gateway.client.get_contractors.side_effect = lambda **kwargs: contractors.get(kwargs["license_agreement_id"], [])
        return gateway

    def contact_unit(self, identity=3003, number="NE-1", location="links", building_id=2002):
        return types.SimpleNamespace(
            id_=identity, id_num=number, building_land=types.SimpleNamespace(id_=building_id, id_num="GE-COMMON"),
            floor=types.SimpleNamespace(name="1. OG"), position=types.SimpleNamespace(name=location),
            description_of_position=None, estate_address=None, exit_date=None,
        )

    def contact_agreement(self, identity, unit_id=3003, start="2020-01-01", end=None, vacancy=False):
        return types.SimpleNamespace(
            id_=identity, use_unit=types.SimpleNamespace(id_=unit_id),
            start_contract=start, end_of_contract=end, restriction_of_use=types.SimpleNamespace(is_vacancy=vacancy),
        )

    def contact_tenant(self, person_id, name="Mieter", end=None):
        communication = types.SimpleNamespace(id_=1, content="0123456789", explanation=None, communication_type=types.SimpleNamespace(id_=3))
        return types.SimpleNamespace(
            id_=person_id + 100, contractual_use_valid_to=end, contractor_type=types.SimpleNamespace(name="Mieter"),
            person=types.SimpleNamespace(
                id_=person_id, is_natural_person=False, legal_person=types.SimpleNamespace(long_name1=name),
                communications=[communication],
            ),
        )

    def test_unit_contacts_include_current_and_all_future_contracts_but_not_past_vacancies_or_foreign_units(self):
        commission = self.create_order("use_unit", self.unit.id)
        unit = self.contact_unit()
        today = date(2026, 11, 14)
        agreements = [
            self.contact_agreement(1, end=today),
            self.contact_agreement(2, start=today + timedelta(days=1)),
            self.contact_agreement(3, start=datetime(2035, 1, 1)),
            self.contact_agreement(4, end=today - timedelta(days=1)),
            self.contact_agreement(5, vacancy=True),
            self.contact_agreement(6, unit_id=9999),
            self.contact_agreement(7, start="2030-01-01", end="2020-01-01"),
        ]
        gateway = self.contact_service([unit, self.contact_unit(identity=9999)], {3003: agreements}, {identity: [self.contact_tenant(identity)] for identity in range(1, 8)})

        items = gateway.get_object_contact_items(commission, on_date=today)

        self.assertEqual([item["license_agreement_id"] for item in items], [1, 2, 3])
        self.assertEqual([item["is_future_contract"] for item in items], [False, True, True])
        self.assertEqual(items[0]["use_unit_number"], "NE-1")
        self.assertEqual(items[0]["use_unit_location"], "1. OG · links")
        self.assertEqual(items[0]["mobile"], "0123456789")
        gateway.client.get_license_agreements.assert_called_once_with(add_args={"useUnitId": 3003}, fetch_all=True)
        self.assertEqual([call.kwargs for call in gateway.client.get_contractors.call_args_list], [
            {"license_agreement_id": identity, "fetch_all": True} for identity in [1, 2, 3]
        ])

    def test_building_contacts_keep_units_and_contract_identities_separate(self):
        commission = self.create_order("building", self.building.id)
        units = [self.contact_unit(), self.contact_unit(identity=3004, number="NE-2", location="rechts"), self.contact_unit(identity=9999, building_id=999)]
        gateway = self.contact_service(units, {
            3003: [self.contact_agreement(1), self.contact_agreement(2, start="2030-01-01")],
            3004: [self.contact_agreement(3, unit_id=3004)],
        }, {identity: [self.contact_tenant(50)] for identity in [1, 2, 3]})

        items = gateway.get_object_contact_items(commission, on_date=date(2026, 11, 14))

        self.assertEqual([item["use_unit_id"] for item in items], [3003, 3003, 3004])
        self.assertEqual(len({item["contact_id"] for item in items}), 3)
        self.assertEqual({item["use_unit_location"] for item in items}, {"1. OG · links", "1. OG · rechts"})
        gateway.client.get_use_units.assert_called_once_with(building_land_idnum=None, add_args={"buildingLandId": 2002}, fetch_all=True)
        self.assertEqual(gateway.client.get_license_agreements.call_count, 2)

    def test_former_contractors_and_duplicate_people_are_excluded_without_losing_other_contracts(self):
        commission = self.create_order("use_unit", self.unit.id)
        gateway = self.contact_service([self.contact_unit()], {3003: [self.contact_agreement(1), self.contact_agreement(2, start="2030-01-01")]}, {
            1: [self.contact_tenant(1, end="2026-11-13"), self.contact_tenant(2, end="2026-11-14"), self.contact_tenant(2)],
            2: [self.contact_tenant(2)],
        })

        items = gateway.get_object_contact_items(commission, on_date=date(2026, 11, 14))

        self.assertEqual([(item["person_id"], item["license_agreement_id"]) for item in items], [(2, 1), (2, 2)])

    def test_empty_buildings_and_retired_units_do_not_query_unrelated_contracts(self):
        commission = self.create_order("building", self.building.id)
        retired = self.contact_unit()
        retired.exit_date = date(2026, 11, 13)
        gateway = self.contact_service([retired], {}, {})
        self.assertEqual(gateway.get_object_contact_items(commission, on_date=date(2026, 11, 14)), [])
        gateway.client.get_license_agreements.assert_not_called()
        gateway.client.get_use_units.return_value = []
        self.assertEqual(gateway.get_object_contact_items(commission), [])
        gateway.client.get_license_agreements.assert_not_called()

    def test_mobile_object_contacts_are_available_without_an_erp_commission_number(self):
        for object_type, identity in [("use_unit", self.unit.id), ("building", self.building.id)]:
            commission = self.create_order(object_type, identity)
            expected = [{"contact_id": "3003:1:50", "name": "Mieter", "additional_numbers": [], "is_future_contract": True}]
            service = Mock()
            service.get_object_contact_items.return_value = expected
            with patch.object(self.api, "viewable_commission", return_value=commission), patch.object(self.api, "WowiportService", return_value=service):
                response = self.client.get(f"/api/commissions/{commission.id}/contacts", headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["contact_items"], expected)
            service.get_object_contact_items.assert_called_once_with(commission)
            service.get_commission_contact_items.assert_not_called()

    def test_contract_orders_keep_the_existing_contact_lookup(self):
        commission = self.create_order("license_agreement", self.agreement.id)
        commission.erp_id = 9000
        service = Mock()
        service.get_commission_contact_items.return_value = []
        with patch.object(self.api, "viewable_commission", return_value=commission), patch.object(self.api, "WowiportService", return_value=service):
            response = self.client.get(f"/api/commissions/{commission.id}/contacts", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        service.get_commission_contact_items.assert_called_once_with(9000)
        service.get_object_contact_items.assert_not_called()

    def test_economic_unit_order_has_no_implicit_tenant_lookup(self):
        commission = self.create_order("economic_unit", self.economic.id)
        commission.erp_id = 9000
        with patch.object(self.api, "viewable_commission", return_value=commission), patch.object(self.api, "WowiportService") as service:
            response = self.client.get(f"/api/commissions/{commission.id}/contacts", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["contact_items"], [])
        service.assert_not_called()

    def test_explicit_future_contract_contacts_also_include_the_future_label_and_start_date(self):
        gateway = self.gateway.WowiportService.__new__(self.gateway.WowiportService)
        gateway.client = Mock()
        start = (date.today() + timedelta(days=2)).isoformat()
        agreement = self.contact_agreement(7, start=start)
        agreement.contractors = [self.contact_tenant(50)]
        gateway.client.get_license_agreements.return_value = [agreement]
        commission_object = types.SimpleNamespace(use_unit_id=3003, license_agreement={"id": 7})

        items = gateway.get_commission_contact_items(9000, commission_object=commission_object)

        self.assertEqual(items[0]["license_agreement_id"], 7)
        self.assertEqual(items[0]["contract_start"], start)
        self.assertTrue(items[0]["is_future_contract"])
        gateway.client.get_license_agreements.assert_called_once_with(add_args={"licenseAgreementId": 7}, add_contractors=True, limit=1)

    def test_failed_contact_reads_report_an_error_instead_of_an_empty_success(self):
        commission = self.create_order("building", self.building.id)
        service = Mock()
        service.get_object_contact_items.side_effect = RuntimeError("Connection interrupted")
        with patch.object(self.api, "viewable_commission", return_value=commission), patch.object(self.api, "WowiportService", return_value=service):
            response = self.client.get(f"/api/commissions/{commission.id}/contacts", headers=self.headers)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("contact_items", response.json)

    def test_mobile_search_keeps_tenant_search_as_the_default(self):
        response = self.client.get("/api/commission-objects/search?q=Erika", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual({item["object_type"] for item in response.json["items"]}, {"license_agreement"})

    def test_queued_contractless_notification_is_discarded_before_contacting_wowiport(self):
        commission = self.create_order("economic_unit", 1)
        commission.notify_tenants = True
        commission.status_id = 12
        notification = self.models.CommissionTenantNotification(commission=commission, state="sending")
        self.db.session.add(notification)
        self.db.session.commit()
        with patch.object(self.gateway, "WowiportService") as gateway:
            self.notifications.execute_tenant_notification(notification.id)
        self.assertEqual(notification.state, "discarded")
        gateway.assert_not_called()

    def test_we_orders_use_the_first_building_number_without_requesting_an_address(self):
        self.empty_building.id_num = "GE-0001"
        self.db.session.commit()
        commission = self.create_order("economic_unit", 1)
        self.assertEqual(commission.building_erp_id, 2005)
        self.assertEqual(commission.apartment_address, "Nebengebäude")
        self.assertEqual(commission.economic_unit_erp_id, 1001)
        self.assertIsNone(commission.use_unit_id)
        self.assertIsNone(commission.license_agreement_id)
        self.assertEqual(commission.object_type, "economic_unit")

    def test_the_first_we_address_skips_empty_inactive_and_foreign_buildings(self):
        self.building.address = " \t\n"
        self.empty_building.active = False
        self.db.session.add_all([
            self.models.WowiBuilding(id=6, erp_id=2006, id_num="GE-Z", address="Gartenstraße 1", economic_unit=self.economic),
            self.models.WowiBuilding(id=7, erp_id=2007, id_num="GE-0000", address="Fremde Straße 1", economic_unit=self.empty_economic),
        ])
        self.db.session.commit()
        commission = self.create_order("economic_unit", 1)
        self.assertEqual(commission.building_erp_id, 2006)
        self.assertEqual(commission.apartment_address, "Gartenstraße 1")

    def test_a_we_without_an_active_address_is_rejected_before_queuing(self):
        with self.assertRaisesRegex(ValueError, "keine aktive Gebäudeadresse"):
            self.create_order("economic_unit", 10)
        self.service.enqueue_erp_operation.assert_not_called()
        self.assertEqual(self.models.Commission.query.count(), 0)

    def test_we_scope_and_name_are_persisted_and_supplied_to_the_app(self):
        commission = self.create_order("economic_unit", 1)
        commission_id = commission.id
        self.db.session.expire_all()
        stored = self.db.session.get(self.models.Commission, commission_id)
        payload = self.api.serialize_commission(stored)
        self.assertEqual(payload["object_type"], "economic_unit")
        self.assertEqual(payload["economic_unit_idnum"], "WE-COMMON")
        self.assertEqual(payload["economic_unit_name"], "COMMON Siedlung")
        self.assertEqual(payload["apartment_address"], "COMMON Straße 1")
        self.assertIsNone(payload["license_agreement_id"])
        self.app.json.dumps(payload)

    def test_existing_building_and_unit_orders_are_not_mislabeled_as_entire_we_orders(self):
        for values, expected in (
            ({"building_erp_id": 2002}, "building"),
            ({"building_erp_id": 2002, "use_unit_id": 3003}, "use_unit"),
            ({"building_erp_id": 2002, "license_agreement_idnum": "MV-1"}, "license_agreement"),
        ):
            with self.subTest(expected=expected):
                commission = self.models.Commission(economic_unit_erp_id=1001, **values)
                self.assertEqual(commission.object_type, expected)

    def test_existing_we_orders_keep_scope_when_an_address_is_added_and_reuse_the_process(self):
        commission = self.create_order("economic_unit", 1)
        commission.building_erp_id = None
        commission.building_idnum = None
        commission.apartment_address = None
        commission.crafts_process_erp_id = 9100
        commission.raw_payload = {"sync_signature": "existing-signature", "push_sync_status": [12, "WaitForCraftsmanFeedback"]}
        self.db.session.commit()
        gateway = self.gateway_with_mocked_client()
        gateway._find_crafts_process_by_id = Mock(return_value={"id": 9100})
        gateway.create_commission_waiting_for_acceptance(commission)
        self.db.session.commit()
        self.db.session.expire_all()
        self.assertEqual(commission.object_type, "economic_unit")
        self.assertEqual(commission.economic_unit_name, "COMMON Siedlung")
        self.assertEqual(commission.building_erp_id, 2002)
        self.assertEqual(commission.raw_payload["sync_signature"], "existing-signature")
        self.assertEqual(commission.raw_payload["push_sync_status"], [12, "WaitForCraftsmanFeedback"])
        gateway.client.create_commissioning_crafts_process.assert_not_called()
        self.assertEqual(gateway.client.create_commissioning_commission_wait_for_craftsman_feedback.call_args.kwargs["crafts_process_id"], 9100)

    def test_retries_keep_the_previously_selected_address_if_building_order_changes(self):
        commission = self.create_order("economic_unit", 1)
        self.empty_building.id_num = "GE-0001"
        self.db.session.commit()
        gateway = self.gateway_with_mocked_client()
        gateway.create_commission_waiting_for_acceptance(commission)
        self.assertEqual(gateway.client.create_commissioning_commission_wait_for_craftsman_feedback.call_args.kwargs["building_id"], 2002)
        self.assertEqual(commission.object_type, "economic_unit")

    def test_erp_sync_keeps_the_explicit_we_scope_and_name(self):
        commission = self.create_order("economic_unit", 1)
        commission.erp_id = 100
        self.db.session.commit()
        with patch.object(self.gateway, "configured_commission_type_ids", return_value={100}), patch.object(
            self.gateway, "configured_commission_status_ids", return_value={12},
        ):
            result = self.gateway.upsert_commission({
                "Id": 100, "IdNum": "A-100", "CommissionStatus": {"Id": 12, "Code": "WaitForCraftsmanFeedback"},
                "CommissionType": {"Id": 100}, "Craftsman": {"Id": 2020}, "RecordingDate": "2026-11-01",
            }, sync_signature="updated-signature")
        self.db.session.commit()
        self.db.session.expire_all()
        self.assertIsNotNone(result)
        self.assertEqual(commission.object_type, "economic_unit")
        self.assertEqual(commission.economic_unit_name, "COMMON Siedlung")
        self.assertIn("sync_signature", commission.raw_payload)

    def test_real_erp_json_contains_the_automatic_address_reference(self):
        from test_wowiport_client import transport

        commission = self.create_order("economic_unit", 1)
        gateway = self.gateway_with_mocked_client()
        lookups = gateway.client
        with patch("wowipy.rest_adapter.requests_cache.install_cache"):
            client = transport.WowiportClient(hostname="", user="test", password="test", api_key="test-key")
        client._rest_adapter.access_token = "test-token"
        client.get_license_agreements = lookups.get_license_agreements
        client.get_use_units = lookups.get_use_units
        client.get_building_lands = lookups.get_building_lands
        replies = []
        for erp_id, number in ((9100, "V-1"), (9200, "A-1")):
            reply = Mock(status_code=200, reason="OK")
            reply.json.return_value = {"Id": erp_id, "IdNum": number}
            replies.append(reply)
        client._rest_adapter._request = Mock(side_effect=replies)
        gateway.client = client
        gateway.create_commission_waiting_for_acceptance(commission)
        requests = client._rest_adapter._request.call_args_list
        self.assertEqual(len(requests), 2)
        for request in requests:
            self.assertEqual(request.kwargs["json"]["EconomicUnitId"], 1001)
            self.assertEqual(request.kwargs["json"]["BuildingId"], 2002)
            self.assertNotIn("UseUnitId", request.kwargs["json"])
            self.assertNotIn("LicenseAgreementId", request.kwargs["json"])


if __name__ == "__main__":
    unittest.main()

