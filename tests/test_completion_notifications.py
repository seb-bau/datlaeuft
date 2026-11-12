import importlib
import importlib.util
import json
import os
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
import sqlalchemy as sa
from sqlalchemy.engine import make_url


class CompletionNotificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.db = SQLAlchemy()
        cls.app = Flask(__name__)
        cls.app.config.update(SQLALCHEMY_DATABASE_URI="sqlite://", TESTING=True)
        cls.db.init_app(cls.app)
        cls.package_name = "_completion_notification_test_app"
        package = types.ModuleType(cls.package_name)
        package.__path__ = [str(cls.root / "app")]
        extensions = types.ModuleType(cls.package_name + ".extensions")
        extensions.db = cls.db
        module_patch = patch.dict(sys.modules, {cls.package_name: package, extensions.__name__: extensions})
        module_patch.start()
        cls.addClassCleanup(module_patch.stop)
        cls.models = importlib.import_module(cls.package_name + ".models")
        cls.notifications = importlib.import_module(cls.package_name + ".completion_notifications")
        cls.settings = importlib.import_module(cls.package_name + ".settings")
        cls.templates = importlib.import_module(cls.package_name + ".email_templates")
        cls.tables = [model.__table__ for model in (
            cls.models.AppSetting, cls.models.User, cls.models.Commission,
            cls.models.CommissionPosition, cls.models.CommissionCompletionNotification,
        )]

    def setUp(self):
        context = self.app.app_context()
        context.push()
        self.addCleanup(context.pop)
        self.addCleanup(self.db.session.remove)
        self.db.metadata.drop_all(self.db.engine, tables=self.tables)
        self.db.metadata.create_all(self.db.engine, tables=self.tables)
        self.now = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
        self.creator = self.models.User(email="creator@example.invalid", display_name="Erstellende Sachbearbeitung")
        self.assigned = self.models.User(email="assigned@example.invalid", display_name="Aktuelle Sachbearbeitung")
        self.craftsman = self.models.User(email="craftsman@example.invalid", display_name="Ausführender Handwerker")
        self.db.session.add_all([self.creator, self.assigned, self.craftsman])
        for definition, value in (
            (self.settings.SettingKeys.SMTP_HOST, "smtp.example.invalid"),
            (self.settings.SettingKeys.SMTP_SENDER, "app@example.invalid"),
            (self.settings.SettingKeys.PUBLIC_BASE_URL, "https://app.example.invalid"),
        ):
            self.settings.settings.set(definition, value)
        self.db.session.commit()
        sending = patch.object(self.notifications, "send_email")
        self.send_email = sending.start()
        self.addCleanup(sending.stop)

    def commission(self, **values):
        defaults = {
            "source": "web", "created_by": self.creator, "local_official": self.assigned,
            "user": self.craftsman, "local_status": "completion_review", "status_id": 7,
            "status_code": "Accepted", "erp_idnum": "A-100", "title": "Wasserhahn reparieren",
            "completion_successful": False, "completion_reason": "Ersatzteil fehlt.",
            "completion_submitted_at": self.now, "completion_submitted_by": self.craftsman,
            "notify_on_completion": False,
        }
        defaults.update(values)
        commission = self.models.Commission(**defaults)
        self.db.session.add(commission)
        self.db.session.commit()
        return commission

    def process(self):
        return self.notifications.process_completion_notification_batch()

    def test_incomplete_report_notifies_the_creator_with_reason_and_link_without_opt_in(self):
        commission = self.commission()
        self.assertEqual(self.process(), 1)
        self.send_email.assert_called_once()
        message = self.send_email.call_args.args[0]
        self.assertEqual(str(message["To"]), self.creator.email)
        self.assertNotEqual(str(message["To"]), self.assigned.email)
        self.assertIn("nicht abgeschlossen", str(message["Subject"]))
        body = message.get_body(preferencelist=("plain",)).get_content()
        self.assertIn(commission.completion_reason, body)
        self.assertIn(self.craftsman.display_name, body)
        self.assertIn(f"https://app.example.invalid/admin/commissions/{commission.id}", body)
        item = self.models.CommissionCompletionNotification.query.one()
        self.assertEqual(item.state, "sent")

    def test_incomplete_report_is_not_restricted_to_web_orders_or_successful_erp_sync(self):
        for source, state in (("ios", "pending"), ("wowiport", "failed")):
            self.commission(source=source, erp_sync_state=state)
        self.assertEqual(self.process(), 2)
        self.assertEqual(self.send_email.call_count, 2)

    def test_repeated_worker_rounds_do_not_send_duplicate_notifications(self):
        self.commission()
        self.assertEqual(self.process(), 1)
        self.assertEqual(self.process(), 0)
        self.send_email.assert_called_once()

    def test_a_later_incomplete_report_on_the_same_order_has_its_own_notification(self):
        commission = self.commission()
        self.process()
        commission.completion_submitted_at = self.now + timedelta(days=1)
        commission.completion_reason = "Weiterhin kein Zugang."
        self.db.session.commit()
        self.assertEqual(self.process(), 1)
        self.assertEqual(self.send_email.call_count, 2)
        items = self.models.CommissionCompletionNotification.query.all()
        self.assertEqual(len(items), 2)
        self.assertNotEqual(items[0].notification_key, items[1].notification_key)

    def test_a_successful_completion_can_be_notified_after_an_incomplete_report(self):
        commission = self.commission(erp_id=100)
        self.process()
        commission.local_status = "done"
        commission.status_id = 9
        commission.completion_successful = True
        commission.notify_on_completion = True
        self.db.session.commit()
        self.assertEqual(self.process(), 1)
        self.assertEqual(self.send_email.call_count, 2)
        self.assertIn("abgeschlossen", str(self.send_email.call_args.args[0]["Subject"]))
        self.assertEqual(self.models.CommissionCompletionNotification.query.count(), 2)

    def test_successful_completion_still_requires_opt_in(self):
        self.commission(erp_id=100, local_status="done", status_id=9, completion_successful=True)
        self.assertEqual(self.process(), 0)
        self.send_email.assert_not_called()

    def test_incomplete_report_never_sends_a_success_email_from_a_stale_erp_status(self):
        self.commission(erp_id=100, status_id=9, notify_on_completion=True)
        self.assertEqual(self.process(), 1)
        self.send_email.assert_called_once()
        self.assertIn("nicht abgeschlossen", str(self.send_email.call_args.args[0]["Subject"]))

    def test_failed_mail_is_retried_with_the_existing_notification(self):
        self.commission()
        self.send_email.side_effect = RuntimeError("SMTP nicht erreichbar")
        with self.assertLogs(self.app.logger, level="ERROR"):
            self.assertEqual(self.process(), 1)
        item = self.models.CommissionCompletionNotification.query.one()
        self.assertEqual(item.state, "failed")
        item.available_at = self.now - timedelta(days=1)
        self.db.session.commit()
        self.send_email.side_effect = None
        self.assertEqual(self.process(), 1)
        self.assertEqual(item.state, "sent")
        self.assertEqual(self.models.CommissionCompletionNotification.query.count(), 1)

    def test_missing_creator_address_is_retried_instead_of_notifying_another_official(self):
        self.commission(created_by=None)
        with self.assertLogs(self.app.logger, level="ERROR"):
            self.assertEqual(self.process(), 1)
        self.send_email.assert_not_called()
        item = self.models.CommissionCompletionNotification.query.one()
        self.assertEqual(item.state, "failed")
        self.assertIn("anlegenden Sachbearbeiter", item.last_error)

    def test_superseded_incomplete_notification_is_discarded(self):
        commission = self.commission()
        self.notifications.ensure_completion_notifications()
        self.db.session.commit()
        commission.completion_submitted_at = self.now + timedelta(days=1)
        commission.completion_reason = "Neue Meldung"
        self.db.session.commit()
        self.assertEqual(self.process(), 2)
        self.send_email.assert_called_once()
        self.assertEqual(self.models.CommissionCompletionNotification.query.filter_by(state="discarded").count(), 1)

    def test_notification_limit_advances_past_previously_queued_reports(self):
        self.commission()
        self.commission()
        self.assertEqual(self.notifications.ensure_completion_notifications(limit=1), 1)
        self.db.session.commit()
        self.assertEqual(self.notifications.ensure_completion_notifications(limit=1), 1)
        self.db.session.commit()
        self.assertEqual(self.models.CommissionCompletionNotification.query.count(), 2)

    def test_incomplete_template_is_available_for_preview_and_editing(self):
        content = self.templates.render_email_value(
            "incomplete", self.templates.EMAIL_TEMPLATES["incomplete"], self.templates.EXAMPLE_CONTEXT,
        )
        self.assertIn("nicht abgeschlossen", content["subject"])
        self.assertIn("Ersatzteil fehlt.", content["body"])


class CompletionNotificationMigrationTests(unittest.TestCase):
    def test_upgrade_preserves_successful_delivery_and_allows_distinct_reports(self):
        root = Path(__file__).resolve().parents[1]
        path = root / "migrations/versions/d3f5a7c9e1b2_incomplete_commission_notifications.py"
        spec = importlib.util.spec_from_file_location("incomplete_notification_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        database_url = os.environ.get("DATLAEUFT_TEST_DATABASE_URL", "sqlite://")
        database = make_url(database_url)
        if database.get_backend_name() != "sqlite" and not (database.database or "").startswith("datlaeuft_test_"):
            raise ValueError("Für diese Tests ist eine eigene Testdatenbank erforderlich.")
        engine = sa.create_engine(database_url)
        self.addCleanup(engine.dispose)
        metadata = sa.MetaData()
        sa.Table("commission", metadata, sa.Column("id", sa.Integer, primary_key=True))
        old = sa.Table(
            "commission_completion_notification", metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("commission_id", sa.Integer, sa.ForeignKey("commission.id")),
            sa.Column("state", sa.String(30)),
            sa.Column("payload", sa.JSON),
            sa.UniqueConstraint("commission_id", name="uq_commission_completion_notification"),
        )
        with engine.begin() as connection:
            if connection.dialect.name in {"mysql", "mariadb"}:
                connection.execute(sa.text("SET FOREIGN_KEY_CHECKS=0"))
            metadata.drop_all(connection)
            metadata.create_all(connection)
            if connection.dialect.name in {"mysql", "mariadb"}:
                connection.execute(sa.text("SET FOREIGN_KEY_CHECKS=1"))
            connection.execute(metadata.tables["commission"].insert().values(id=1))
            connection.execute(old.insert().values(id=1, commission_id=1, state="sent", payload={"subject": "Erledigt"}))
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()
            current = sa.Table(old.name, sa.MetaData(), autoload_with=connection)
            item = connection.execute(sa.select(current)).one()
            self.assertEqual(item.notification_key, "completion")
            payload = json.loads(item.payload) if isinstance(item.payload, str) else item.payload
            self.assertEqual(payload, {"subject": "Erledigt"})
            connection.execute(current.insert(), [
                {"commission_id": 1, "notification_key": "incomplete:first", "state": "sent"},
                {"commission_id": 1, "notification_key": "incomplete:second", "state": "pending"},
            ])
            self.assertEqual(connection.scalar(sa.select(sa.func.count()).select_from(current)), 3)
            with self.assertRaises(sa.exc.IntegrityError), connection.begin_nested():
                connection.execute(current.insert().values(commission_id=1, notification_key="incomplete:first"))
            migration.downgrade()
            self.assertEqual(connection.scalar(sa.select(sa.func.count()).select_from(old)), 1)
            columns = {column["name"] for column in sa.inspect(connection).get_columns(old.name)}
            self.assertNotIn("notification_key", columns)
            self.assertNotIn("report_submitted_at", columns)
        self.assertEqual(ScriptDirectory(str(root / "migrations")).get_heads(), [migration.revision])


if __name__ == "__main__":
    unittest.main()
