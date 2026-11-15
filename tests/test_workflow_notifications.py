import ast
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import types
import unittest
from datetime import timedelta
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from flask import Flask, render_template
from flask_jwt_extended import JWTManager, create_access_token
from flask_login import LoginManager, login_required
from flask_sqlalchemy import SQLAlchemy
from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader
import sqlalchemy as sa
from sqlalchemy.engine import make_url


class WorkflowNotificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.db = SQLAlchemy()
        cls.app = Flask(__name__, template_folder=str(cls.root / "app/templates"))
        cls.app.config.update(SQLALCHEMY_DATABASE_URI="sqlite://", TESTING=True, SECRET_KEY="workflow-test", JWT_SECRET_KEY="workflow-test-secret-with-at-least-thirty-two-characters")
        cls.db.init_app(cls.app)
        cls.package_name = "_workflow_notification_test_app"
        package = types.ModuleType(cls.package_name)
        package.__path__ = [str(cls.root / "app")]
        extensions = types.ModuleType(cls.package_name + ".extensions")
        extensions.db = cls.db
        extensions.jwt = JWTManager(cls.app)
        modules = {cls.package_name: package, extensions.__name__: extensions}
        real_modules = {
            "models", "extensions", "settings", "utils", "blacklist", "email_templates", "email_layout",
            "mail_service", "workflow_notifications", "commission_links", "push_preferences", "ticket_replies",
            "tenant_notifications", "tenant_sms_notifications", "sms_service", "completion_notifications",
            "push_service", "external_craftsman_service", "admin", "api",
        }
        for source_name in real_modules - {"extensions"}:
            source = ast.parse((cls.root / "app" / (source_name + ".py")).read_text())
            for node in ast.walk(source):
                if not isinstance(node, ast.ImportFrom) or node.level != 1 or node.module in real_modules:
                    continue
                name = cls.package_name + "." + node.module
                module = modules.setdefault(name, types.ModuleType(name))
                for item in node.names:
                    setattr(module, item.name, login_required if node.module == "auth" else Mock())
        ticket_service = modules[cls.package_name + ".ticket_service"]
        ticket_service.value = lambda data, name: next((value for key, value in data.items() if key.casefold() == name.casefold()), None) if isinstance(data, dict) else None
        ticket_service.ticket_license_agreement_local_id.return_value = None
        cls.stub_modules = modules
        module_patch = patch.dict(sys.modules, modules)
        module_patch.start()
        cls.addClassCleanup(module_patch.stop)
        cls.models = importlib.import_module(cls.package_name + ".models")
        cls.settings_module = importlib.import_module(cls.package_name + ".settings")
        cls.notifications = importlib.import_module(cls.package_name + ".workflow_notifications")
        cls.preferences = importlib.import_module(cls.package_name + ".push_preferences")
        cls.links = importlib.import_module(cls.package_name + ".commission_links")
        cls.replies = importlib.import_module(cls.package_name + ".ticket_replies")
        cls.push = importlib.import_module(cls.package_name + ".push_service")
        cls.tenant_mail = importlib.import_module(cls.package_name + ".tenant_notifications")
        cls.tenant_sms = importlib.import_module(cls.package_name + ".tenant_sms_notifications")
        cls.completion = importlib.import_module(cls.package_name + ".completion_notifications")
        cls.external = importlib.import_module(cls.package_name + ".external_craftsman_service")
        cls.admin = importlib.import_module(cls.package_name + ".admin")
        cls.api = importlib.import_module(cls.package_name + ".api")
        cls.app.register_blueprint(cls.admin.admin_bp)
        cls.app.register_blueprint(cls.api.api_bp)
        cls.app.jinja_loader = ChoiceLoader([
            DictLoader({"base.html": "{% block content %}{% endblock %}"}),
            FileSystemLoader(str(cls.root / "app/templates")),
        ])
        cls.app.jinja_env.globals["csrf_token"] = lambda: "test-token"
        manager = LoginManager(cls.app)
        manager.user_loader(lambda identity: cls.db.session.get(cls.models.User, int(identity)))
        cls.tables = [model.__table__ for model in (
            cls.models.AppSetting, cls.models.BlacklistEntry, cls.models.User,
            cls.models.WowiResponsibleOfficial, cls.models.WowiEconomicUnit, cls.models.WowiEconomicUnitJurisdiction,
            cls.models.ExternalCraftsman, cls.models.Commission, cls.models.CommissionPosition,
            cls.models.TicketMirror, cls.models.CommissionTicketLink, cls.models.WorkflowNotification,
            cls.models.PushDevice, cls.models.PushDelivery, cls.models.CommissionTenantNotification,
            cls.models.CommissionTenantSmsNotification, cls.models.CommissionCompletionNotification,
            cls.models.ExternalCommissionAssignmentMail, cls.models.WowiLicenseAgreement,
        )]

    def setUp(self):
        context = self.app.app_context()
        context.push()
        self.addCleanup(context.pop)
        self.addCleanup(self.db.session.remove)
        self.db.metadata.drop_all(self.db.engine, tables=self.tables)
        self.db.metadata.create_all(self.db.engine, tables=self.tables)
        self.settings = self.settings_module.settings
        self.keys = self.settings_module.SettingKeys
        self.office = self.models.User(email="office@example.invalid", display_name="Sachbearbeitung", is_admin=True, is_foreman=True)
        self.craftsman = self.models.User(email="craftsman@example.invalid", display_name="Handwerker", craftsman_erp_id=200)
        self.db.session.add_all([self.office, self.craftsman])
        for definition, value in (
            (self.keys.SMTP_HOST, "smtp.example.invalid"), (self.keys.SMTP_SENDER, "app@example.invalid"),
            (self.keys.PUBLIC_BASE_URL, "https://app.example.invalid"), (self.keys.WOWI_HOSTNAME, "wbg-luenen.wowiport.de"),
        ):
            self.settings.set(definition, value)
        self.db.session.add(self.models.AppSetting(
            key="commissions.tenant_notifications_start_at", value_text="2020-01-01T00:00:00+00:00",
            value_type="string", label="Beginn", group="Test", is_secret=False,
        ))
        self.db.session.commit()
        self.send = self.start_patch(patch.object(self.notifications, "send_email", return_value=None))
        self.tenant_send = self.start_patch(patch.object(self.tenant_mail, "send_email"))
        self.sms_send = self.start_patch(patch.object(self.tenant_sms, "send_sms"))
        self.completion_send = self.start_patch(patch.object(self.completion, "send_email"))
        self.external_send = self.start_patch(patch.object(self.external, "send_external_assignment_email"))
        self.http_post = self.start_patch(patch.object(self.push.requests, "post"))
        self.service = Mock()
        self.stub_modules[self.package_name + ".wowiport"].WowiportService.return_value = self.service
        self.ticket_rows = self.stub_modules[self.package_name + ".ticket_service"].fetch_ticket_row
        self.ticket_rows.return_value = {"Id": 501, "Source": {"Id": 4}, "Comments": []}
        self.service.client.create_ticket_comment.return_value = types.SimpleNamespace(status_code=201, data={"CommentId": 801})
        self.next_id = 100
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["_user_id"] = str(self.office.id)
            session["_fresh"] = True

    def start_patch(self, item):
        result = item.start()
        self.addCleanup(item.stop)
        return result

    def commission(self, **values):
        self.next_id += 1
        defaults = {
            "source": "web", "erp_id": self.next_id, "erp_idnum": f"A-{self.next_id}",
            "created_by": self.office, "user": self.craftsman, "craftsman_erp_id": 200,
            "erp_sync_state": "synced", "local_status": "in_progress", "status_id": 7,
            "execution_to": self.models.utc_now() - timedelta(minutes=1), "title": "Reparatur",
        }
        defaults.update(values)
        item = self.models.Commission(**defaults)
        self.db.session.add(item)
        self.db.session.commit()
        return item

    def ticket(self, **values):
        values = {"erp_id": 501, "id_num": "12345", "subject": "Meldung", "state": "new", **values}
        item = self.models.TicketMirror(**values)
        self.db.session.add(item)
        self.db.session.commit()
        return item

    def test_overdue_orders_enter_review_without_changing_their_work_status_and_notify_both_roles_once(self):
        commission = self.commission()
        self.assertTrue(self.notifications.commission_is_overdue(commission))
        with self.app.test_request_context():
            from flask_login import login_user
            login_user(self.office)
            self.assertIn(commission, self.admin.commission_review_query().all())
        self.assertEqual(self.notifications.process_workflow_notification_batch(), 2)
        self.assertEqual({str(call.args[0]["To"]) for call in self.send.call_args_list}, {"office@example.invalid", "craftsman@example.invalid"})
        self.assertEqual(commission.local_status, "in_progress")
        self.assertEqual(self.notifications.process_workflow_notification_batch(), 0)
        self.assertEqual(self.send.call_count, 2)

    def test_completed_future_deleted_and_retrospective_orders_are_not_overdue(self):
        for values in (
            {"execution_to": self.models.utc_now() + timedelta(minutes=5)}, {"execution_to": None},
            {"local_status": "done", "completed_at": self.models.utc_now()}, {"is_retrospective": True},
            {"deleted_at": self.models.utc_now()}, {"erp_id": None}, {"local_status": "awaiting_invoice"},
            {"status_id": 9},
        ):
            self.assertFalse(self.notifications.commission_is_overdue(self.commission(**values)))
        self.assertEqual(self.notifications.process_workflow_notification_batch(), 0)
        self.send.assert_not_called()

    def test_a_changed_deadline_discards_the_old_notice_and_can_notify_for_the_new_deadline(self):
        commission = self.commission()
        self.notifications.ensure_overdue_notifications()
        self.db.session.commit()
        commission.execution_to = self.models.utc_now() + timedelta(days=1)
        self.db.session.commit()
        self.notifications.process_workflow_notification_batch()
        self.send.assert_not_called()
        self.assertEqual({item.state for item in self.models.WorkflowNotification.query.all()}, {"discarded"})
        commission.execution_to = self.models.utc_now() - timedelta(minutes=2)
        self.db.session.commit()
        self.assertEqual(self.notifications.process_workflow_notification_batch(), 2)
        self.assertEqual(self.send.call_count, 2)

    def test_overdue_batch_limit_does_not_starve_later_orders(self):
        first = self.commission()
        second = self.commission()
        self.assertEqual(self.notifications.ensure_overdue_notifications(limit=1), 2)
        self.db.session.commit()
        self.assertEqual(self.notifications.ensure_overdue_notifications(limit=1), 2)
        self.db.session.commit()
        self.assertEqual({item.commission_id for item in self.models.WorkflowNotification.query.all()}, {first.id, second.id})

    def test_an_overdue_order_with_the_same_author_and_craftsman_gets_one_email(self):
        self.commission(created_by=self.craftsman)
        self.assertEqual(self.notifications.process_workflow_notification_batch(), 1)
        self.send.assert_called_once()

    def test_high_priority_email_is_independent_of_push_opt_out_and_is_deduplicated(self):
        commission = self.commission(is_high_priority=True, execution_to=None)
        self.craftsman.push_event_preferences = {"commission_priority_raised": False}
        self.db.session.commit()
        for _ in range(2):
            self.push.enqueue_commission_push(commission, self.models.PushEventType.NEW_COMMISSION)
            self.db.session.commit()
        self.assertEqual(self.models.WorkflowNotification.query.count(), 1)
        self.assertEqual(self.models.PushDelivery.query.count(), 0)
        self.assertEqual(self.notifications.process_workflow_notification_batch(), 1)
        self.send.assert_called_once()
        self.assertIn("Hohe Priorität", str(self.send.call_args.args[0]["Subject"]))

    def test_changed_high_priority_orders_queue_a_new_email_only_when_the_snapshot_changes(self):
        commission = self.commission(is_high_priority=True, execution_to=None, raw_payload={"sync_signature": "first"})
        event = self.models.PushEventType.CHANGED_COMMISSION
        self.notifications.enqueue_priority_notification(commission, event)
        self.notifications.enqueue_priority_notification(commission, event)
        commission.raw_payload = {"sync_signature": "second"}
        self.notifications.enqueue_priority_notification(commission, event)
        self.db.session.commit()
        self.assertEqual(self.models.WorkflowNotification.query.count(), 2)

    def test_external_high_priority_orders_receive_email_without_a_push_device(self):
        external = self.models.ExternalCraftsman(craftsman_erp_id=300, display_name="Externe Firma", email="external@example.invalid", wowi_active=True)
        self.db.session.add(external)
        commission = self.commission(user=None, craftsman_erp_id=300, assignment_type="external", is_high_priority=True, execution_to=None)
        self.push.enqueue_commission_push(commission, self.models.PushEventType.NEW_COMMISSION)
        self.db.session.commit()
        self.notifications.process_workflow_notification_batch()
        self.assertEqual(str(self.send.call_args.args[0]["To"]), "external@example.invalid")

    def test_a_priority_email_waits_for_the_erp_number_and_a_reassigned_notice_is_discarded(self):
        commission = self.commission(is_high_priority=True, execution_to=None, erp_id=None, erp_idnum=None, erp_sync_state="pending")
        item = self.notifications.enqueue_priority_notification(commission, self.models.PushEventType.NEW_COMMISSION)
        self.db.session.commit()
        with self.assertLogs(self.app.logger, level="ERROR"):
            self.notifications.process_workflow_notification_batch()
        self.send.assert_not_called()
        self.assertEqual(item.state, "failed")
        commission.erp_id = 500
        commission.erp_idnum = "A-500"
        commission.erp_sync_state = "synced"
        commission.craftsman_erp_id = 999
        item.available_at = self.models.utc_now()
        self.db.session.commit()
        self.notifications.process_workflow_notification_batch()
        self.assertEqual(item.state, "discarded")
        self.send.assert_not_called()

    def test_retrospective_orders_cannot_queue_or_send_any_creation_notifications(self):
        commission = self.commission(is_retrospective=True, is_high_priority=True, notify_tenants=True, notify_on_completion=True, license_agreement_id=123)
        self.settings.set(self.keys.SMS_ENABLED, True)
        self.db.session.commit()
        self.assertEqual(self.push.enqueue_commission_push(commission, self.models.PushEventType.NEW_COMMISSION).queued_count, 0)
        self.assertIsNone(self.notifications.enqueue_priority_notification(commission, self.models.PushEventType.NEW_COMMISSION))
        self.assertEqual(self.tenant_mail.ensure_tenant_notifications(), 0)
        self.assertEqual(self.external.ensure_external_assignment_mails(), 0)
        self.assertEqual(self.notifications.process_workflow_notification_batch(), 0)
        for model, execute in (
            (self.models.CommissionTenantNotification, self.tenant_mail.execute_tenant_notification),
            (self.models.CommissionTenantSmsNotification, self.tenant_sms.execute_tenant_sms_notification),
            (self.models.CommissionCompletionNotification, self.completion.execute_completion_notification),
        ):
            item = model(commission=commission, state="sending")
            self.db.session.add(item)
            self.db.session.commit()
            execute(item.id)
            self.assertEqual(item.state, "discarded")
        for sender in (self.send, self.tenant_send, self.sms_send, self.completion_send, self.external_send, self.http_post):
            sender.assert_not_called()

    def test_ticket_reply_is_queued_atomically_and_double_submission_does_not_duplicate_it(self):
        ticket = self.ticket()
        for _ in range(2):
            response = self.client.post(f"/admin/tickets/{ticket.id}/ignore-with-reply", data={
                "recipient": "tenant@example.invalid", "response_text": "Bitte wenden Sie sich an den zuständigen Versorger.",
            })
            self.assertEqual(response.status_code, 302)
        self.assertEqual(ticket.state, "ignored")
        self.assertEqual(self.models.WorkflowNotification.query.count(), 1)
        self.notifications.process_workflow_notification_batch()
        item = self.models.WorkflowNotification.query.one()
        self.assertEqual(item.state, "sent")
        self.assertEqual(item.payload["comment_id"], 801)
        self.send.assert_called_once()
        self.assertIn("Bitte wenden", self.service.client.create_ticket_comment.call_args.kwargs["content"])

    def test_ticket_comment_retry_does_not_send_a_confirmed_email_again(self):
        ticket = self.ticket()
        item = self.replies.enqueue_ticket_reply(ticket, "tenant@example.invalid", "Kein Auftrag erforderlich.")
        self.db.session.commit()
        self.service.client.create_ticket_comment.side_effect = [RuntimeError("Wowiport nicht erreichbar"), types.SimpleNamespace(status_code=201, data={"CommentId": 802})]
        with self.assertLogs(self.app.logger, level="ERROR"):
            self.notifications.process_workflow_notification_batch()
        self.assertEqual(item.state, "failed")
        self.assertTrue(item.payload["mail_sent"])
        item.available_at = self.models.utc_now()
        self.db.session.commit()
        self.notifications.process_workflow_notification_batch()
        self.assertEqual(item.state, "sent")
        self.send.assert_called_once()

    def test_an_existing_ticket_comment_is_reused_after_an_interrupted_writeback(self):
        ticket = self.ticket()
        item = self.replies.enqueue_ticket_reply(ticket, "tenant@example.invalid", "Kein Auftrag erforderlich.")
        self.db.session.commit()
        self.ticket_rows.return_value["Comments"] = [{"Id": 803, "CommentFromApi": True, "Content": f"Antwort [datlaeuft-ticket-reply:{item.id}]"}]
        self.notifications.process_workflow_notification_batch()
        self.assertEqual(item.payload["comment_id"], 803)
        self.service.client.create_ticket_comment.assert_not_called()

    def test_ticket_replies_validate_recipient_and_text_before_changing_ticket_state(self):
        ticket = self.ticket()
        for recipient, text in (("", "Antwort"), ("tenant@example.invalid\nBcc: attacker@example.invalid", "Antwort"), ("tenant@example.invalid", "")):
            with self.assertRaises(ValueError):
                self.replies.enqueue_ticket_reply(ticket, recipient, text)
            self.assertEqual(ticket.state, "new")
        self.assertEqual(self.models.WorkflowNotification.query.count(), 0)
        ticket.state = "converted"
        with self.assertRaises(ValueError):
            self.replies.enqueue_ticket_reply(ticket, "tenant@example.invalid", "Antwort")

    def test_recipient_lookup_prefers_an_explicit_sender_and_never_guesses_from_body_text(self):
        ticket = self.ticket()
        self.ticket_rows.return_value = {"Id": 501, "ReplyTo": "Mieter <tenant@example.invalid>", "Content": "other@example.invalid"}
        self.assertEqual(self.replies.ticket_reply_recipients(ticket), ["tenant@example.invalid"])
        self.ticket_rows.return_value = {"Id": 501, "Content": "other@example.invalid"}
        self.assertEqual(self.replies.ticket_reply_recipients(ticket), [])

    def test_unknown_smtp_delivery_is_not_automatically_replayed(self):
        commission = self.commission(is_high_priority=True, execution_to=None)
        item = self.notifications.enqueue_priority_notification(commission, self.models.PushEventType.NEW_COMMISSION)
        self.db.session.commit()
        self.send.side_effect = RuntimeError("Verbindung nach Übergabe unterbrochen")
        with self.assertLogs(self.app.logger, level="ERROR"):
            self.notifications.process_workflow_notification_batch()
        self.assertEqual(item.state, "uncertain")
        self.assertEqual(self.notifications.process_workflow_notification_batch(), 0)
        self.send.assert_called_once()

    def test_missing_smtp_configuration_can_be_fixed_without_losing_the_notification(self):
        commission = self.commission(is_high_priority=True, execution_to=None)
        item = self.notifications.enqueue_priority_notification(commission, self.models.PushEventType.NEW_COMMISSION)
        self.settings.set(self.keys.SMTP_HOST, "")
        self.db.session.commit()
        with self.assertLogs(self.app.logger, level="ERROR"):
            self.notifications.process_workflow_notification_batch()
        self.assertEqual(item.state, "failed")
        self.send.assert_not_called()
        self.settings.set(self.keys.SMTP_HOST, "smtp.example.invalid")
        item.available_at = self.models.utc_now()
        self.db.session.commit()
        self.notifications.process_workflow_notification_batch()
        self.assertEqual(item.state, "sent")
        self.send.assert_called_once()

    def test_queued_external_assignment_and_push_are_discarded_for_retrospective_orders(self):
        external = self.models.ExternalCraftsman(craftsman_erp_id=300, display_name="Externe Firma", email="external@example.invalid", portal_enabled=True, wowi_active=True)
        self.db.session.add(external)
        commission = self.commission(user=None, craftsman_erp_id=300, assignment_type="external", is_retrospective=True)
        item = self.models.ExternalCommissionAssignmentMail(commission=commission, external_craftsman=external, assignment_version=0, state="sending")
        self.db.session.add(item)
        self.db.session.commit()
        self.assertEqual(self.external.ensure_external_assignment_mails(), 0)
        self.external.execute_external_assignment_mail(item.id)
        self.assertEqual(item.state, "discarded")
        self.external_send.assert_not_called()
        device = self.models.PushDevice(user=self.craftsman, installation_id="22222222-2222-2222-2222-222222222222", device_token="b" * 64)
        delivery = self.models.PushDelivery(
            device=device, user=self.craftsman, commission=commission, batch_id="33333333-3333-3333-3333-333333333333",
            device_token="b" * 64, event_type="new_commission", title="Auftrag", body="Auftrag", payload={}, state="pending",
        )
        self.db.session.add_all([device, delivery])
        self.db.session.commit()
        self.push.process_push_batch()
        self.assertEqual(delivery.state, "discarded")
        self.http_post.assert_not_called()

    def test_a_notice_for_the_previous_craftsman_is_discarded_before_sending(self):
        commission = self.commission()
        self.notifications.ensure_overdue_notifications()
        self.db.session.commit()
        commission.craftsman_erp_id = 999
        self.db.session.commit()
        self.notifications.process_workflow_notification_batch()
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(str(self.send.call_args.args[0]["To"]), "office@example.invalid")

    def test_global_push_defaults_apply_until_a_user_or_admin_overrides_them(self):
        event = self.models.PushEventType.NEW_COMMISSION
        self.settings.set(self.keys.PUSH_DEFAULT_NEW_COMMISSION, False)
        self.db.session.commit()
        self.assertFalse(self.preferences.is_push_event_enabled(self.craftsman, event))
        self.craftsman.push_event_preferences = {event.value: True}
        self.assertTrue(self.preferences.is_push_event_enabled(self.craftsman, event))
        rows = self.preferences.admin_push_preference_rows(self.craftsman)
        self.assertEqual(next(row for row in rows if row["event"] == event.value)["mode"], "on")
        self.preferences.save_admin_push_preferences(self.craftsman, {"push_preferences_present": "1", "push_event_new_commission": "default"})
        self.assertFalse(self.preferences.is_push_event_enabled(self.craftsman, event))
        self.settings.set(self.keys.PUSH_DEFAULT_NEW_COMMISSION, True)
        self.assertTrue(self.preferences.is_push_event_enabled(self.craftsman, event))

    def test_legacy_push_opt_out_is_preserved_and_admin_reset_is_explicit(self):
        self.craftsman.push_changed_commission_enabled = False
        self.assertFalse(self.preferences.is_push_event_enabled(self.craftsman, self.models.PushEventType.APPOINTMENT_ACCEPTED))
        data = {"push_preferences_present": "1", **{"push_event_" + row["event"]: row["mode"] for row in self.preferences.admin_push_preference_rows(self.craftsman)}}
        data["push_event_appointment_accepted"] = "default"
        self.preferences.save_admin_push_preferences(self.craftsman, data)
        self.assertTrue(self.preferences.is_push_event_enabled(self.craftsman, self.models.PushEventType.APPOINTMENT_ACCEPTED))
        self.assertFalse(self.preferences.is_push_event_enabled(self.craftsman, self.models.PushEventType.APPOINTMENT_REJECTED))

    def test_wowiport_link_uses_the_internal_id_and_root_hostname(self):
        commission = self.commission(erp_id=51489, erp_idnum="01-2026-12345")
        for hostname in ("wbg-luenen.wowiport.de", "https://wbg-luenen.wowiport.de/openwowi/"):
            self.settings.set(self.keys.WOWI_HOSTNAME, hostname)
            link = self.links.commission_wowiport_url(commission)
            parsed = urlsplit(link)
            self.assertEqual(parsed.netloc, "wbg-luenen.wowiport.de")
            self.assertEqual(parsed.path, "/EntityOpen")
            self.assertEqual(parse_qs(parsed.query), {"entityName": ["Commission"], "id": ["51489"]})
        commission.erp_idnum = None
        self.assertIsNone(self.links.commission_wowiport_url(commission))

    def test_push_api_can_reset_an_override_to_the_global_default(self):
        self.settings.set(self.keys.PUSH_DEFAULT_NEW_COMMISSION, False)
        self.craftsman.push_event_preferences = {"new_commission": True, "new_process_info": False}
        self.db.session.commit()
        token = create_access_token(identity=str(self.craftsman.id))
        response = self.client.patch("/api/push/preferences", json={"events": {"new_commission": None}}, headers={"Authorization": "Bearer " + token})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertFalse(response.json["preferences"]["events"]["new_commission"])
        self.assertNotIn("new_commission", response.json["preferences"]["overrides"])
        self.assertFalse(response.json["preferences"]["events"]["new_process_info"])

    def test_admin_user_form_exposes_inherited_and_explicit_push_choices(self):
        user = self.models.User(email="new@example.invalid", display_name="Neuer Benutzer")
        self.settings.set(self.keys.PUSH_DEFAULT_NEW_COMMISSION, False)
        with self.app.test_request_context():
            html = render_template("admin/user_form.html", item=user,
                push_preference_rows=self.preferences.admin_push_preference_rows(user),
                selected_craft_activities=[], craft_activities=[], commission_visibility_users=[])
        self.assertIn('name="push_event_new_commission"', html)
        self.assertIn("Standard (Inaktiv)", html)
        self.assertIn('value="on"', html)
        self.assertIn('value="off"', html)

    def test_high_priority_changes_enqueue_push_and_email_together(self):
        commission = self.commission(is_high_priority=True, execution_to=None)
        self.db.session.add(self.models.PushDevice(
            user=self.craftsman, installation_id="11111111-1111-1111-1111-111111111111", device_token="a" * 64,
        ))
        self.db.session.commit()
        batch = self.push.enqueue_commission_push(commission, self.models.PushEventType.CHANGED_COMMISSION)
        self.db.session.commit()
        self.assertEqual(batch.queued_count, 1)
        self.assertEqual(self.models.WorkflowNotification.query.count(), 1)
        self.notifications.process_workflow_notification_batch()
        self.send.assert_called_once()

    def test_ticket_response_uses_the_existing_reply_address_and_thread_marker(self):
        ticket = self.ticket()
        self.settings.set(self.keys.TENANT_TICKET_REPLY_TO, "tickets@example.invalid")
        self.db.session.commit()
        item = self.replies.enqueue_ticket_reply(ticket, "tenant@example.invalid", "Kein Auftrag erforderlich.")
        self.db.session.commit()
        self.notifications.process_workflow_notification_batch()
        message = self.send.call_args.args[0]
        self.assertEqual(str(message["Reply-To"]), "tickets@example.invalid")
        self.assertIn("(Ticket 12345)", str(message["Subject"]))
        self.assertEqual(item.state, "sent")

    def test_wowiport_button_opens_a_new_tab_and_invalid_hostnames_do_not_create_links(self):
        commission = self.commission()
        source = (self.root / "app/templates/admin/commission_detail.html").read_text()
        start = source.index("{% if wowiport_url %}")
        end = source.index("{% endif %}", start) + len("{% endif %}")
        template = self.app.jinja_env.from_string(source[start:end])
        html = template.render(wowiport_url=self.links.commission_wowiport_url(commission))
        self.assertIn('target="_blank"', html)
        self.assertIn('rel="noopener noreferrer"', html)
        for hostname in ("javascript:alert(1)", "https://user:password@example.invalid", "https://bad host.invalid"):
            self.settings.set(self.keys.WOWI_HOSTNAME, hostname)
            self.assertIsNone(self.links.commission_wowiport_url(commission))


class WorkflowMigrationTests(unittest.TestCase):
    def test_workflow_migration_preserves_orders_and_rolls_back_cleanly(self):
        root = Path(__file__).resolve().parents[1]
        database_url = os.environ.get("DATLAEUFT_TEST_DATABASE_URL", "sqlite://")
        url = make_url(database_url)
        if url.get_backend_name() != "sqlite" and not (url.database or "").startswith("datlaeuft_test"):
            self.fail("Migrationstests benötigen eine separate datlaeuft_test-Datenbank.")
        engine = sa.create_engine(database_url)
        self.addCleanup(engine.dispose)
        spec = importlib.util.spec_from_file_location("_workflow_migration", root / "migrations/versions/f1b3d5e7a9c2_commission_workflow_notifications.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with engine.begin() as connection:
            metadata = sa.MetaData()
            commission = sa.Table("commission", metadata, sa.Column("id", sa.Integer(), primary_key=True))
            ticket = sa.Table("ticket_mirror", metadata, sa.Column("id", sa.Integer(), primary_key=True))
            metadata.create_all(connection)
            connection.execute(commission.insert().values(id=1))
            with patch.object(migration, "op", Operations(MigrationContext.configure(connection))):
                migration.upgrade()
                self.assertIn("is_retrospective", {column["name"] for column in sa.inspect(connection).get_columns("commission")})
                self.assertEqual(connection.scalar(sa.text("SELECT is_retrospective FROM commission WHERE id=1")), 0)
                self.assertIn("workflow_notification", sa.inspect(connection).get_table_names())
                migration.downgrade()
                self.assertEqual(connection.scalar(sa.select(sa.func.count()).select_from(commission)), 1)
            metadata.drop_all(connection)
        self.assertEqual(ScriptDirectory(str(root / "migrations")).get_heads(), [migration.revision])


if __name__ == "__main__":
    unittest.main()
