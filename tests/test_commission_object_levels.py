import ast
import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from flask import Flask
from flask_login import LoginManager, login_required
from flask_jwt_extended import JWTManager, create_access_token
from flask_sqlalchemy import SQLAlchemy


class CommissionObjectLevelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.db = SQLAlchemy()
        cls.app = Flask(__name__, template_folder=str(cls.root / "app" / "templates"))
        cls.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite://", TESTING=True,
            SECRET_KEY="object-level-session-test",
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
        sources = ("object_directory", "commission_service", "wowiport", "tenant_notifications", "api", "admin")
        real_modules = {"models", "extensions", "settings", "utils", "blacklist", "email_templates", "email_layout", "health_rules", *sources}
        for source_name in sources:
            source = ast.parse((cls.root / "app" / (source_name + ".py")).read_text())
            for node in ast.walk(source):
                if not isinstance(node, ast.ImportFrom) or node.level != 1 or node.module in real_modules:
                    continue
                module_name = cls.package_name + "." + node.module
                module = modules.setdefault(module_name, types.ModuleType(module_name))
                for item in node.names:
                    setattr(module, item.name, login_required if node.module == "auth" else Mock())
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
        cls.admin = importlib.import_module(cls.package_name + ".admin")
        login_manager = LoginManager(cls.app)
        login_manager.user_loader(lambda user_id: cls.db.session.get(cls.models.User, int(user_id)))
        cls.app.register_blueprint(cls.api.api_bp)
        cls.app.register_blueprint(cls.admin.admin_bp)
        cls.app.add_url_rule("/logout", endpoint="auth.logout", view_func=lambda: "")
        for name in ("local_official", "office_chat_read", "office_chat_status", "office_chat_create"):
            cls.app.add_url_rule("/process-info/" + name + "/<int:commission_id>", endpoint="process_info_web." + name, view_func=lambda commission_id: "")
        cls.app.jinja_env.globals["csrf_token"] = lambda: "test-token"
        cls.tables = [model.__table__ for model in (
            cls.models.AppSetting, cls.models.BlacklistEntry, cls.models.User,
            cls.models.UserJurisdictionPersonMapping, cls.models.WowiResponsibleOfficial,
            cls.models.WowiCompanyCode, cls.models.WowiEconomicUnit,
            cls.models.WowiEconomicUnitJurisdiction, cls.models.WowiBuilding,
            cls.models.WowiUseUnit, cls.models.WowiLicenseAgreement, cls.models.WowiContractor,
            cls.models.WowiDepartment, cls.models.CraftActivity,
            cls.models.CraftActivityDepartmentMapping, cls.models.TimeTaskTemplate,
            cls.models.Commission, cls.models.CommissionPosition, cls.models.CommissionTenantNotification,
            cls.models.ErpOutboxOperation, cls.models.CommissionPhoto, cls.models.CommissionAppointmentRequest,
            cls.models.AuditLog, cls.models.TimeEntry, cls.models.InventoryMovement, cls.models.InventoryItem,
            cls.models.ProcessInfo,
        )]

    def setUp(self):
        context = self.app.app_context()
        context.push()
        self.addCleanup(context.pop)
        self.addCleanup(self.db.session.remove)
        self.db.metadata.drop_all(self.db.engine, tables=self.tables)
        self.db.metadata.create_all(self.db.engine, tables=self.tables)
        self.creator = self.models.User(email="creator@example.invalid", display_name="Sachbearbeitung", person_erp_id=101, is_foreman=True)
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
        self.addressed_economic = self.models.WowiEconomicUnit(
            id=11, erp_id=1011, id_num="WE-ADDRESS", name="Gartenanlage", location="Lünen",
            company_code_erp_id=77, company_code_code="BW", universal_responsibility=True,
            universal_responsible_official_erp_id=901, universal_responsible_person_erp_id=202,
        )
        self.address_building = self.models.WowiBuilding(
            id=6, erp_id=2006, id_num="GE-ADDRESS", address="Gartenstraße 1", economic_unit=self.addressed_economic,
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
            self.addressed_economic, self.address_building,
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
        with self.client.session_transaction() as session:
            session["_user_id"] = str(self.creator.id)
            session["_fresh"] = True
        self.headers = {"Authorization": "Bearer " + create_access_token(identity=str(self.creator.id))}

    def create_order(self, object_type, object_id, **values):
        commission = self.service.create_pending_commission({
            "object_type": object_type, "object_id": object_id,
            "craft_activity_id": self.craft.id, "commission_type_id": 100, "notify_tenants": "true",
            "positions": [{"service_catalog_id": 600, "commission_text": "Bitte reparieren", "units": 1}],
            **values,
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
            ("economic_unit", 11, (1011, 2006, None, None)),
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
        gateway.client.get_building_lands.return_value = [
            types.SimpleNamespace(id_=2002, id_num="GE-COMMON"),
            types.SimpleNamespace(id_=2006, id_num="GE-ADDRESS"),
        ]
        return gateway

    def test_erp_process_and_order_receive_the_selected_reference_ids(self):
        for object_type, object_id, expected in (
            ("license_agreement", 4, (1001, 2002, 3003, 4004)),
            ("use_unit", 3, (1001, 2002, 3003, None)),
            ("building", 2, (1001, 2002, None, None)),
            ("economic_unit", 11, (1011, 2006, None, None)),
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

    def test_contractless_orders_do_not_queue_tenant_notifications_even_with_a_legacy_opt_in(self):
        commission = self.create_order("economic_unit", 11)
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
        commission = self.create_order("economic_unit", 11)
        commission.notify_tenants = True
        commission.status_id = 12
        notification = self.models.CommissionTenantNotification(commission=commission, state="sending")
        self.db.session.add(notification)
        self.db.session.commit()
        with patch.object(self.gateway, "WowiportService") as gateway:
            self.notifications.execute_tenant_notification(notification.id)
        self.assertEqual(notification.state, "discarded")
        gateway.assert_not_called()

    def test_we_search_exposes_only_active_buildings_with_an_address(self):
        self.empty_building.address = "  "
        self.db.session.add(self.models.WowiBuilding(
            id=8, erp_id=2008, address="Inaktive Straße 1", economic_unit=self.economic, active=False,
        ))
        self.db.session.commit()
        choice = self.directory.get_commission_object_choice("economic_unit", 1)
        self.assertEqual([item["id"] for item in choice["address_buildings"]], [2])
        self.assertEqual(choice["address_building"]["id"], 2)
        self.assertIsNone(choice["building"])
        self.assertEqual(choice["object_type"], "economic_unit")

    def test_we_creation_rejects_a_missing_or_ambiguous_address_before_queuing(self):
        for object_id, message in ((1, "ausgewählt"), (10, "keine aktive Gebäudeadresse")):
            with self.subTest(object_id=object_id), self.assertRaisesRegex(ValueError, message):
                self.create_order("economic_unit", object_id)
        self.service.enqueue_erp_operation.assert_not_called()
        self.assertEqual(self.models.Commission.query.count(), 0)

    def test_we_creation_accepts_a_selected_address_without_a_tenant_contract(self):
        commission = self.create_order("economic_unit", 1, address_building_id=5)
        self.assertEqual((commission.economic_unit_erp_id, commission.building_erp_id), (1001, 2005))
        self.assertEqual(commission.apartment_address, "Nebengebäude")
        self.assertIsNone(commission.use_unit_id)
        self.assertIsNone(commission.license_agreement_id)
        self.assertFalse(commission.notify_tenants)
        self.assertEqual(commission.user_id, self.craftsman.id)

    def test_we_creation_rejects_foreign_inactive_empty_and_invalid_address_choices(self):
        for building_id in (6, 9999, "invalid"):
            with self.subTest(building_id=building_id), self.assertRaisesRegex(ValueError, "Gebäudeadresse"):
                self.create_order("economic_unit", 1, address_building_id=building_id)
        for attribute, value in (("active", False), ("address", "  ")):
            with self.subTest(attribute=attribute):
                original = getattr(self.empty_building, attribute)
                setattr(self.empty_building, attribute, value)
                self.db.session.commit()
                with self.assertRaisesRegex(ValueError, "Gebäudeadresse"):
                    self.create_order("economic_unit", 1, address_building_id=5)
                setattr(self.empty_building, attribute, original)
                self.db.session.commit()
        self.service.enqueue_erp_operation.assert_not_called()

    def test_a_selected_we_address_can_be_restored_after_form_validation(self):
        choice = self.directory.get_commission_object_choice("economic_unit", 1, address_building_id="5")
        self.assertEqual(choice["address_building"]["id"], 5)
        self.assertEqual({item["id"] for item in choice["address_buildings"]}, {2, 5})
        with self.app.test_request_context(method="POST", data={"address_building_id": "5"}):
            self.assertEqual(self.admin.read_commission_form()["address_building_id"], "5")

    def test_assignment_preview_uses_the_selected_we_address(self):
        path = f"/admin/commissions/assignment-preview?object_type=economic_unit&object_id=1&craft_activity_id={self.craft.id}"
        response = self.client.get(path + "&address_building_id=5")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.json["company_code_erp_id"], 77)
        self.assertEqual(response.json["craftsman_user_id"], self.craftsman.id)
        for suffix in ("", "&address_building_id=6"):
            with self.subTest(suffix=suffix):
                response = self.client.get(path + suffix)
                self.assertEqual(response.status_code, 400)
                self.assertIn("Gebäudeadresse", response.json["message"])

    def legacy_we_order(self, economic_unit=None):
        commission = self.create_order("economic_unit", 11)
        commission.economic_unit_erp_id = (economic_unit or self.addressed_economic).erp_id
        commission.building_erp_id = None
        commission.building_idnum = None
        commission.apartment_address = None
        commission.erp_sync_state = self.models.CommissionSyncState.FAILED.value
        operation = self.models.ErpOutboxOperation(
            commission=commission, operation_type=self.models.ErpOperationType.CREATE_COMMISSION.value,
            idempotency_key=f"commission:create:{commission.public_id}", state=self.models.ErpOutboxState.FAILED.value,
            attempt_count=2, last_error="EstateAddress: Das Feld Adresse darf nicht leer sein.",
        )
        self.db.session.add(operation)
        self.db.session.commit()
        return commission, operation

    def test_existing_we_order_recovers_its_unique_address_and_reuses_the_erp_process(self):
        commission, operation = self.legacy_we_order()
        commission.crafts_process_erp_id = 9100
        self.db.session.commit()
        gateway = self.gateway_with_mocked_client()
        gateway._find_crafts_process_by_id = Mock(return_value={"id": 9100})
        result = gateway.create_commission_waiting_for_acceptance(commission)
        self.assertEqual(result.data["Id"], 9200)
        self.assertEqual(commission.apartment_address, "Gartenstraße 1")
        self.assertEqual(commission.building_erp_id, 2006)
        gateway.client.create_commissioning_crafts_process.assert_not_called()
        payload = gateway.client.create_commissioning_commission_wait_for_craftsman_feedback.call_args.kwargs
        self.assertEqual((payload["crafts_process_id"], payload["economic_unit_id"], payload["building_id"]), (9100, 1011, 2006))

    def test_existing_we_orders_without_a_clear_address_fail_before_any_erp_write(self):
        for economic in (self.economic, self.empty_economic):
            with self.subTest(economic=economic.id):
                commission, operation = self.legacy_we_order(economic)
                gateway = self.gateway_with_mocked_client()
                with self.assertRaisesRegex(RuntimeError, "Adresse ergänzen: /admin/commissions/"):
                    gateway.create_commission_waiting_for_acceptance(commission)
                gateway.client.create_commissioning_crafts_process.assert_not_called()
                gateway.client.create_commissioning_commission_wait_for_craftsman_feedback.assert_not_called()

    def test_real_wowipy_posts_include_the_we_building_address_reference(self):
        from test_wowiport_client import transport

        commission = self.create_order("economic_unit", 11)
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
        self.assertTrue(requests[1].args[1].endswith("Commission/WaitForCraftsmanFeedback"))
        for request in requests:
            self.assertEqual(request.kwargs["json"]["EconomicUnitId"], 1011)
            self.assertEqual(request.kwargs["json"]["BuildingId"], 2006)
            self.assertNotIn("LicenseAgreementId", request.kwargs["json"])
            self.assertNotIn("UseUnitId", request.kwargs["json"])

    def test_address_repair_updates_the_existing_order_and_outbox_operation(self):
        commission, operation = self.legacy_we_order(self.economic)
        commission.crafts_process_erp_id = 9100
        self.db.session.commit()
        response = self.client.post(f"/admin/commissions/{commission.id}/address", data={"address_building_id": "5"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(commission.building_erp_id, 2005)
        self.assertEqual(commission.apartment_address, "Nebengebäude")
        self.assertEqual(commission.crafts_process_erp_id, 9100)
        self.assertEqual(commission.economic_unit_erp_id, 1001)
        self.assertEqual(operation.state, "pending")
        self.assertEqual(operation.attempt_count, 2)
        self.assertIsNone(operation.last_error)
        self.assertEqual(self.models.Commission.query.count(), 1)
        self.assertEqual(self.models.ErpOutboxOperation.query.count(), 1)

    def test_address_repair_rejects_foreign_buildings_and_processing_or_synced_orders(self):
        commission, operation = self.legacy_we_order(self.economic)
        path = f"/admin/commissions/{commission.id}/address"
        response = self.client.post(path, data={"address_building_id": "6"})
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(commission.building_erp_id)
        self.assertEqual(operation.state, "failed")
        operation.state = "processing"
        self.db.session.commit()
        self.client.post(path, data={"address_building_id": "5"})
        self.assertIsNone(commission.building_erp_id)
        self.assertEqual(operation.state, "processing")
        operation.state = "failed"
        commission.erp_id = 9200
        self.db.session.commit()
        self.client.post(path, data={"address_building_id": "5"})
        self.assertIsNone(commission.building_erp_id)
        self.assertEqual(operation.state, "failed")

    def test_address_repair_requires_a_logged_in_user(self):
        commission, operation = self.legacy_we_order(self.economic)
        response = self.app.test_client().post(
            f"/admin/commissions/{commission.id}/address", data={"address_building_id": "5"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertIsNone(commission.building_erp_id)
        self.assertEqual(operation.state, "failed")

    def test_failed_we_order_detail_offers_its_own_addresses_for_repair(self):
        commission, operation = self.legacy_we_order(self.economic)
        self.admin.format_manual_appointment.return_value = None
        response = self.client.get(f"/admin/commissions/{commission.id}")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        html = response.get_data(as_text=True)
        self.assertIn("Gebäudeadresse für WE-Auftrag ergänzen", html)
        self.assertIn('name="address_building_id" value="5"', html)
        self.assertNotIn('name="address_building_id" value="6"', html)


if __name__ == "__main__":
    unittest.main()
