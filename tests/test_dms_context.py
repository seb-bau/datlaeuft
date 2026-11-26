import ast
import importlib
import importlib.util
import sys
import types
import unittest
from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, call, patch

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from flask import Flask, current_app, request
from flask_jwt_extended import JWTManager
from flask_sqlalchemy import SQLAlchemy


class DmsContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.db = SQLAlchemy()
        cls.app = Flask(__name__)
        cls.app.config.update(SQLALCHEMY_DATABASE_URI="sqlite://", TESTING=True)
        cls.db.init_app(cls.app)
        cls.package_name = "_dms_context_test_app"
        package = types.ModuleType(cls.package_name)
        package.__path__ = [str(cls.root / "app")]
        extensions = types.ModuleType(cls.package_name + ".extensions")
        extensions.db = cls.db
        extensions.jwt = JWTManager(cls.app)
        modules = {cls.package_name: package, extensions.__name__: extensions}
        source_names = {"dms_context", "dms_service", "dms_client", "invoice_archive", "safety_inspection_archive", "user_sync", "entra"}
        real_modules = {"models", "extensions", "settings", "utils", "blacklist", *source_names}
        for name in source_names:
            source = ast.parse((cls.root / "app" / (name + ".py")).read_text())
            for node in ast.walk(source):
                if not isinstance(node, ast.ImportFrom) or node.level != 1 or node.module in real_modules:
                    continue
                module_name = cls.package_name + "." + node.module
                module = modules.setdefault(module_name, types.ModuleType(module_name))
                for item in node.names:
                    setattr(module, item.name, Mock())
        module_patch = patch.dict(sys.modules, modules)
        module_patch.start()
        cls.addClassCleanup(module_patch.stop)
        for attribute, name in (("models", "models"), ("settings", "settings"), ("context", "dms_context"), ("service", "dms_service"), ("user_sync", "user_sync"), ("entra", "entra")):
            setattr(cls, attribute, importlib.import_module(cls.package_name + "." + name))
        cls.tables = [model.__table__ for model in (
            cls.models.User, cls.models.AppSetting, cls.models.BlacklistEntry,
            cls.models.CraftActivity, cls.models.UserJurisdictionPersonMapping,
            cls.models.WowiCompanyCode, cls.models.WowiEconomicUnit,
            cls.models.WowiEconomicUnitJurisdiction, cls.models.WowiBuilding,
            cls.models.WowiUseUnit, cls.models.WowiLicenseAgreement,
            cls.models.DmsProfile, cls.models.DmsDocument, cls.models.Invoice,
        )] + [cls.models.user_craft_activity]

    def setUp(self):
        context = self.app.app_context()
        context.push()
        self.addCleanup(context.pop)
        self.addCleanup(self.db.session.remove)
        self.db.metadata.drop_all(self.db.engine, tables=self.tables)
        self.db.metadata.create_all(self.db.engine, tables=self.tables)
        self.company = self.models.WowiCompanyCode(erp_id=77, code="BW", name="Buchwerk")
        self.user = self.models.User(
            email="inspector@example.invalid", display_name="Anna Maria von der Linden",
            first_name="Anna Maria", last_name="von der Linden",
        )
        self.db.session.add_all([self.company, self.user])
        self.db.session.commit()

    def invoice(self, commissions=()):
        return self.models.Invoice(
            company_code=self.company, invoice_date=date(2026, 11, 20),
            commission_links=[self.models.InvoiceCommission(commission=commission) for commission in commissions],
        )

    def commission(self, **values):
        return self.models.Commission(
            erp_idnum="A-100", title="Reparatur", building_idnum="0001",
            economic_unit_idnum="0002", use_unit_idnum="0003", **values,
        )

    def schema(self, data_type="STRING", multiple=True):
        return {"fields": [
            {"id": "key", "name": "Schlüssel", "dataType": "STRING", "isMultiValue": False},
            {"id": "object", "name": "Objekt", "dataType": data_type, "isMultiValue": multiple},
        ]}

    def test_object_fields_are_available_for_invoices_work_reports_and_all_protocol_levels(self):
        for document_type in (
            "invoice", "cancellation_invoice", "invoice_with_work_reports", "work_report",
            "safety_protocol_building", "safety_protocol_economic_unit", "safety_protocol_use_unit",
        ):
            with self.subTest(document_type=document_type):
                self.assertTrue(self.context.OBJECT_IDNUM_FIELDS <= self.context.available_fields(document_type).keys())
        for document_type in ("safety_protocol_building", "safety_protocol_economic_unit", "safety_protocol_use_unit"):
            self.assertTrue({"schedule_name", "inspector_name", "completed_date", "completed_at", "completed_by"} <= self.context.available_fields(document_type).keys())

    def test_invoice_object_numbers_are_sorted_deduplicated_and_keep_leading_zeros(self):
        first = self.commission()
        second = self.commission()
        second.building_idnum = "0004"
        second.use_unit_idnum = " 0005 "
        third = self.commission()
        third.use_unit_idnum = None
        values = self.context.invoice_context(self.invoice([second, first, third]))
        self.assertEqual(values["building_idnum"], ["0001", "0004"])
        self.assertEqual(values["economic_unit_idnum"], ["0002"])
        self.assertEqual(values["use_unit_idnum"], ["0003", "0005"])
        self.assertEqual(values["invoice_date"], "2026-11-20")

    def test_cancellation_invoice_uses_the_original_orders(self):
        original = self.invoice([self.commission()])
        cancellation = self.invoice()
        cancellation.original_invoice = original
        self.assertEqual(self.context.invoice_context(cancellation)["building_idnum"], ["0001"])

    def test_invoices_without_orders_resolve_the_billing_contract_object(self):
        economic = self.models.WowiEconomicUnit(erp_id=10, id_num="0010")
        building = self.models.WowiBuilding(erp_id=20, id_num="0020", economic_unit=economic)
        unit = self.models.WowiUseUnit(erp_id=30, id_num="0030", building=building, economic_unit=economic)
        agreement = self.models.WowiLicenseAgreement(erp_id=40, id_num="0040", use_unit=unit)
        self.db.session.add(agreement)
        self.db.session.commit()
        for tenant_charge in (False, True):
            with self.subTest(tenant_charge=tenant_charge):
                invoice = self.invoice()
                if tenant_charge:
                    invoice.tenant_billing_reference = {"agreement_erp_id": 40, "agreement_number": "0040"}
                else:
                    invoice.recipient_license_agreement_erp_id = 40
                values = self.context.invoice_context(invoice)
                self.assertEqual(values["building_idnum"], ["0020"])
                self.assertEqual(values["economic_unit_idnum"], ["0010"])
                self.assertEqual(values["use_unit_idnum"], ["0030"])

    def test_missing_objects_stay_empty(self):
        invoice = self.invoice()
        self.assertEqual(self.context.invoice_context(invoice)["building_idnum"], [])
        invoice.recipient_license_agreement_erp_id = 999
        self.assertEqual(self.context.invoice_context(invoice)["use_unit_idnum"], [])

    def test_work_report_contains_the_order_object_and_keeps_existing_metadata(self):
        commission = self.commission()
        commission.completed_at = datetime(2026, 11, 20, 10, tzinfo=timezone.utc)
        commission.completion_submitted_by = self.user
        entries = [types.SimpleNamespace(minutes=45, user=self.user)]
        values = self.context.work_report_context(commission, entries, "all")
        self.assertEqual(values["building_idnum"], "0001")
        self.assertEqual(values["economic_unit_idnum"], "0002")
        self.assertEqual(values["use_unit_idnum"], "0003")
        self.assertEqual(values["total_minutes"], 45)
        self.assertEqual(values["completed_by"], self.user.display_name)
        self.assertEqual(values["completed_at"], "2026-11-20T10:00:00+00:00")

    def test_protocol_adds_schedule_and_structured_name_and_a_local_completion_date(self):
        job = types.SimpleNamespace(
            id=1, public_id="job-1", status="completed", object_level="building", object_erp_id=20,
            template_version=types.SimpleNamespace(template=types.SimpleNamespace(name="Sichtprüfung")),
            group=types.SimpleNamespace(name="Prüfgruppe", schedule=types.SimpleNamespace(name="Monatliche Kontrolle")),
            period_start=date(2026, 11, 1), period_end=date(2026, 11, 30),
            due_from=date(2026, 11, 1), due_to=date(2026, 11, 30),
            completed_at=datetime(2026, 11, 19, 23, 30, tzinfo=timezone.utc), completed_by=self.user,
            company_code_erp_id=77, company_code_code="BW", economic_unit_erp_id=10,
            economic_unit_idnum="0010", economic_unit_name="Siedlung", building_erp_id=20,
            building_idnum="0020", use_unit_erp_id=None, use_unit_idnum=None,
            object_address="Beispielstraße", object_location=None, department=None, defects=[],
        )
        protocol = types.SimpleNamespace(id=1, version_number=1, job=job, correction_reason=None)
        values = self.context.protocol_context(protocol)
        self.assertEqual(values["schedule_name"], "Monatliche Kontrolle")
        self.assertEqual(values["inspector_name"], "von der Linden Anna Maria")
        self.assertEqual(values["completed_by"], "Anna Maria von der Linden")
        self.assertEqual(values["completed_date"], "2026-11-20")
        self.assertEqual(values["completed_at"], "2026-11-19T23:30:00+00:00")
        job.completed_at = None
        job.completed_by = None
        values = self.context.protocol_context(protocol)
        self.assertEqual(values["completed_date"], "")
        self.assertEqual(values["inspector_name"], "")

    def test_completion_date_handles_utc_database_values_and_summer_time(self):
        for timestamp, expected in (
            (datetime(2026, 11, 19, 23, 30), "2026-11-20"),
            (datetime(2026, 7, 19, 22, 30, tzinfo=timezone.utc), "2026-07-20"),
            (None, ""),
        ):
            with self.subTest(timestamp=timestamp):
                self.assertEqual(self.context.date_only(timestamp), expected)

    def test_document_snapshot_keeps_object_values_and_uploads_real_multivalues(self):
        first, second = self.commission(), self.commission()
        second.building_idnum = "0004"
        values = self.context.invoice_context(self.invoice([first, second]))
        with TemporaryDirectory() as directory, patch.object(self.app, "instance_path", directory):
            document = self.service.register_generated_pdf("invoice", "commission", 1, "rechnung.pdf", b"%PDF-1.4\n", values)
            self.db.session.commit()
            first.building_idnum = "Geändert"
            self.db.session.expire(document)
            self.assertEqual(document.context["building_idnum"], ["0001", "0004"])
            document.profile_snapshot = {
                "key_property_id": "key", "schema": self.schema(),
                "mappings": {"object": {"kind": "field", "value": "building_idnum"}},
            }
            client = Mock()
            client.add_upload_property.side_effect = lambda name, value, prop_guid, plist: plist + [{"id": prop_guid, "values": value}]
            properties = self.service.upload_properties(document, client)
            self.assertEqual(properties[1], {"id": "object", "values": ["0001", "0004"]})
            document.profile_snapshot["schema"] = self.schema(multiple=False)
            with self.assertRaisesRegex(self.service.DmsError, "erlaubt nur einen Wert"):
                self.service.upload_properties(document, client)
            document.context = {**document.context, "building_idnum": ["0001"]}
            self.assertEqual(self.service.upload_properties(document, client)[1]["values"], ["0001"])

    def test_templates_only_offer_scalar_object_fields_and_profiles_accept_new_sources(self):
        self.assertNotIn("building_idnum", self.context.template_fields("invoice"))
        self.assertIn("building_idnum", self.context.template_fields("work_report"))
        profile = self.models.DmsProfile(document_type="invoice", category_id="invoices", key_property_id="key")
        profile.mappings = {"object": {"kind": "field", "value": "building_idnum"}}
        self.service.validate_profile(profile, self.schema())
        profile.mappings = {"object": {"kind": "text", "value": "{building_idnum}"}}
        with self.assertRaises(self.service.DmsError):
            self.service.validate_profile(profile, self.schema())
        profile.document_type = "work_report"
        self.service.validate_profile(profile, self.schema(multiple=False))
        profile.document_type = "safety_protocol_building"
        for source in ("schedule_name", "inspector_name", "completed_date", "building_idnum"):
            profile.mappings = {"object": {"kind": "field", "value": source}}
            self.service.validate_profile(profile, self.schema())

    def test_invoice_link_uses_the_archived_repository_even_after_the_connection_changes(self):
        invoice = self.models.Invoice(
            company_code=self.company, company_code_erp_id=self.company.erp_id, company_code_code=self.company.code,
            created_by=self.user, dms_document_id="D000000123",
        )
        self.db.session.add(invoice)
        self.db.session.flush()
        now = self.models.utc_now()
        self.db.session.add(self.models.DmsDocument(
            id="archived", document_type="invoice", entity_type="invoice", entity_id=invoice.id,
            filename="rechnung.pdf", storage_name="rechnung.pdf", sha256="a" * 64, context={},
            hostname="archiv.example.invalid", repository_id="repo-alt", status="archived",
            available_at=now, created_at=now, archived_at=now, dms_document_id=invoice.dms_document_id,
        ))
        keys = self.settings.SettingKeys
        self.settings.settings.set(keys.DMS_HOSTNAME, "anderes-archiv.example.invalid")
        self.settings.settings.set(keys.DMS_REPOSITORY, "repo-neu")
        self.db.session.commit()
        self.assertEqual(self.service.invoice_dms_url(invoice), "https://archiv.example.invalid/dms/r/repo-alt/o2m/D000000123")

    def test_invoice_versions_reuse_the_document_and_reconcile_a_lost_confirmation(self):
        invoice = self.models.Invoice(
            company_code=self.company, company_code_erp_id=self.company.erp_id, company_code_code=self.company.code,
            created_by=self.user, document_sha256=sha256(b"%PDF-1.4\nOriginal").hexdigest(),
        )
        self.db.session.add(invoice)
        self.settings.settings.set(self.settings.SettingKeys.DMS_ENABLED, True)
        self.db.session.commit()
        connection = ("dms.example.invalid", "key", "repository")
        profile = {"category_id": "invoices", "key_property_id": "", "schema": {"fields": []}, "mappings": {}}
        remote_id = "D000000001"
        remote_content = {}
        archive_calls = []

        def new_client(*_args, before_create):
            client = Mock()

            def create(path, **_kwargs):
                before_create()
                remote_content[remote_id] = Path(path).read_bytes()
                archive_calls.append(("create", remote_id, remote_content[remote_id]))
                return remote_id

            def version(path, document_id):
                before_create()
                remote_content[document_id] = path.read_bytes()
                archive_calls.append(("version", document_id, remote_content[document_id]))
                if len(archive_calls) == 2:
                    raise self.service.DmsError("Timeout", retryable=True, uncertain=True)

            client.archive_file.side_effect = create
            client.archive_invoice_version.side_effect = version
            client.document_sha256.side_effect = lambda document_id: sha256(remote_content[document_id]).hexdigest()
            return client

        original, letter, amended = (b"%PDF-1.4\n" + content for content in (b"Original", b"Anschreiben", b"Anhang"))
        with (
            TemporaryDirectory() as directory,
            patch.object(self.app, "instance_path", directory),
            patch.object(self.service, "connection", return_value=connection),
            patch.object(self.service, "profile_snapshot", return_value=profile),
            patch.object(self.service, "CloudDmsClient", side_effect=new_client),
        ):
            def register(content):
                return self.service.register_generated_pdf("invoice", "invoice", invoice.id, "Rechnung.pdf", content, {})

            first, second, third = map(register, (original, letter, amended))
            self.assertIs(register(amended), third)
            self.db.session.commit()

            self.assertEqual(self.service.process_dms_batch(), 2)
            self.assertEqual([item.status for item in (first, second, third)], ["archived", "uncertain", "pending"])
            self.assertEqual(second.dms_document_id, remote_id)
            second.available_at = self.models.utc_now()
            self.db.session.commit()

            self.assertEqual(self.service.process_dms_batch(), 2)
            self.assertEqual([item.invoice_version for item in (first, second, third)], [1, 2, 3])
            self.assertEqual([item.dms_document_id for item in (first, second, third)], [remote_id] * 3)
            self.assertEqual([item.status for item in (first, second, third)], ["archived"] * 3)
            self.assertEqual(archive_calls, [("create", remote_id, original), ("version", remote_id, letter), ("version", remote_id, amended)])
            self.assertEqual(invoice.dms_document_id, remote_id)
            self.assertEqual(invoice.dms_archive_status, "archived")
            self.assertEqual(invoice.dms_archive_pdf_sha256, sha256(amended).hexdigest())
            self.assertEqual(invoice.document_sha256, sha256(original).hexdigest())
            self.assertEqual(invoice.dms_archive_key, first.context["archive_key"])

    def test_invoice_version_upload_follows_the_dms_link_and_preserves_properties(self):
        client_type = self.service.CloudDmsClient
        with patch.object(client_type, "get_mappings"):
            callback = Mock()
            client = client_type("dms.example.invalid", "key", "repository", before_create=callback)
        self.addCleanup(client.close)
        document_id = "D000000001"
        prefix = "/dms/r/repository"
        target = f"{prefix}/o2m/{document_id}"
        blob = f"{prefix}/blob/chunk/uploaded"
        details = Mock(status_code=200)
        details.json.return_value = {
            "id": document_id, "sourceProperties": [{"key": "property_state", "value": "Release"}],
        }
        search = Mock(status_code=200)
        search.json.return_value = {"items": [{"id": document_id, "_links": {"updateWithContent": {"href": target}}}]}
        with (
            TemporaryDirectory() as directory,
            patch.object(client._rest_adapter.session, "request", side_effect=[
                details, search, Mock(status_code=201, headers={"Location": blob}), Mock(status_code=200),
            ]) as requests,
        ):
            pdf = Path(directory) / "Rechnung.pdf"
            pdf.write_bytes(b"%PDF-1.4\n")
            client.archive_invoice_version(pdf, document_id)

        origin = "https://dms.example.invalid"
        self.assertEqual([request.args for request in requests.call_args_list], [
            ("GET", origin + target), ("GET", origin + prefix + "/srm"),
            ("POST", origin + prefix + "/blob/chunk/"), ("PUT", origin + target),
        ])
        self.assertEqual(requests.call_args.kwargs["json"], {
            "sourceId": prefix + "/source", "contentLocationUri": blob,
            "filename": "Rechnung.pdf", "alterationText": "Rechnungsunterlagen aktualisiert",
            "sourceProperties": {"properties": []},
        })
        self.assertEqual(callback.call_args_list, [call()])

    def test_wowiport_import_stores_name_parts_and_detects_changes(self):
        self.user.wowiport_managed = True
        craft = self.models.CraftActivity(erp_id=50, name="Prüfung")
        self.db.session.add(craft)
        self.db.session.commit()
        row = {
            "Id": 100, "Person": {"Name": self.user.display_name, "NaturalPerson": {"FirstName": "Anna Maria", "LastName": "von der Linden"}},
            "MainCommunication": {"Content": self.user.email},
            "CraftsmanAccessibilityList": [{"CraftActivityId": 50}],
        }
        self.user.first_name = self.user.last_name = None
        self.db.session.commit()
        with patch.object(self.user_sync, "_configured_email_domains", return_value={"example.invalid"}):
            result = self.user_sync.sync_users_from_wowiport(Mock(), craftsman_rows=[row])
            self.db.session.commit()
            self.assertEqual(result.updated, 1)
            self.assertEqual(self.context.inspector_name(self.user), "von der Linden Anna Maria")
            row["Person"]["NaturalPerson"]["LastName"] = "Müller"
            result = self.user_sync.sync_users_from_wowiport(Mock(), craftsman_rows=[row])
            self.assertEqual(result.updated, 1)
            self.assertEqual(self.user.last_name, "Müller")

    def test_wowiport_only_fills_missing_name_parts_for_entra_managed_users(self):
        self.user.entra_oid = "entra-1"
        self.user.first_name = None
        self.db.session.commit()
        row = {
            "Id": 100, "Person": {"NaturalPerson": {"FirstName": "Anna Maria", "LastName": "Falscher Nachname"}},
            "MainCommunication": {"Content": self.user.email},
            "CraftsmanAccessibilityList": [{"CraftActivityId": 50}],
        }
        with patch.object(self.user_sync, "_configured_email_domains", return_value={"example.invalid"}), patch.object(self.user_sync, "_responsible_person_candidates_by_email", return_value={}):
            self.user_sync.sync_users_from_wowiport(Mock(), craftsman_rows=[row])
        self.assertEqual(self.context.inspector_name(self.user), "von der Linden Anna Maria")

    def test_name_without_separate_parts_uses_the_existing_display_name(self):
        for first_name, last_name in ((None, None), (None, "von der Linden"), ("Anna Maria", None)):
            with self.subTest(first_name=first_name, last_name=last_name):
                self.user.first_name = first_name
                self.user.last_name = last_name
                self.assertEqual(self.context.inspector_name(self.user), "Anna Maria von der Linden")

    def test_entra_import_loads_and_updates_name_parts_without_parsing_display_names(self):
        response = Mock()
        response.json.return_value = {"value": [{
            "id": "entra-1", "mail": self.user.email, "displayName": "Anderer Anzeigename",
            "givenName": "Anna Maria", "surname": "von der Linden",
        }]}
        with patch.object(self.entra, "_acquire_graph_access_token", return_value="test-token"), patch.object(self.entra, "_configured_allowed_group_id", return_value=""), patch.object(self.entra.requests, "get", return_value=response) as get:
            self.entra.import_entra_users()
            self.assertEqual(self.context.inspector_name(self.user), "von der Linden Anna Maria")
            self.assertIn("givenName", get.call_args.kwargs["params"]["$select"])
            self.assertIn("surname", get.call_args.kwargs["params"]["$select"])
            response.json.return_value["value"][0]["surname"] = "Müller"
            self.entra.import_entra_users()
            self.assertEqual(self.user.last_name, "Müller")

    def test_login_claims_and_manual_user_form_keep_structured_names(self):
        source = ast.parse((self.root / "app/auth.py").read_text())
        functions = [node for node in source.body if isinstance(node, ast.FunctionDef) and node.name in {"resolve_generic_oidc_user", "resolve_microsoft_user"}]
        namespace = {
            "User": self.models.User, "Any": object, "db": self.db, "current_app": current_app,
            "is_entra_user_in_allowed_group": Mock(return_value=True), "get_entra_user_profile": Mock(),
            "include_blacklisted": self.user_sync.include_blacklisted,
        }
        exec(compile(ast.Module(body=functions, type_ignores=[]), "auth.py", "exec"), namespace)
        claims = {"email": self.user.email, "name": self.user.display_name, "given_name": "Anna", "family_name": "Müller"}
        self.assertIs(namespace["resolve_generic_oidc_user"](claims, False), self.user)
        self.assertEqual(self.context.inspector_name(self.user), "Müller Anna")
        self.user.entra_oid = "entra-1"
        self.db.session.commit()
        namespace["resolve_microsoft_user"]({**claims, "oid": "entra-1", "given_name": "Maria"}, False)
        self.assertEqual(self.context.inspector_name(self.user), "Müller Maria")
        source = ast.parse((self.root / "app/admin.py").read_text())
        function = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == "save_user_form")
        namespace = {"User": self.models.User, "request": request, "save_admin_push_preferences": Mock(), "save_user_approval_settings": Mock()}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "admin.py", "exec"), namespace)
        with self.app.test_request_context(method="POST", data={"email": self.user.email, "display_name": self.user.display_name, "first_name": "Anna Maria", "last_name": "von der Linden"}):
            namespace["save_user_form"](self.user)
        self.assertEqual(self.context.inspector_name(self.user), "von der Linden Anna Maria")


class UserNameMigrationTests(unittest.TestCase):
    def test_existing_users_survive_upgrade_and_downgrade(self):
        path = Path(__file__).resolve().parents[1] / "migrations/versions/94f2a6c8d0e3_user_name_parts.py"
        specification = importlib.util.spec_from_file_location("_user_name_migration", path)
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        engine = sa.create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE user_account (id INTEGER PRIMARY KEY, display_name VARCHAR(255))")
            connection.exec_driver_sql("INSERT INTO user_account VALUES (1, 'Anna Maria von der Linden')")
            with Operations.context(MigrationContext.configure(connection)):
                module.upgrade()
                self.assertEqual(connection.exec_driver_sql("SELECT display_name, first_name, last_name FROM user_account").one(), ("Anna Maria von der Linden", None, None))
                connection.exec_driver_sql("UPDATE user_account SET first_name='Anna Maria', last_name='von der Linden'")
                module.downgrade()
            self.assertEqual([column["name"] for column in sa.inspect(connection).get_columns("user_account")], ["id", "display_name"])
            self.assertEqual(connection.exec_driver_sql("SELECT display_name FROM user_account").scalar(), "Anna Maria von der Linden")


class DmsInvoiceVersionMigrationTests(unittest.TestCase):
    def test_existing_invoice_archive_jobs_keep_their_ids_and_order(self):
        path = Path(__file__).resolve().parents[1] / "migrations/versions/3e8a1c7d5b2f_dms_invoice_versions.py"
        specification = importlib.util.spec_from_file_location("_dms_invoice_version_migration", path)
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        engine = sa.create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE dms_document (id VARCHAR(36) PRIMARY KEY, entity_type VARCHAR(40), entity_id INTEGER, "
                "document_type VARCHAR(80), created_at DATETIME, status VARCHAR(30), dms_document_id VARCHAR(80), error TEXT)"
            )
            connection.exec_driver_sql(
                "INSERT INTO dms_document (id, entity_type, entity_id, document_type, created_at, status, dms_document_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    ("first", "invoice", 1, "invoice", "2026-11-20", "pending", None),
                    ("original", "invoice", 1, "invoice", "2026-11-21", "archived", "D000000001"),
                    ("next", "invoice", 1, "invoice", "2026-11-22", "pending", None),
                    ("reports", "invoice", 1, "invoice_with_work_reports", "2026-11-22", "archived", "D000000002"),
                ],
            )
            with Operations.context(MigrationContext.configure(connection)):
                module.upgrade()
                self.assertEqual(connection.exec_driver_sql(
                    "SELECT id, invoice_version, status, dms_document_id FROM dms_document ORDER BY id"
                ).all(), [
                    ("first", 1, "cancelled", None), ("next", 3, "pending", None),
                    ("original", 2, "archived", "D000000001"), ("reports", None, "archived", "D000000002"),
                ])
                module.downgrade()
            self.assertNotIn("invoice_version", {column["name"] for column in sa.inspect(connection).get_columns("dms_document")})


if __name__ == "__main__":
    unittest.main()
