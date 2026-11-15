import ast
from datetime import date, datetime, time, timedelta, timezone
import importlib
import importlib.util
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token
from flask_sqlalchemy import SQLAlchemy
from icalendar import Calendar
from jinja2 import DictLoader
import sqlalchemy as sa
from sqlalchemy.engine import make_url


class MobileAppointmentFeaturesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.db = SQLAlchemy()
        cls.app = Flask(__name__)
        database_url = os.environ.get("DATLAEUFT_TEST_DATABASE_URL", "sqlite://")
        database = make_url(database_url)
        if database.get_backend_name() != "sqlite" and not (database.database or "").startswith("datlaeuft_test_"):
            raise ValueError("Für diese Tests ist eine eigene Testdatenbank erforderlich.")
        cls.app.config.update(
            SQLALCHEMY_DATABASE_URI=database_url,
            TESTING=True, JWT_SECRET_KEY="mobile-appointment-test-secret-at-least-thirty-two-characters",
        )
        cls.db.init_app(cls.app)
        cls.package_name = "_mobile_appointment_features_test_app"
        package = types.ModuleType(cls.package_name)
        package.__path__ = [str(cls.root / "app")]
        extensions = types.ModuleType(cls.package_name + ".extensions")
        extensions.db = cls.db
        extensions.jwt = JWTManager(cls.app)
        modules = {cls.package_name: package, extensions.__name__: extensions}
        real_modules = {
            "models", "extensions", "settings", "utils", "blacklist", "email_templates", "email_layout",
            "mail_service", "workflow_notifications", "appointment_service", "appointment_calendar", "appointments",
            "commission_object_history", "commission_related", "mobile_transaction", "push_preferences", "api",
        }
        for source_name in real_modules - {"extensions"}:
            source = ast.parse((cls.root / "app" / (source_name + ".py")).read_text())
            for node in ast.walk(source):
                if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module not in real_modules:
                    name = cls.package_name + "." + node.module
                    module = modules.setdefault(name, types.ModuleType(name))
                    for item in node.names:
                        setattr(module, item.name, Mock())
        module_patch = patch.dict(sys.modules, modules)
        module_patch.start()
        cls.addClassCleanup(module_patch.stop)
        for name in ("models", "settings", "appointment_calendar", "workflow_notifications", "appointments", "mobile_transaction", "api"):
            setattr(cls, name, importlib.import_module(cls.package_name + "." + name))
        cls.app.register_blueprint(cls.api.api_bp)
        cls.app.register_blueprint(cls.appointments.appointments_bp)
        cls.app.jinja_loader = DictLoader({"appointments/result.html": "{{ state }}"})
        cls.tables = [model.__table__ for model in (
            cls.models.AppSetting, cls.models.BlacklistEntry, cls.models.User, cls.models.Commission,
            cls.models.CommissionPosition, cls.models.TicketMirror, cls.models.WorkflowNotification,
            cls.models.AppointmentWindow, cls.models.CommissionAppointmentRequest, cls.models.CommissionAppointmentProposal,
        )] + [cls.models.user_commission_visibility]

    def setUp(self):
        context = self.app.app_context()
        context.push()
        self.addCleanup(context.pop)
        self.addCleanup(self.db.session.remove)
        with self.db.engine.begin() as connection:
            if connection.dialect.name in {"mysql", "mariadb"}:
                connection.execute(sa.text("SET FOREIGN_KEY_CHECKS=0"))
            self.db.metadata.drop_all(connection, tables=self.tables)
            self.db.metadata.create_all(connection, tables=self.tables)
            if connection.dialect.name in {"mysql", "mariadb"}:
                connection.execute(sa.text("SET FOREIGN_KEY_CHECKS=1"))
        self.user = self.models.User(email="worker@example.invalid", display_name="Hausmeister Müller", craftsman_erp_id=100)
        self.other_user = self.models.User(email="other@example.invalid", display_name="Kollegin", craftsman_erp_id=101)
        self.db.session.add_all([self.user, self.other_user])
        self.db.session.commit()
        settings = self.settings.settings
        keys = self.settings.SettingKeys
        for definition, value in ((keys.SMTP_HOST, "smtp.example.invalid"), (keys.SMTP_SENDER, "app@example.invalid"), (keys.APPOINTMENT_TIMEZONE, "Europe/Berlin")):
            settings.set(definition, value)
        self.db.session.commit()
        self.client = self.app.test_client()
        self.send = self.start_patch(patch.object(self.workflow_notifications, "send_email", return_value=None))
        self.start_patch(patch.object(self.appointments, "notify_tenant", return_value=True))
        self.start_patch(patch.object(self.appointments, "notify_assigned_user"))
        self.next_id = 100

    def start_patch(self, item):
        result = item.start()
        self.addCleanup(item.stop)
        return result

    def headers(self, user=None):
        return {"Authorization": "Bearer " + create_access_token(identity=str((user or self.user).id))}

    def commission(self, **values):
        self.next_id += 1
        defaults = {
            "erp_id": self.next_id, "erp_idnum": f"A-{self.next_id}", "user": self.user,
            "craftsman_erp_id": self.user.craftsman_erp_id, "title": "Reparatur", "local_status": "in_progress",
            "company_code_erp_id": 1, "company_code_code": "01", "building_erp_id": 1000,
            "building_idnum": "B-1", "use_unit_id": 2000, "use_unit_idnum": "W-1",
            "license_agreement_id": 3000, "license_agreement_idnum": "V-1", "apartment_address": "Teststraße 1",
        }
        item = self.models.Commission(**(defaults | values))
        self.db.session.add(item)
        self.db.session.commit()
        return item

    def manual_appointment(self, commission, **values):
        return self.client.post(f"/api/commissions/{commission.id}/appointments/accepted", headers=self.headers(), json={"date": "2027-01-15", "time": "10:30", **values})

    def process(self):
        self.db.session.commit()
        self.workflow_notifications.process_workflow_notification_batch()

    def calendar(self):
        self.assertIsNotNone(self.send.call_args, [(item.state, item.last_error) for item in self.models.WorkflowNotification.query.all()])
        message = self.send.call_args.args[0]
        attachment = next(message.iter_attachments())
        self.assertEqual(attachment.get_filename(), "termin.ics")
        self.assertEqual(attachment.get_param("method"), "REQUEST")
        calendar = Calendar.from_ical(attachment.get_payload(decode=True))
        self.assertEqual(str(calendar["METHOD"]), "REQUEST")
        return calendar.walk("VEVENT")[0]

    def test_preference_defaults_on_and_is_persisted_per_user(self):
        path = "/api/me/appointment-preferences"
        self.assertEqual(self.client.get(path, headers=self.headers()).json["preferences"], {"calendar_email_enabled": True})
        self.assertEqual(self.client.patch(path, headers=self.headers(), json={"calendar_email_enabled": False}).status_code, 200)
        self.db.session.expire_all()
        self.assertFalse(self.client.get(path, headers=self.headers()).json["preferences"]["calendar_email_enabled"])
        self.assertTrue(self.client.get(path, headers=self.headers(self.other_user)).json["preferences"]["calendar_email_enabled"])
        self.assertEqual(self.client.get(path).status_code, 401)
        for value in (None, "true", 1, {}, []):
            self.assertEqual(self.client.patch(path, headers=self.headers(), json={"calendar_email_enabled": value}).status_code, 400)

    def test_manual_appointment_creates_one_timed_invitation_after_commit(self):
        commission = self.commission()
        self.assertEqual(self.manual_appointment(commission).status_code, 201)
        self.send.assert_not_called()
        self.assertEqual(self.manual_appointment(commission).status_code, 409)
        self.process()
        event = self.calendar()
        self.assertEqual(str(event["ATTENDEE"]), "mailto:worker@example.invalid")
        self.assertEqual(event.decoded("DTSTART"), datetime(2027, 1, 15, 9, 30, tzinfo=timezone.utc))
        self.assertEqual(event.decoded("DTEND") - event.decoded("DTSTART"), timedelta(minutes=60))
        self.process()
        self.assertEqual(self.send.call_count, 1)

    def test_date_only_manual_appointment_uses_an_all_day_event(self):
        commission = self.commission()
        self.assertEqual(self.manual_appointment(commission, time=None).status_code, 201)
        self.process()
        event = self.calendar()
        self.assertEqual(event.decoded("DTSTART"), date(2027, 1, 15))
        self.assertEqual(event.decoded("DTEND"), date(2027, 1, 16))

    def test_tenant_acceptance_preserves_local_timezone_and_does_not_duplicate(self):
        commission = self.commission()
        token = "test-tenant-token"
        appointment = self.models.CommissionAppointmentRequest(
            commission=commission, recipient_email="tenant@example.invalid", token_hash=self.appointments.public_token_hash(token),
            expires_at=self.models.utc_now() + timedelta(days=1),
        )
        proposal = self.models.CommissionAppointmentProposal(
            appointment_request=appointment, proposed_date=date(2027, 6, 10), start_time=time(10), end_time=time(12), window_name="Vormittag",
        )
        self.db.session.add_all([appointment, proposal])
        self.db.session.commit()
        for _ in range(2):
            response = self.client.post(f"/termine/{token}", data={"action": "accept", "proposal_id": proposal.id})
            self.assertEqual(response.status_code, 200)
        self.process()
        event = self.calendar()
        self.assertEqual(event.decoded("DTSTART"), datetime(2027, 6, 10, 8, tzinfo=timezone.utc))
        self.assertEqual(event.decoded("DTEND"), datetime(2027, 6, 10, 10, tzinfo=timezone.utc))
        self.assertEqual(self.send.call_count, 1)

    def test_disabled_preference_does_not_queue_and_enabling_does_not_send_old_appointments(self):
        self.user.appointment_calendar_email_enabled = False
        commission = self.commission()
        self.assertEqual(self.manual_appointment(commission).status_code, 201)
        self.assertEqual(self.models.WorkflowNotification.query.count(), 0)
        self.user.appointment_calendar_email_enabled = True
        self.process()
        self.send.assert_not_called()

    def test_pending_invitation_is_discarded_when_disabled_or_appointment_cancelled(self):
        for disabled in (True, False):
            commission = self.commission()
            self.user.appointment_calendar_email_enabled = True
            self.db.session.commit()
            self.assertEqual(self.manual_appointment(commission).status_code, 201)
            if disabled:
                self.user.appointment_calendar_email_enabled = False
            else:
                commission.accepted_appointment_from = None
            self.process()
        self.send.assert_not_called()
        self.assertEqual({item.state for item in self.models.WorkflowNotification.query.all()}, {"discarded"})

    def test_mobile_transaction_rolls_back_invitation_together_with_appointment(self):
        commission = self.commission()
        token = self.mobile_transaction.current_mobile_transaction.set({"user_id": self.user.id, "operation_id": "rollback-test"})
        try:
            self.assertEqual(self.manual_appointment(commission).status_code, 201)
            self.assertEqual(self.models.WorkflowNotification.query.count(), 1)
            self.db.session.rollback()
        finally:
            self.mobile_transaction.current_mobile_transaction.reset(token)
        self.assertEqual(self.models.WorkflowNotification.query.count(), 0)
        self.assertIsNone(self.db.session.get(self.models.Commission, commission.id).accepted_appointment_from)

    def test_related_orders_are_classified_without_duplicates_and_open_read_only(self):
        anchor = self.commission()
        contract = self.commission(user=self.other_user)
        unit = self.commission(user=self.other_user, license_agreement_id=3001, license_agreement_idnum="V-2")
        building = self.commission(user=self.other_user, license_agreement_id=None, license_agreement_idnum=None, use_unit_id=None, use_unit_idnum=None)
        unrelated = self.commission(building_erp_id=1001, building_idnum="B-2", use_unit_id=2001, use_unit_idnum="W-2", license_agreement_id=3002, license_agreement_idnum="V-3")
        result = self.client.get(f"/api/commissions/{anchor.id}/related", headers=self.headers())
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["total"], 3)
        self.assertEqual(result.json["counts"], {"license_agreement": 1, "use_unit": 1, "building": 1})
        self.assertEqual({item["commission"]["id"]: item["relationship"] for item in result.json["items"]}, {contract.id: "license_agreement", unit.id: "use_unit", building.id: "building"})
        for item in result.json["items"]:
            self.assertFalse(item["direct_access"])
            self.assertFalse(item["commission"]["can_edit"])
        self.assertEqual(self.client.get(f"/api/commissions/{anchor.id}/related/{contract.id}", headers=self.headers()).status_code, 200)
        self.assertEqual(self.client.get(f"/api/commissions/{anchor.id}/related/{unrelated.id}", headers=self.headers()).status_code, 404)
        self.assertEqual(self.client.post(f"/api/commissions/{contract.id}/start", headers=self.headers()).status_code, 403)
        self.assertEqual(self.client.get(f"/api/commissions/{contract.id}/related", headers=self.headers()).status_code, 403)

    def test_related_orders_hide_pending_deleted_and_unpublished_orders(self):
        anchor = self.commission()
        for values in ({"approval_state": "pending"}, {"deleted_at": self.models.utc_now()}, {"local_status": "deleted"}, {"erp_idnum": None}):
            hidden = self.commission(**values)
            self.assertEqual(self.client.get(f"/api/commissions/{anchor.id}/related/{hidden.id}", headers=self.headers()).status_code, 404)
        result = self.client.get(f"/api/commissions/{anchor.id}/related", headers=self.headers()).json
        self.assertEqual(result["total"], 0)

    def test_number_fallback_is_company_scoped_and_does_not_match_conflicting_ids(self):
        anchor = self.commission(building_erp_id=None, use_unit_id=None, license_agreement_id=None)
        same = self.commission()
        self.commission(company_code_erp_id=2, company_code_code="02")
        result = self.client.get(f"/api/commissions/{anchor.id}/related", headers=self.headers()).json
        self.assertEqual([item["commission"]["id"] for item in result["items"]], [same.id])
        anchor.building_erp_id, anchor.use_unit_id, anchor.license_agreement_id = 9997, 9998, 9999
        self.db.session.commit()
        self.assertEqual(self.client.get(f"/api/commissions/{anchor.id}/related", headers=self.headers()).json["total"], 0)

    def test_related_pagination_keeps_open_orders_first(self):
        anchor = self.commission()
        for _ in range(21):
            self.commission(local_status="done", completed_at=self.models.utc_now())
        open_order = self.commission()
        result = self.client.get(f"/api/commissions/{anchor.id}/related", headers=self.headers()).json
        self.assertEqual(result["total"], 22)
        self.assertEqual(result["pages"], 2)
        self.assertEqual(len(result["items"]), 20)
        self.assertEqual(result["items"][0]["commission"]["id"], open_order.id)
        second = self.client.get(f"/api/commissions/{anchor.id}/related?page=2", headers=self.headers()).json
        self.assertEqual(len(second["items"]), 2)


class AppointmentCalendarMigrationTests(unittest.TestCase):
    def test_migration_preserves_users_defaults_on_and_can_resume(self):
        root = Path(__file__).resolve().parents[1]
        path = root / "migrations/versions/b4d6f8a0c2e4_appointment_calendar_email.py"
        spec = importlib.util.spec_from_file_location("appointment_calendar_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite://")
        with engine.begin() as connection:
            connection.execute(sa.text("CREATE TABLE user_account (id INTEGER PRIMARY KEY, email VARCHAR(255))"))
            connection.execute(sa.text("INSERT INTO user_account VALUES (1, 'worker@example.invalid')"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                connection.execute(sa.text("UPDATE user_account SET appointment_calendar_email_enabled = 0 WHERE id = 1"))
                migration.upgrade()
                connection.execute(sa.text("INSERT INTO user_account (id, email) VALUES (2, 'new@example.invalid')"))
                self.assertEqual(connection.execute(sa.text("SELECT appointment_calendar_email_enabled FROM user_account ORDER BY id")).scalars().all(), [0, 1])
                migration.downgrade()
                self.assertEqual(connection.execute(sa.text("SELECT COUNT(*) FROM user_account")).scalar(), 2)
        self.assertEqual(ScriptDirectory(str(root / "migrations")).get_heads(), [migration.revision])
