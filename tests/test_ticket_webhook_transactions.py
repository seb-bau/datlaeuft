import importlib
import os
import sys
import tempfile
import types
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session


class TicketWebhookTransactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.database_url = os.environ.get(
            "DATLAEUFT_TEST_DATABASE_URL",
            "sqlite:///" + str(Path(cls.directory.name) / "tickets.sqlite"),
        )
        database_url = make_url(cls.database_url)
        if database_url.get_backend_name() != "sqlite" and not (
            database_url.database or ""
        ).startswith("datlaeuft_test_"):
            raise ValueError("Für diese Tests ist eine eigene Testdatenbank erforderlich.")
        cls.db = SQLAlchemy()
        cls.app = Flask(__name__)
        cls.app.config["SQLALCHEMY_DATABASE_URI"] = cls.database_url
        cls.db.init_app(cls.app)
        cls.package_name = "_ticket_webhook_test_app"
        package = types.ModuleType(cls.package_name)
        package.__path__ = [str(Path(__file__).resolve().parents[1] / "app")]
        cls.settings = Mock()
        setting_names = (
            "OPENAI_ENABLED", "OPENAI_API_KEY", "OPENAI_MODEL",
            "OPENAI_TIMEOUT_SECONDS", "TICKET_AI_PREFETCH_ENABLED",
        )
        dependencies = {
            "extensions": {"db": cls.db},
            "settings": {
                "settings": cls.settings,
                "SettingKeys": types.SimpleNamespace(**{
                    name: name for name in setting_names
                }),
            },
            "wowiport": {
                "WowiportService": Mock(), "ERP_OPERATION_STATUS": {},
                "mark_commission_deleted": Mock(), "sync_commission_from_erp": Mock(),
            },
            "external_craftsman_service": {
                "available_external_craftsmen": Mock(),
                "process_external_assignment_mail_batch": Mock(),
            },
            "object_directory": {"commission_service_positions": Mock()},
            "openai_service": {
                "TICKET_ANALYSIS_PROMPT_VERSION": "test",
                "analyze_ticket_content": Mock(),
            },
            "document_sync": {"sync_commission_documents": Mock()},
            "push_service": {
                "enqueue_commission_push": Mock(), "process_push_batch": Mock(),
                "requeue_stale_push_deliveries": Mock(),
            },
            "process_info_service": {
                "process_process_info_work": Mock(), "sync_process_infos": Mock(),
            },
            "tenant_notifications": {"process_tenant_notification_batch": Mock()},
            "completion_notifications": {"process_completion_notification_batch": Mock()},
            "dms_service": {"process_dms_batch": Mock()},
        }
        cls.modules = {cls.package_name: package}
        for name, attributes in dependencies.items():
            module = types.ModuleType(cls.package_name + "." + name)
            module.__dict__.update(attributes)
            cls.modules[module.__name__] = module
        cls.module_patch = patch.dict(sys.modules, cls.modules)
        cls.module_patch.start()
        cls.addClassCleanup(cls.module_patch.stop)
        cls.addClassCleanup(cls.directory.cleanup)
        cls.models = importlib.import_module(cls.package_name + ".models")
        cls.ticket_service = importlib.import_module(cls.package_name + ".ticket_service")
        cls.ai_service = importlib.import_module(cls.package_name + ".ticket_ai_service")
        sys.modules["app"] = package
        sys.modules["app.models"] = cls.models
        cls.erp_sync = importlib.import_module(cls.package_name + ".erp_sync")
        cls.tables = [model.__table__ for model in (
            cls.models.TicketMirror, cls.models.TicketAiJob,
            cls.models.WebhookEvent, cls.models.AuditLog,
            cls.models.CommissionTicketLink,
        )]

    def setUp(self):
        self.context = self.app.app_context()
        self.context.push()
        self.addCleanup(self.context.pop)
        self.addCleanup(self.db.session.remove)
        with self.db.engine.begin() as connection:
            if connection.dialect.name in {"mysql", "mariadb"}:
                connection.execute(text("SET FOREIGN_KEY_CHECKS=0"))
            self.db.metadata.drop_all(connection, tables=self.tables)
            self.db.metadata.create_all(connection, tables=self.tables)
            if connection.dialect.name in {"mysql", "mariadb"}:
                connection.execute(text("SET FOREIGN_KEY_CHECKS=1"))
        self.settings.get.side_effect = {
            "OPENAI_ENABLED": True, "OPENAI_API_KEY": "test-key",
            "OPENAI_MODEL": "test-model", "OPENAI_TIMEOUT_SECONDS": 30,
            "TICKET_AI_PREFETCH_ENABLED": True,
        }.get
        self.row = {
            "Id": 12318, "Subject": "Wasserhahn", "Content": "Wasserhahn tropft",
            "TimeReceived": "2026-11-09T17:16:57Z", "Comments": [],
            "Status": {"Id": 1, "Code": "Open"},
            "Priority": {"Id": 1, "Code": "Normal"},
        }
        self.service = Mock()
        self.service.get_ticket_rows.return_value = [self.row]
        self.service.get_priority_ticket_rows.return_value = []
        self.service_patch = patch.object(
            self.erp_sync, "WowiportService", return_value=self.service,
        )
        self.service_patch.start()
        self.addCleanup(self.service_patch.stop)
        ticket = self.models.TicketMirror(
            erp_id=12318, subject=self.row["Subject"],
            source_signature=self.ticket_service.ticket_signature(self.row),
            state=self.models.TicketMirrorState.NEW.value,
            ai_state=self.models.TicketAiState.PROCESSING.value,
        )
        event = self.models.WebhookEvent(
            entity_type="Ticket", entity_id=12318, action="Updated", payload={},
        )
        self.db.session.add_all([ticket, event])
        self.db.session.flush()
        self.ticket_id, self.event_id = ticket.id, event.id
        self.db.session.commit()
        self.db.session.remove()

    def complete_ai(self):
        with Session(self.db.engine) as writer:
            if writer.bind.dialect.name in {"mysql", "mariadb"}:
                writer.execute(text("SET SESSION innodb_lock_wait_timeout=1"))
            ticket = writer.get(self.models.TicketMirror, self.ticket_id)
            ticket.ai_state = self.models.TicketAiState.READY.value
            ticket.ai_title = "KI-Ergebnis erhalten"
            writer.commit()

    def test_ai_completion_during_api_request_does_not_fail_webhook(self):
        def fetch_rows(**kwargs):
            self.complete_ai()
            return [self.row]

        self.service.get_ticket_rows.side_effect = fetch_rows
        self.assertEqual(self.erp_sync.process_webhook_batch(1), 1)
        event = self.db.session.get(self.models.WebhookEvent, self.event_id)
        ticket = self.db.session.get(self.models.TicketMirror, self.ticket_id)
        self.assertTrue(event.processed, event.message)
        self.assertEqual(event.attempt_count, 1)
        self.assertEqual(ticket.ai_state, self.models.TicketAiState.READY.value)
        self.assertEqual(ticket.ai_title, "KI-Ergebnis erhalten")

    def test_changed_source_invalidates_concurrently_completed_analysis(self):
        self.settings.get.side_effect = lambda name: False

        def fetch_rows(**kwargs):
            self.complete_ai()
            row = deepcopy(self.row)
            row["Content"] = "Wasserhahn undicht, zusätzlich Wasserschaden"
            return [row]

        self.service.get_ticket_rows.side_effect = fetch_rows
        self.erp_sync.process_webhook_batch(1)
        ticket = self.db.session.get(self.models.TicketMirror, self.ticket_id)
        event = self.db.session.get(self.models.WebhookEvent, self.event_id)
        self.assertTrue(event.processed, event.message)
        self.assertEqual(ticket.ai_state, self.models.TicketAiState.STALE.value)
        self.assertIn("Wasserschaden", ticket.content_excerpt)

    def test_ticket_lock_is_released_before_priority_api_request(self):
        def fetch_priorities(**kwargs):
            self.complete_ai()
            return []

        self.service.get_priority_ticket_rows.side_effect = fetch_priorities
        self.erp_sync.process_webhook_batch(1)
        event = self.db.session.get(self.models.WebhookEvent, self.event_id)
        self.assertTrue(event.processed, event.message)
        self.service.get_priority_ticket_rows.assert_called_once()

    def test_ai_claim_starts_fresh_after_reading_configuration(self):
        self.db.session.add(self.models.TicketAiJob(
            ticket_id=self.ticket_id, source_signature="test", automatic=True,
        ))
        self.db.session.commit()
        get_setting = self.settings.get.side_effect

        def load_setting(name):
            if name == "OPENAI_TIMEOUT_SECONDS":
                self.db.session.get(self.models.WebhookEvent, self.event_id)
                self.complete_ai()
            return get_setting(name)

        self.settings.get.side_effect = load_setting
        claim = self.ai_service.claim_ticket_ai()
        self.assertIsNotNone(claim)
        self.assertEqual(claim.ticket_id, self.ticket_id)
        ticket = self.db.session.get(self.models.TicketMirror, self.ticket_id)
        self.assertEqual(ticket.ai_state, self.models.TicketAiState.PROCESSING.value)

    def test_new_ticket_and_ai_job_are_saved_together(self):
        ticket = self.db.session.get(self.models.TicketMirror, self.ticket_id)
        self.db.session.delete(ticket)
        self.db.session.commit()
        self.erp_sync.process_webhook_batch(1)
        ticket = self.models.TicketMirror.query.filter_by(erp_id=12318).one()
        job = self.db.session.get(self.models.TicketAiJob, ticket.id)
        event = self.db.session.get(self.models.WebhookEvent, self.event_id)
        self.assertTrue(event.processed, event.message)
        self.assertEqual(ticket.ai_state, self.models.TicketAiState.QUEUED.value)
        self.assertIsNotNone(job)
        self.assertEqual(job.source_signature, ticket.source_signature)

    def test_deleted_ticket_is_processed_without_ticket_api_request(self):
        event = self.db.session.get(self.models.WebhookEvent, self.event_id)
        event.action = "Deleted"
        self.db.session.commit()
        self.erp_sync.process_webhook_batch(1)
        event = self.db.session.get(self.models.WebhookEvent, self.event_id)
        ticket = self.db.session.get(self.models.TicketMirror, self.ticket_id)
        self.assertTrue(event.processed, event.message)
        self.assertIsNotNone(ticket.deleted_at)
        self.service.get_ticket_rows.assert_not_called()
        self.service.get_priority_ticket_rows.assert_not_called()

    def test_priority_failure_keeps_mirror_and_retries_unprocessed_event(self):
        row = deepcopy(self.row)
        row["Subject"] = "Aktualisierter Betreff"
        self.service.get_ticket_rows.return_value = [row]
        self.service.get_priority_ticket_rows.side_effect = RuntimeError("API nicht erreichbar")
        with self.assertLogs(self.app.logger, level="ERROR"):
            self.erp_sync.process_webhook_batch(1)
        event = self.db.session.get(self.models.WebhookEvent, self.event_id)
        ticket = self.db.session.get(self.models.TicketMirror, self.ticket_id)
        job = self.db.session.get(self.models.TicketAiJob, self.ticket_id)
        self.assertFalse(event.processed)
        self.assertIsNotNone(event.next_attempt_at)
        self.assertEqual(ticket.subject, "Aktualisierter Betreff")
        revision = job.revision
        event.next_attempt_at = self.models.utc_now()
        self.db.session.commit()
        self.service.get_priority_ticket_rows.side_effect = None
        self.erp_sync.process_webhook_batch(1)
        self.assertTrue(self.db.session.get(self.models.WebhookEvent, self.event_id).processed)
        self.assertEqual(self.db.session.get(self.models.TicketAiJob, self.ticket_id).revision, revision)

    def test_api_failure_does_not_change_ticket(self):
        self.service.get_ticket_rows.side_effect = RuntimeError("API nicht erreichbar")
        with self.assertLogs(self.app.logger, level="ERROR"):
            self.erp_sync.process_webhook_batch(1)
        event = self.db.session.get(self.models.WebhookEvent, self.event_id)
        ticket = self.db.session.get(self.models.TicketMirror, self.ticket_id)
        self.assertFalse(event.processed)
        self.assertEqual(ticket.ai_state, self.models.TicketAiState.PROCESSING.value)
        self.assertEqual(ticket.subject, "Wasserhahn")
        self.assertEqual(self.models.AuditLog.query.count(), 0)


if __name__ == "__main__":
    unittest.main()
