import ast
import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token
from flask_sqlalchemy import SQLAlchemy


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
        )]

    def setUp(self):
        context = self.app.app_context()
        context.push()
        self.addCleanup(context.pop)
        self.addCleanup(self.db.session.remove)
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

    def create_order(self, object_type, object_id):
        commission = self.service.create_pending_commission({
            "object_type": object_type, "object_id": object_id,
            "craft_activity_id": self.craft.id, "commission_type_id": 100, "notify_tenants": "true",
            "positions": [{"service_catalog_id": 600, "commission_text": "Bitte reparieren", "units": 1}],
        }, self.creator, self.models.CommissionSource.WEB)
        self.db.session.commit()
        return commission

    def test_unit_search_keeps_an_occupied_unit_separate_from_its_contract(self):
        choices = self.directory.search_commission_objects("COMMON", "use_unit")
        self.assertEqual([(item["object_type"], item["object_id"]) for item in choices], [("use_unit", self.unit.id)])
        self.assertIsNone(choices[0]["license_agreement"])
        self.assertIsNone(choices[0]["tenant_name"])
        self.assertFalse(choices[0]["is_vacancy"])

    def test_tenant_and_contract_search_still_return_the_contract(self):
        for context in ("tenant", "agreement"):
            choices = self.directory.search_commission_objects("COMMON", context)
            self.assertEqual([item["object_type"] for item in choices], ["license_agreement"])
            self.assertEqual(choices[0]["tenant_name"], "Erika COMMON")

    def test_building_search_returns_buildings_without_promoting_units_to_contracts(self):
        choices = self.directory.search_commission_objects("COMMON", "address")
        self.assertEqual([item["object_type"] for item in choices], ["building"])
        self.assertIsNone(choices[0]["use_unit"])
        self.assertIsNone(choices[0]["license_agreement"])
        empty = self.directory.search_commission_objects("GE-EMPTY", "address")
        self.assertEqual(empty[0]["object_id"], self.empty_building.id)

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
            ("economic_unit", 10, (1010, None, None, None)),
        ):
            with self.subTest(object_type=object_type):
                commission = self.create_order(object_type, object_id)
                self.assertEqual((
                    commission.economic_unit_erp_id, commission.building_erp_id,
                    commission.use_unit_id, commission.license_agreement_id,
                ), expected)
                self.assertEqual(commission.user_id, self.craftsman.id)
                self.assertEqual(commission.company_code_erp_id, 77)
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

    def test_erp_process_and_order_receive_the_selected_reference_ids(self):
        for object_type, object_id, expected in (
            ("license_agreement", 4, (1001, 2002, 3003, 4004)),
            ("use_unit", 3, (1001, 2002, 3003, None)),
            ("building", 2, (1001, 2002, None, None)),
            ("economic_unit", 10, (1010, None, None, None)),
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
                    gateway.client.get_building_lands.assert_not_called()

    def test_erp_reference_resolution_still_rejects_an_order_without_any_object(self):
        gateway = self.gateway_with_mocked_client()
        with self.assertRaisesRegex(RuntimeError, "Objektangabe"):
            gateway.resolve_commission_reference_ids(self.models.Commission())

    def test_contractless_orders_do_not_queue_tenant_notifications_even_with_a_legacy_opt_in(self):
        commission = self.create_order("economic_unit", 10)
        commission.notify_tenants = True
        commission.erp_id = 1234
        commission.erp_sync_state = "synced"
        commission.status_id = 12
        self.db.session.commit()
        self.assertEqual(self.notifications.ensure_tenant_notifications(), 0)

    def test_mobile_search_accepts_the_selected_context_and_rejects_unknown_contexts(self):
        for context, object_type in (
            ("tenant", "license_agreement"), ("agreement", "license_agreement"),
            ("use_unit", "use_unit"), ("address", "building"), ("economic_unit", "economic_unit"),
        ):
            with self.subTest(context=context):
                response = self.client.get(f"/api/commission-objects/search?q=COMMON&context={context}", headers=self.headers)
                self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                self.assertEqual({item["object_type"] for item in response.get_json()["items"]}, {object_type})
        response = self.client.get("/api/commission-objects/search?q=COMMON&context=invalid", headers=self.headers)
        self.assertEqual(response.status_code, 400)

    def test_mobile_search_keeps_tenant_search_as_the_default(self):
        response = self.client.get("/api/commission-objects/search?q=Erika", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual({item["object_type"] for item in response.json["items"]}, {"license_agreement"})

    def test_queued_contractless_notification_is_discarded_before_contacting_wowiport(self):
        commission = self.create_order("economic_unit", 10)
        commission.notify_tenants = True
        commission.status_id = 12
        notification = self.models.CommissionTenantNotification(commission=commission, state="sending")
        self.db.session.add(notification)
        self.db.session.commit()
        with patch.object(self.gateway, "WowiportService") as gateway:
            self.notifications.execute_tenant_notification(notification.id)
        self.assertEqual(notification.state, "discarded")
        gateway.assert_not_called()


if __name__ == "__main__":
    unittest.main()
