import ast
import importlib
import os
import re
import sys
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from email.message import EmailMessage
from pathlib import Path
from threading import Barrier, Event
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from flask import Blueprint, Flask, abort, flash, g, redirect, render_template, request, url_for
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect
from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.orm import selectinload


class ExternalPortalLightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.db = SQLAlchemy()
        cls.app = Flask(__name__, template_folder=str(cls.root / "app/templates"))
        cls.app.config.update(SQLALCHEMY_DATABASE_URI="sqlite://", TESTING=True,
                              SECRET_KEY="external-response-tests-secret", WTF_CSRF_ENABLED=False)
        cls.db.init_app(cls.app)
        CSRFProtect(cls.app)
        manager = LoginManager(cls.app)
        manager.user_loader(lambda identity: None)
        cls.package_name = "_external_portal_light_test_app"
        package = types.ModuleType(cls.package_name)
        package.__path__ = [str(cls.root / "app")]
        extensions = types.ModuleType(cls.package_name + ".extensions")
        extensions.db = cls.db
        modules = {cls.package_name: package, extensions.__name__: extensions}
        real_names = {
            "models", "extensions", "utils", "settings", "health_rules", "invoice_layout", "invoice_cover_letter_layout",
            "email_templates", "email_layout", "mail_service", "external_branding", "external_craftsman_service",
            "external_order_responses", "external_portal", "commission_workflow_service", "process_info_service",
            "workflow_notifications", "audit", "erp_sync",
            "external_portal_texts",
        }
        for name in real_names - {"extensions"}:
            source = ast.parse((cls.root / "app" / (name + ".py")).read_text())
            for node in ast.walk(source):
                if not isinstance(node, ast.ImportFrom) or node.level != 1 or node.module in real_names:
                    continue
                module_name = cls.package_name + "." + node.module
                module = modules.setdefault(module_name, types.ModuleType(module_name))
                for item in node.names:
                    setattr(module, item.name, Mock())
        modules[cls.package_name + ".blacklist"].include_blacklisted = lambda: lambda function: function
        module_patch = patch.dict(sys.modules, modules)
        module_patch.start()
        cls.addClassCleanup(module_patch.stop)
        cls.models = importlib.import_module(cls.package_name + ".models")
        model_patch = patch.dict(sys.modules, {"app.models": cls.models})
        model_patch.start()
        cls.addClassCleanup(model_patch.stop)
        cls.settings_module = importlib.import_module(cls.package_name + ".settings")
        cls.responses = importlib.import_module(cls.package_name + ".external_order_responses")
        cls.external = importlib.import_module(cls.package_name + ".external_craftsman_service")
        cls.workflow = importlib.import_module(cls.package_name + ".commission_workflow_service")
        cls.notifications = importlib.import_module(cls.package_name + ".workflow_notifications")
        cls.portal = importlib.import_module(cls.package_name + ".external_portal")
        cls.portal_texts = importlib.import_module(cls.package_name + ".external_portal_texts")
        cls.mail_templates = importlib.import_module(cls.package_name + ".email_templates")
        cls.app.register_blueprint(cls.portal.external_portal_bp)
        cls.app.jinja_loader = ChoiceLoader([DictLoader({"base.html": "{% block content %}{% endblock %}{% block scripts %}{% endblock %}"}),
                                            FileSystemLoader(str(cls.root / "app/templates"))])
        namespace = {name: getattr(cls.models, name) for name in ("ExternalCraftsman", "User", "CraftActivity")}
        namespace.update(db=cls.db, settings=cls.settings_module.settings, SettingKeys=cls.settings_module.SettingKeys,
                         selectinload=selectinload, render_template=render_template, request=request, flash=flash, redirect=redirect,
                         url_for=url_for, audit=importlib.import_module(cls.package_name + ".audit").audit,
                         validate_external_communication=cls.responses.validate_external_communication, Any=object, abort=abort,
                         __package__=cls.package_name, __name__=cls.package_name + ".admin")
        source = ast.parse((cls.root / "app/admin.py").read_text())
        admin = Blueprint("admin", __name__)
        for name, path, methods in (("external_craftsmen", "/admin/external-craftsmen", ["GET"]),
                                    ("external_craftsman_communication", "/admin/external-craftsmen/<int:craftsman_id>/communication", ["POST"]),
                                    ("email_templates_page", "/admin/settings/email-templates", ["GET", "POST"])):
            function = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == name)
            function.decorator_list = []
            exec(compile(ast.Module(body=[function], type_ignores=[]), "admin.py", "exec"), namespace)
            admin.add_url_rule(path, endpoint=name, view_func=namespace[name], methods=methods)
        for name in ("external_craftsmen_sync", "external_craftsman_craft_activities", "external_craftsman_assignment",
                     "external_craftsman_invite", "external_craftsman_disable", "external_craftsman_enable", "settings_page"):
            path = "/admin/test/" + name + ("" if name in {"external_craftsmen_sync", "settings_page"} else "/<int:craftsman_id>")
            admin.add_url_rule(path, endpoint=name, view_func=lambda **values: "")
        cls.app.register_blueprint(admin)

    def setUp(self):
        context = self.app.app_context()
        context.push()
        self.addCleanup(context.pop)
        self.addCleanup(self.db.session.remove)
        self.db.drop_all()
        self.db.create_all()
        self.app.config["WTF_CSRF_ENABLED"] = False
        self.settings = self.settings_module.settings
        self.keys = self.settings_module.SettingKeys
        for definition, value in ((self.keys.PUBLIC_BASE_URL, "https://app.example.invalid"),
                                  (self.keys.SMTP_HOST, "smtp.example.invalid"),
                                  (self.keys.SMTP_SENDER, "office@example.invalid")):
            self.settings.set(definition, value)
        self.craftsman = self.models.ExternalCraftsman(craftsman_erp_id=900, display_name="Externe Firma",
                                                     email="external@example.invalid", portal_light_enabled=True,
                                                     assignment_enabled=True)
        self.commission = self.models.Commission(erp_id=100, erp_idnum="A-100", title="Rohrbruch",
                                                craftsman_erp_id=900, crafts_process_erp_id=800,
                                                status_id=12, local_status="waiting_for_acceptance", erp_sync_state="synced")
        self.db.session.add_all([self.craftsman, self.commission])
        self.db.session.flush()
        self.mail = self.models.ExternalCommissionAssignmentMail(commission=self.commission, external_craftsman=self.craftsman,
                                                                 assignment_version=self.commission.external_assignment_version,
                                                                 state="sent", sent_at=self.models.utc_now())
        self.db.session.add(self.mail)
        self.db.session.commit()
        self.url = self.responses.response_url(self.mail)
        self.path = urlsplit(self.url).path
        self.token = self.path.rsplit("/", 1)[1]
        self.client = self.app.test_client()
        self.status_email = self.start_patch(patch.object(self.portal, "send_external_status_notification"))
        self.send = self.start_patch(patch.object(self.notifications, "send_email", return_value=None))
        self.external_send = self.start_patch(patch.object(self.external, "send_external_email"))

    def start_patch(self, item):
        result = item.start()
        self.addCleanup(item.stop)
        return result

    def submit(self, decision="accept", text=""):
        return self.client.post(self.path, data={"decision": decision, "text": text})

    def schedule(self, days, first=2, second=4, cancellation=6):
        self.craftsman.reminder_first_days = first
        self.craftsman.reminder_second_days = second
        self.craftsman.cancel_unanswered_days = cancellation
        self.mail.sent_at = self.models.utc_now() - timedelta(days=days, seconds=1)
        self.db.session.commit()
        result = self.responses.ensure_external_followups()
        self.db.session.commit()
        return result

    def test_email_get_and_prefetched_reject_link_never_change_the_order(self):
        for choice in ("accept", "reject"):
            response = self.client.get(self.path + "?decision=" + choice)
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'Entscheidung absenden', response.data)
            self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
            self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(self.commission.local_status, "waiting_for_acceptance")
        self.assertIsNone(self.mail.response_at)
        self.assertEqual(self.models.ErpOutboxOperation.query.count(), 0)

    def test_accept_optional_note_is_queued_as_external_additional_information(self):
        response = self.submit(text="Bitte den Zugang zum Keller ermöglichen.")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.commission.local_status, "in_progress")
        self.assertEqual(self.mail.response_decision, "accept")
        info = self.models.ProcessInfo.query.one()
        self.assertEqual(info.body, "Bitte den Zugang zum Keller ermöglichen.")
        self.assertEqual(info.sender_external_craftsman_id, self.craftsman.id)
        self.assertEqual(info.process_erp_id, 800)
        self.assertEqual(info.state, "pending")
        self.assertEqual(self.models.ErpOutboxOperation.query.one().operation_type, "accept_commission")
        self.assertIn("Portalzugang einrichten", self.client.get(self.path).get_data(as_text=True))

    def test_accept_without_note_is_allowed_and_does_not_create_empty_information(self):
        self.assertEqual(self.submit().status_code, 303)
        self.assertEqual(self.models.ProcessInfo.query.count(), 0)

    def test_decision_can_be_changed_from_the_link_before_submitting(self):
        self.client.get(self.path + "?decision=accept")
        response = self.submit("reject", "Kapazitäten erschöpft")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.commission.completion_reason, "Kapazitäten erschöpft")
        self.assertEqual(self.mail.response_decision, "reject")
        self.assertIsNotNone(self.commission.rejected_at)
        self.assertEqual(self.models.ErpOutboxOperation.query.one().operation_type, "refuse_commission")
        self.status_email.assert_called_once_with(self.commission, event_name="rejected", reason="Kapazitäten erschöpft",
                                                  submitted_by_name=self.craftsman.display_name)

    def test_reject_without_reason_is_allowed_but_the_existing_workflow_requires_one_by_default(self):
        with self.assertRaises(self.workflow.CommissionWorkflowError):
            self.workflow.reject_commission(self.commission, "", external_craftsman=self.craftsman)
        self.assertEqual(self.submit("reject").status_code, 303)
        self.assertEqual(self.commission.completion_reason, "")
        self.assertIsNone(self.mail.response_text)

    def test_replay_and_changed_decision_after_submission_do_not_duplicate_actions(self):
        self.submit(text="Zusatzinformation")
        self.assertEqual(self.submit("reject", "Andere Entscheidung").status_code, 303)
        self.assertEqual(self.commission.local_status, "in_progress")
        self.assertEqual(self.mail.response_decision, "accept")
        self.assertEqual(self.models.ErpOutboxOperation.query.count(), 1)
        self.assertEqual(self.models.ProcessInfo.query.count(), 1)
        self.status_email.assert_not_called()

    def test_response_and_additional_information_are_rolled_back_together(self):
        self.commission.crafts_process_erp_id = None
        self.db.session.commit()
        self.assertEqual(self.submit(text="Bitte zurückrufen").status_code, 400)
        self.assertEqual(self.commission.local_status, "waiting_for_acceptance")
        self.assertIsNone(self.mail.response_at)
        self.assertEqual(self.models.ErpOutboxOperation.query.count(), 0)

    def test_invalid_decision_and_overlong_text_leave_order_unchanged(self):
        for data in ({"decision": "delete"}, {"decision": "accept", "text": "x" * 2001}):
            self.assertEqual(self.client.post(self.path, data=data).status_code, 400)
            self.assertEqual(self.commission.local_status, "waiting_for_acceptance")

    def test_csrf_protects_public_post_and_a_browser_can_submit_with_its_token(self):
        self.app.config["WTF_CSRF_ENABLED"] = True
        self.assertEqual(self.submit().status_code, 400)
        page = self.client.get(self.path).get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        self.assertEqual(self.client.post(self.path, data={"decision": "accept", "csrf_token": token}).status_code, 303)

    def test_tampered_and_expired_tokens_cannot_change_orders(self):
        self.assertEqual(self.client.post(self.path + "tampered", data={"decision": "accept"}).status_code, 410)
        with patch.object(self.responses, "RESPONSE_LINK_MAX_AGE", -1):
            self.assertEqual(self.submit().status_code, 410)
        self.assertEqual(self.commission.local_status, "waiting_for_acceptance")

    def test_reassignment_version_change_revoked_access_and_email_change_invalidate_links(self):
        for target, name, value in ((self.commission, "external_assignment_version", 99),
                                    (self.commission, "craftsman_erp_id", 901),
                                    (self.craftsman, "wowi_active", False),
                                    (self.craftsman, "portal_light_enabled", False),
                                    (self.craftsman, "email", "changed@example.invalid")):
            previous = getattr(target, name)
            setattr(target, name, value)
            self.db.session.commit()
            self.assertEqual(self.submit().status_code, 410)
            setattr(target, name, previous)
            self.db.session.commit()

    def test_already_processed_or_canceled_order_does_not_accept_an_old_email(self):
        for status in ("in_progress", "canceled", "deleted", "done"):
            self.commission.local_status = status
            self.db.session.commit()
            self.assertEqual(self.submit().status_code, 400)
            self.assertIsNone(self.mail.response_at)

    def test_optional_upgrade_sets_password_and_supports_future_login(self):
        self.submit()
        invitation = self.models.ExternalCraftsmanAccessToken(external_craftsman=self.craftsman, purpose="invitation",
                                                              token_hash="a" * 64, expires_at=self.models.utc_now() + timedelta(days=1))
        self.db.session.add(invitation)
        self.db.session.commit()
        password = "Testpasswort-mit-12-Zeichen"
        response = self.client.post(self.path + "/portalzugang", data={"password": password, "password_confirmation": password})
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.location.endswith("/auftragnehmer/auftraege"))
        self.assertTrue(self.craftsman.portal_enabled)
        self.assertTrue(self.craftsman.check_password(password))
        self.assertEqual(self.craftsman.username, "handwerker-900")
        self.assertIsNotNone(invitation.used_at)
        with self.client.session_transaction() as session:
            self.assertEqual(session["external_craftsman_id"], self.craftsman.id)
        self.client.post("/auftragnehmer/logout")
        g.pop("external_craftsman", None)
        response = self.client.post("/auftragnehmer/login", data={"username": "handwerker-900", "password": password})
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertEqual(session["external_craftsman_id"], self.craftsman.id)

    def test_upgrade_never_resets_an_existing_password_or_runs_before_response(self):
        password = "Unveraendertes-Kennwort"
        data = {"password": "Anderes-Kennwort-123", "password_confirmation": "Anderes-Kennwort-123"}
        self.client.post(self.path + "/portalzugang", data=data)
        self.assertIsNone(self.craftsman.password_hash)
        self.craftsman.set_password(password)
        self.craftsman.portal_enabled = True
        self.db.session.commit()
        self.submit()
        self.client.post(self.path + "/portalzugang", data=data)
        self.assertTrue(self.craftsman.check_password(password))
        self.assertIn("Im Portal anmelden", self.client.get(self.path).get_data(as_text=True))

    def test_expired_upgrade_and_invalid_password_leave_response_saved(self):
        self.submit()
        self.client.post(self.path + "/portalzugang", data={"password": "short", "password_confirmation": "short"})
        self.assertIsNone(self.craftsman.password_hash)
        self.mail.response_at = self.models.utc_now() - timedelta(hours=49)
        self.db.session.commit()
        self.client.post(self.path + "/portalzugang", data={"password": "Testpasswort-123", "password_confirmation": "Testpasswort-123"})
        self.assertIsNone(self.craftsman.password_hash)
        self.assertEqual(self.mail.response_decision, "accept")

    def test_global_default_and_explicit_overrides_are_independent_of_portal_access(self):
        self.craftsman.portal_light_enabled = None
        self.assertFalse(self.responses.portal_light_enabled(self.craftsman))
        self.settings.set(self.keys.EXTERNAL_PORTAL_LIGHT_DEFAULT, True)
        self.assertTrue(self.responses.portal_light_enabled(self.craftsman))
        self.craftsman.portal_light_enabled = False
        self.craftsman.portal_enabled = True
        self.assertFalse(self.responses.portal_light_enabled(self.craftsman))
        self.assertTrue(self.responses.automatic_external_mails_enabled(self.craftsman))

    def test_administration_renders_and_saves_the_per_craftsman_policy(self):
        page = self.client.get("/admin/external-craftsmen")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Erinnerungsmail 1 nach Tagen", page.get_data(as_text=True))
        self.assertIn('name="portal_light_enabled"', page.get_data(as_text=True))
        response = self.client.post(f"/admin/external-craftsmen/{self.craftsman.id}/communication", data={
            "portal_light_enabled": "disabled", "reminder_first_days": "2", "reminder_second_days": "4", "cancel_unanswered_days": "7",
        })
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.craftsman.portal_light_enabled)
        self.assertEqual((self.craftsman.reminder_first_days, self.craftsman.reminder_second_days, self.craftsman.cancel_unanswered_days), (2, 4, 7))
        self.assertEqual(self.models.AuditLog.query.filter_by(action="external_craftsman.communication.update").count(), 1)

    def test_invalid_administration_policy_does_not_partially_change_settings(self):
        self.client.post(f"/admin/external-craftsmen/{self.craftsman.id}/communication", data={
            "portal_light_enabled": "disabled", "reminder_first_days": "4", "reminder_second_days": "2", "cancel_unanswered_days": "7",
        })
        self.assertTrue(self.craftsman.portal_light_enabled)
        self.assertEqual(self.craftsman.reminder_first_days, 0)
        self.assertEqual(self.models.AuditLog.query.count(), 0)

    def test_assignments_and_reminders_contain_working_html_buttons_and_plain_text_urls(self):
        self.external.send_external_assignment_email(self.mail)
        message = self.external_send.call_args.args[0]
        plain, html = message.get_body(preferencelist=("plain",)).get_content(), message.get_body(preferencelist=("html",)).get_content()
        for text in ("Auftrag annehmen", "Auftrag ablehnen"):
            self.assertIn(text, plain)
            self.assertIn(text + "</a>", html)
        self.assertIn(self.path, html)
        self.assertIn("decision=reject", plain)
        self.craftsman.portal_enabled = True
        self.craftsman.portal_light_enabled = False
        self.db.session.commit()
        self.external.send_external_assignment_email(self.mail)
        plain = self.external_send.call_args.args[0].get_body(preferencelist=("plain",)).get_content()
        self.assertNotIn("Ohne Anmeldung antworten", plain)
        self.assertIn("/auftragnehmer/auftraege/", plain)

    def test_automatic_assignment_mail_can_be_sent_without_a_portal_account(self):
        self.db.session.delete(self.mail)
        self.db.session.commit()
        self.assertEqual(self.external.ensure_external_assignment_mails(), 1)
        self.db.session.commit()
        self.assertEqual(self.external.process_external_assignment_mail_batch(), 1)
        self.assertEqual(self.models.ExternalCommissionAssignmentMail.query.one().state, "sent")
        self.assertFalse(self.craftsman.portal_enabled)
        self.external_send.assert_called_once()

    def test_reminder_only_email_mode_does_not_require_portal_light(self):
        self.craftsman.portal_light_enabled = False
        self.craftsman.reminder_first_days = 2
        self.db.session.delete(self.mail)
        self.db.session.commit()
        self.assertEqual(self.external.ensure_external_assignment_mails(), 1)
        self.db.session.commit()
        self.external.process_external_assignment_mail_batch()
        plain = self.external_send.call_args.args[0].get_body(preferencelist=("plain",)).get_content()
        self.assertNotIn("/auftragnehmer/antwort/", plain)
        self.assertNotIn("/auftragnehmer/auftraege/", plain)

    def test_each_reminder_is_sent_once_after_its_own_deadline(self):
        self.assertEqual(self.schedule(1), 0)
        self.schedule(2)
        self.assertEqual(self.models.WorkflowNotification.query.count(), 1)
        self.notifications.process_workflow_notification_batch()
        message = self.send.call_args.args[0]
        self.assertIn("Erinnerung 1", str(message["Subject"]))
        self.assertIn("Auftrag annehmen</a>", message.get_body(preferencelist=("html",)).get_content())
        self.assertTrue(self.send.call_args.kwargs["external_craftsman"])
        self.schedule(3)
        self.notifications.process_workflow_notification_batch()
        self.assertEqual(self.send.call_count, 1)
        self.schedule(4)
        self.notifications.process_workflow_notification_batch()
        self.assertEqual(self.send.call_count, 2)
        self.assertIn("Erinnerung 2", str(self.send.call_args.args[0]["Subject"]))

    def test_timeout_cancels_through_the_office_workflow_and_queues_exactly_one_notice(self):
        self.schedule(6)
        self.assertEqual(self.commission.local_status, "canceled")
        self.assertIsNone(self.commission.deleted_at)
        self.assertIsNotNone(self.mail.auto_canceled_at)
        self.assertEqual(self.models.ErpOutboxOperation.query.one().operation_type, "cancel_commission_from_office")
        notice = self.models.WorkflowNotification.query.one()
        self.assertEqual(notice.payload["event"], "cancel")
        self.assertTrue(self.responses.followup_is_current(notice))
        self.assertEqual(self.responses.ensure_external_followups(), 0)
        self.db.session.commit()
        self.notifications.process_workflow_notification_batch()
        self.assertEqual(self.send.call_count, 1)
        message = self.send.call_args.args[0]
        self.assertIn("storniert", str(message["Subject"]))
        self.assertNotIn("Auftrag annehmen", message.get_body(preferencelist=("plain",)).get_content())

    def test_acceptance_rejection_or_reassignment_stops_queued_reminders_and_cancellation(self):
        self.schedule(2)
        reminder = self.models.WorkflowNotification.query.one()
        self.submit()
        self.assertFalse(self.responses.followup_is_current(reminder))
        self.notifications.process_workflow_notification_batch()
        self.send.assert_not_called()
        self.assertEqual(reminder.state, "discarded")
        self.schedule(7)
        self.assertEqual(self.commission.local_status, "in_progress")
        self.assertEqual(self.models.WorkflowNotification.query.count(), 1)

    def test_zero_deadlines_failed_initial_delivery_and_old_assignments_do_not_trigger_followups(self):
        self.schedule(10, first=0, second=0, cancellation=0)
        self.assertEqual(self.models.WorkflowNotification.query.count(), 0)
        self.mail.sent_at = None
        self.craftsman.cancel_unanswered_days = 1
        self.db.session.commit()
        self.assertEqual(self.responses.ensure_external_followups(), 0)
        self.mail.sent_at = self.models.utc_now() - timedelta(days=10)
        self.commission.external_assignment_version += 1
        self.db.session.commit()
        self.assertEqual(self.responses.ensure_external_followups(), 0)
        self.assertEqual(self.commission.local_status, "waiting_for_acceptance")

    def test_deadline_validation_rejects_negative_out_of_order_and_noninteger_values(self):
        for values in ({"reminder_first_days": "-1"}, {"reminder_first_days": "2.5"}, {"reminder_first_days": "3651"},
                       {"reminder_first_days": "3", "reminder_second_days": "3"},
                       {"reminder_first_days": "3", "cancel_unanswered_days": "2"},
                       {"reminder_second_days": "5", "cancel_unanswered_days": "5"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.responses.validate_external_communication(values)
        valid = self.responses.validate_external_communication({"portal_light_enabled": "disabled", "reminder_first_days": "0",
                                                               "reminder_second_days": "3", "cancel_unanswered_days": "5"})
        self.assertFalse(valid["portal_light_enabled"])
        self.assertEqual(valid["cancel_unanswered_days"], 5)

    @contextmanager
    def concurrency_database(self):
        database_url = os.environ["DATLAEUFT_TEST_DATABASE_URL"]
        if not (make_url(database_url).database or "").startswith("datlaeuft_test_"):
            self.fail("Gleichzeitige Zugriffe benötigen eine separate datlaeuft_test-Datenbank.")
        app = Flask("external-response-concurrency")
        app.config.update(SQLALCHEMY_DATABASE_URI=database_url, SECRET_KEY="external-response-concurrency-secret", TESTING=True)
        self.db.init_app(app)
        metadata = sa.MetaData()
        for model in (self.models.User, self.models.ExternalCraftsman, self.models.Commission, self.models.ExternalCommissionAssignmentMail,
                      self.models.ErpOutboxOperation, self.models.AuditLog, self.models.AppSetting, self.models.WorkflowNotification):
            table = model.__table__
            sa.Table(table.name, metadata, *[
                sa.Column(column.name, column.type, primary_key=column.primary_key, nullable=column.nullable,
                          server_default=column.server_default.arg if column.server_default is not None else None)
                for column in table.columns
            ])
        try:
            with app.app_context():
                metadata.create_all(self.db.engine)
                craftsman = self.models.ExternalCraftsman(craftsman_erp_id=900, display_name="Externe Firma", email="external@example.invalid",
                                                          portal_light_enabled=True, cancel_unanswered_days=1)
                commission = self.models.Commission(erp_id=100, erp_idnum="A-100", craftsman_erp_id=900,
                                                    status_id=12, local_status="waiting_for_acceptance", erp_sync_state="synced")
                self.db.session.add_all([craftsman, commission])
                self.db.session.flush()
                mail = self.models.ExternalCommissionAssignmentMail(commission=commission, external_craftsman=craftsman,
                                                                     assignment_version=commission.external_assignment_version,
                                                                     state="sent", sent_at=self.models.utc_now() - timedelta(days=2))
                self.db.session.add(mail)
                self.db.session.commit()
                token = self.responses.response_serializer().dumps(self.responses.response_identity(mail))
            yield app, token
        finally:
            with app.app_context():
                self.db.session.remove()
                metadata.drop_all(self.db.engine)
                self.db.engine.dispose()

    @unittest.skipUnless(os.environ.get("DATLAEUFT_TEST_DATABASE_URL", "").startswith("mysql"), "Benötigt MariaDB für gleichzeitige Antworten")
    def test_simultaneous_conflicting_responses_create_only_one_decision_and_erp_operation(self):
        with self.concurrency_database() as (app, token):
            barrier = Barrier(2)

            def respond(decision):
                with app.app_context():
                    self.models.Commission.query.one()
                    self.models.ExternalCommissionAssignmentMail.query.one()
                    barrier.wait(timeout=10)
                    item = self.responses.response_assignment(token, lock=True)
                    changed = self.responses.submit_response(item, decision, "")
                    self.db.session.commit()
                    return changed

            with ThreadPoolExecutor(max_workers=2) as executor:
                self.assertEqual(sorted(executor.map(respond, ("accept", "reject"))), [False, True])
            with app.app_context():
                self.assertEqual(self.models.ErpOutboxOperation.query.count(), 1)
                mail = self.models.ExternalCommissionAssignmentMail.query.one()
                status = "in_progress" if mail.response_decision == "accept" else "completion_review"
                self.assertEqual(mail.commission.local_status, status)

    @unittest.skipUnless(os.environ.get("DATLAEUFT_TEST_DATABASE_URL", "").startswith("mysql"), "Benötigt MariaDB für Antwort und Stornierung")
    def test_simultaneous_response_and_timeout_cannot_both_change_the_order(self):
        with self.concurrency_database() as (app, token):
            barrier = Barrier(2)

            def act(action):
                with app.app_context():
                    self.models.Commission.query.one()
                    self.models.ExternalCommissionAssignmentMail.query.one()
                    barrier.wait(timeout=10)
                    try:
                        if action == "respond":
                            item = self.responses.response_assignment(token, lock=True)
                            self.responses.submit_response(item, "accept", "")
                        else:
                            self.responses.ensure_external_followups()
                        self.db.session.commit()
                    except ValueError:
                        self.db.session.rollback()

            with ThreadPoolExecutor(max_workers=2) as executor:
                list(executor.map(act, ("respond", "timeout")))
            with app.app_context():
                self.assertEqual(self.models.ErpOutboxOperation.query.count(), 1)
                mail = self.models.ExternalCommissionAssignmentMail.query.one()
                self.assertNotEqual(mail.response_at is None, mail.auto_canceled_at is None)
                self.assertEqual(mail.commission.local_status, "in_progress" if mail.response_at else "canceled")

    @unittest.skipUnless(os.environ.get("DATLAEUFT_TEST_DATABASE_URL", "").startswith("mysql"), "Benötigt MariaDB für mehrere Aufträge einer Firma")
    def test_timeout_releases_company_locks_before_processing_another_order(self):
        with self.concurrency_database() as (app, token):
            with app.app_context():
                craftsman = self.models.ExternalCraftsman.query.one()
                second = self.models.Commission(erp_id=101, erp_idnum="A-101", craftsman_erp_id=900,
                                                status_id=12, local_status="waiting_for_acceptance", erp_sync_state="synced")
                self.db.session.add(second)
                self.db.session.flush()
                mail = self.models.ExternalCommissionAssignmentMail(commission=second, external_craftsman=craftsman,
                                                                     assignment_version=second.external_assignment_version,
                                                                     state="sent", sent_at=self.models.utc_now() - timedelta(days=2))
                self.db.session.add(mail)
                self.db.session.commit()
                second_id = second.id
                second_token = self.responses.response_serializer().dumps(self.responses.response_identity(mail))
            first_queued, second_locked = Event(), Event()
            enqueue = self.notifications.enqueue_notification

            def enqueue_and_wait(*args, **kwargs):
                notification = enqueue(*args, **kwargs)
                if kwargs["commission"].erp_id == 100:
                    first_queued.set()
                    self.assertTrue(second_locked.wait(timeout=10))
                return notification

            def timeout():
                with app.app_context():
                    self.responses.ensure_external_followups()
                    self.db.session.commit()

            def respond():
                self.assertTrue(first_queued.wait(timeout=10))
                with app.app_context():
                    self.models.Commission.query.filter_by(id=second_id).with_for_update().one()
                    second_locked.set()
                    item = self.responses.response_assignment(second_token, lock=True)
                    self.responses.submit_response(item, "accept", "")
                    self.db.session.commit()

            with patch.object(self.notifications, "enqueue_notification", side_effect=enqueue_and_wait):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    first = executor.submit(timeout)
                    second = executor.submit(respond)
                    first.result(timeout=25)
                    second.result(timeout=25)
            with app.app_context():
                states = {order.erp_id: order.local_status for order in self.models.Commission.query.all()}
                self.assertEqual(states, {100: "canceled", 101: "in_progress"})
                self.assertEqual(self.models.ErpOutboxOperation.query.count(), 2)
                self.assertEqual(self.models.WorkflowNotification.query.count(), 1)


if __name__ == "__main__":
    unittest.main()
