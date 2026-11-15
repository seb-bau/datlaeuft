import ast
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
import importlib
import importlib.util
import os
from pathlib import Path
import sys
from threading import Barrier
import types
import unittest
from unittest.mock import Mock, patch

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from flask import Flask, abort, g, redirect, render_template, request, url_for
from flask_jwt_extended import create_access_token
from flask_login import LoginManager, current_user, login_required
from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from werkzeug.exceptions import Forbidden

import test_commission_object_levels as object_fixtures


class CommissionApprovalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        object_fixtures.CommissionObjectLevelTests.setUpClass.__func__(cls)
        cls.app.config["SECRET_KEY"] = "commission-approval-test-secret"
        real_modules = {
            "models", "extensions", "settings", "utils", "blacklist", "email_templates", "email_layout",
            "object_directory", "commission_service", "wowiport", "api", "tenant_notifications",
            "commission_approval", "workflow_notifications", "push_service", "push_preferences", "audit",
        }
        for name in ("audit", "push_preferences", "push_service", "workflow_notifications", "commission_approval"):
            sys.modules.pop(cls.package_name + "." + name, None)
        cls.approvals = importlib.import_module(cls.package_name + ".commission_approval")
        cls.workflow = importlib.import_module(cls.package_name + ".workflow_notifications")
        cls.push = importlib.import_module(cls.package_name + ".push_service")
        cls.service.prepare_commission_approval = cls.approvals.prepare_commission_approval
        cls.service.enqueue_priority_notification = cls.workflow.enqueue_priority_notification
        erp_source = ast.parse((cls.root / "app/erp_sync.py").read_text())
        functions = [node for node in erp_source.body if isinstance(node, ast.FunctionDef) and node.name in {
            "enqueue_erp_operation", "claim_outbox_operation", "execute_outbox_operation",
        }]
        namespace = {name: getattr(cls.models, name) for name in dir(cls.models)}
        namespace.update(db=cls.db, Any=object, WowiportService=object, ERP_OPERATION_STATUS=cls.gateway.ERP_OPERATION_STATUS)
        exec(compile(ast.Module(body=functions, type_ignores=[]), "erp_sync.py", "exec"), namespace)
        cls.erp = sys.modules[cls.package_name + ".erp_sync"]
        cls.erp.enqueue_erp_operation = Mock(wraps=namespace["enqueue_erp_operation"])
        cls.erp.claim_outbox_operation = namespace["claim_outbox_operation"]
        cls.erp.execute_outbox_operation = namespace["execute_outbox_operation"]
        cls.service.enqueue_erp_operation = cls.erp.enqueue_erp_operation
        admin_source = ast.parse((cls.root / "app/admin.py").read_text())
        for node in ast.walk(admin_source):
            if not isinstance(node, ast.ImportFrom) or node.level != 1 or node.module in real_modules:
                continue
            name = cls.package_name + "." + node.module
            module = sys.modules.setdefault(name, types.ModuleType(name))
            for item in node.names:
                if not hasattr(module, item.name):
                    setattr(module, item.name, login_required if node.module == "auth" else Mock())
        auth_source = ast.parse((cls.root / "app/auth.py").read_text())
        auth_functions = [node for node in auth_source.body if isinstance(node, ast.FunctionDef) and node.name in {"foreman_required", "office_review_required"}]
        auth_namespace = dict(F=object, Any=object, wraps=wraps, current_user=current_user, abort=abort, redirect=redirect, url_for=url_for, request=request)
        exec(compile(ast.Module(body=auth_functions, type_ignores=[]), "auth.py", "exec"), auth_namespace)
        auth = sys.modules[cls.package_name + ".auth"]
        for name in ("foreman_required", "office_review_required"):
            setattr(auth, name, auth_namespace[name])
        cls.admin = importlib.import_module(cls.package_name + ".admin")
        cls.app.register_blueprint(cls.admin.admin_bp)
        cls.app.add_url_rule("/login", endpoint="auth.login", view_func=lambda: "Login")
        cls.app.add_url_rule("/logout", endpoint="auth.logout", view_func=lambda: "Logout")
        manager = LoginManager(cls.app)
        manager.user_loader(lambda identity: cls.db.session.get(cls.models.User, int(identity)))
        cls.app.jinja_loader = ChoiceLoader([
            DictLoader({"base.html": "{% block content %}{% endblock %}"}), FileSystemLoader(str(cls.root / "app/templates")),
        ])
        cls.app.jinja_env.globals["csrf_token"] = lambda: "test-token"
        cls.tables.append(cls.models.user_commission_visibility)
        for name in (
            "WorkflowNotification", "ErpOutboxOperation", "AuditLog", "PushDevice", "PushDelivery",
            "CommissionTenantSmsNotification", "CommissionCompletionNotification", "ExternalCommissionAssignmentMail",
            "TicketMirror", "TimeEntry", "InventoryMovement", "InventoryItem", "CommissionPhoto", "CommissionAppointmentRequest",
        ):
            table = getattr(cls.models, name).__table__
            if table not in cls.tables:
                cls.tables.append(table)

    def setUp(self):
        object_fixtures.CommissionObjectLevelTests.setUp(self)
        self.creator.is_foreman = True
        self.reviewer = self.models.User(email="reviewer@example.invalid", display_name="Freigebende Sachbearbeitung", is_foreman=True)
        self.administrator = self.models.User(email="admin@example.invalid", display_name="Administration", is_admin=True)
        self.external = self.models.ExternalCraftsman(craftsman_erp_id=3030, display_name="Externe Firma", email="external@example.invalid", wowi_active=True, portal_enabled=True)
        self.db.session.add_all([self.reviewer, self.administrator, self.external])
        self.db.session.flush()
        self.creator.external_commission_approver_user_id = self.reviewer.id
        self.service.resolve_external_craftsman.return_value = self.external
        self.service.parse_commission_priority.return_value = False
        self.db.session.commit()
        self.mail = Mock(return_value=None)
        self.mail_patch = patch.object(self.workflow, "send_email", self.mail)
        self.mail_patch.start()
        self.addCleanup(self.mail_patch.stop)
        for definition, value in (
            (self.settings.SettingKeys.SMTP_HOST, "smtp.example.invalid"),
            (self.settings.SettingKeys.SMTP_SENDER, "app@example.invalid"),
            (self.settings.SettingKeys.PUBLIC_BASE_URL, "https://app.example.invalid"),
        ):
            self.settings.settings.set(definition, value)
        self.db.session.commit()

    def create_order(self, assignment="external", **values):
        return object_fixtures.CommissionObjectLevelTests.create_order(
            self, "license_agreement", self.agreement.id,
            assignment_type=assignment, external_craftsman_id=self.external.id if assignment == "external" else None,
            **values,
        )

    def login(self, user):
        g.pop("_login_user", None)
        with self.client.session_transaction() as session:
            session.clear()
            session["_user_id"] = str(user.id)
            session["_fresh"] = True

    def test_internal_and_external_policies_are_independent_and_held_orders_have_no_erp_operation(self):
        internal = self.create_order("internal")
        external = self.create_order()
        self.assertEqual(internal.approval_state, "not_required")
        self.assertEqual(external.approval_state, "pending")
        self.assertEqual(external.local_status, "waiting_for_approval")
        self.assertEqual(external.approval_approver_user_id, self.reviewer.id)
        self.assertFalse(external.is_actionable)
        self.assertIsNone(external.erp_id)
        self.assertEqual({row.commission_id for row in self.models.ErpOutboxOperation.query.all()}, {internal.id})
        self.creator.internal_commission_approver_user_id = self.administrator.id
        self.creator.external_commission_approver_user_id = None
        self.db.session.commit()
        self.assertEqual(self.create_order("internal").approval_approver_user_id, self.administrator.id)
        self.assertEqual(self.create_order().approval_state, "not_required")

    def test_pending_orders_notify_only_the_selected_reviewer_without_waiting_for_an_erp_number(self):
        self.service.parse_commission_priority.return_value = True
        order = self.create_order(priority="high")
        self.push.enqueue_commission_push(order, self.models.PushEventType.NEW_COMMISSION)
        self.db.session.commit()
        self.assertEqual(self.models.PushDelivery.query.count(), 0)
        self.assertEqual(self.models.WorkflowNotification.query.count(), 1)
        self.assertEqual(self.workflow.process_workflow_notification_batch(), 1)
        self.mail.assert_called_once()
        message = self.mail.call_args.args[0]
        self.assertEqual(str(message["To"]), self.reviewer.email)
        self.assertIn("Freigabe erforderlich", str(message["Subject"]))
        self.assertIn(f"/admin/commissions/{order.id}", message.get_body(preferencelist=("plain",)).get_content())

    def test_approval_releases_exactly_one_erp_operation_and_records_the_other_user(self):
        order = self.create_order()
        for _ in range(2):
            approved, changed = self.approvals.approve_commission(order.id, self.reviewer)
            self.db.session.commit()
        self.assertFalse(changed)
        self.assertEqual(approved.approval_state, "approved")
        self.assertEqual(approved.approved_by_user_id, self.reviewer.id)
        self.assertIsNotNone(approved.approved_at)
        self.assertEqual(approved.local_status, "recorded")
        self.assertEqual(self.models.ErpOutboxOperation.query.count(), 1)
        self.assertEqual(self.models.AuditLog.query.filter_by(action="commission.approval.approved").count(), 1)
        self.workflow.process_workflow_notification_batch()
        self.mail.assert_not_called()

    def test_snapshot_conflicts_are_retried_from_a_fresh_transaction_with_a_bounded_limit(self):
        order = self.create_order()
        original = self.approvals._approve_commission
        calls = []

        def approve(commission_id, reviewer_id):
            calls.append(commission_id)
            if len(calls) == 1:
                raise OperationalError("SELECT commission FOR UPDATE", {}, Exception(1020, "Record has changed"))
            return original(commission_id, reviewer_id)

        with patch.object(self.approvals, "_approve_commission", side_effect=approve):
            self.approvals.approve_commission(order.id, self.reviewer)
            self.db.session.commit()
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.models.ErpOutboxOperation.query.count(), 1)
        pending = self.create_order()
        for code, expected_attempts in ((1020, 3), (2006, 1)):
            with self.subTest(code=code), patch.object(self.approvals, "_approve_commission", side_effect=OperationalError("SELECT", {}, Exception(code, "database error"))) as attempt:
                with self.assertRaises(OperationalError):
                    self.approvals.approve_commission(pending.id, self.reviewer)
                self.assertEqual(attempt.call_count, expected_attempts)
        self.assertEqual(self.models.ErpOutboxOperation.query.count(), 1)
        self.assertEqual(pending.approval_state, "pending")

    def test_creators_cannot_approve_even_after_their_policy_is_removed_or_they_become_admins(self):
        order = self.create_order()
        self.creator.external_commission_approver_user_id = None
        self.creator.is_admin = True
        self.db.session.commit()
        with self.assertRaises(Forbidden):
            self.approvals.approve_commission(order.id, self.creator)
        self.db.session.rollback()
        self.assertEqual(self.models.ErpOutboxOperation.query.count(), 0)
        self.assertEqual(order.approval_state, "pending")

    def test_only_the_assigned_reviewer_or_another_unrestricted_admin_can_approve(self):
        order = self.create_order()
        self.craftsman.is_foreman = True
        self.db.session.commit()
        with self.assertRaises(Forbidden):
            self.approvals.approve_commission(order.id, self.craftsman)
        self.db.session.rollback()
        self.reviewer.internal_commission_approver_user_id = self.administrator.id
        self.db.session.commit()
        with self.assertRaises(Forbidden):
            self.approvals.approve_commission(order.id, self.reviewer)
        self.db.session.rollback()
        self.approvals.approve_commission(order.id, self.administrator)
        self.db.session.commit()
        self.assertEqual(order.approved_by_user_id, self.administrator.id)

    def test_the_original_reviewer_is_preserved_when_the_creator_policy_changes(self):
        order = self.create_order()
        self.creator.external_commission_approver_user_id = self.administrator.id
        self.db.session.commit()
        self.assertEqual(order.approval_approver_user_id, self.reviewer.id)
        self.approvals.approve_commission(order.id, self.reviewer)
        self.db.session.commit()
        self.assertEqual(order.approved_by_user_id, self.reviewer.id)

    def test_retroactive_orders_are_held_too_and_recover_their_correct_final_status_after_approval(self):
        self.creator.internal_commission_approver_user_id = self.reviewer.id
        self.db.session.commit()
        for assignment, status in (("internal", "done"), ("external", "awaiting_invoice")):
            with self.subTest(assignment=assignment):
                order = self.create_order(assignment, is_retrospective=True)
                self.assertIsNone(order.completed_at)
                self.assertEqual(order.local_status, "waiting_for_approval")
                self.approvals.approve_commission(order.id, self.reviewer)
                self.db.session.commit()
                self.assertEqual(order.local_status, status)
                self.assertIsNotNone(order.completed_at)
                self.assertFalse(order.notify_tenants)
        self.assertEqual(self.models.PushDelivery.query.count(), 0)

    def test_mobile_creation_cannot_bypass_approval_by_supplying_approval_fields(self):
        self.creator.internal_commission_approver_user_id = self.reviewer.id
        self.db.session.commit()
        response = self.client.post("/api/commissions", headers=self.headers, json={
            "object_type": "license_agreement", "object_id": self.agreement.id, "commission_type_id": 100,
            "craft_activity_id": self.craft.id, "approval_state": "approved", "approved_by_user_id": self.creator.id,
            "positions": [{"service_catalog_id": 600, "commission_text": "Bitte reparieren", "units": 1}],
        })
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json["item"]["approval_state"], "pending")
        self.assertFalse(response.json["item"]["can_edit"])
        self.assertEqual(self.models.ErpOutboxOperation.query.count(), 0)

    def test_pending_orders_are_hidden_from_the_assigned_craftsman_but_visible_to_their_creator(self):
        self.creator.internal_commission_approver_user_id = self.reviewer.id
        self.db.session.commit()
        order = self.create_order("internal")
        craftsman_headers = {"Authorization": "Bearer " + create_access_token(identity=str(self.craftsman.id))}
        g.pop("api_user", None)
        self.assertEqual(self.client.get(f"/api/commissions/{order.id}", headers=craftsman_headers).status_code, 403)
        g.pop("api_user", None)
        self.assertEqual(self.client.get("/api/commissions?scope=all", headers=craftsman_headers).json["items"], [])
        g.pop("api_user", None)
        self.assertEqual(self.client.get(f"/api/commissions/{order.id}", headers=self.headers).status_code, 200)
        self.approvals.approve_commission(order.id, self.reviewer)
        self.db.session.commit()
        g.pop("api_user", None)
        self.assertEqual(self.client.get(f"/api/commissions/{order.id}", headers=craftsman_headers).status_code, 200)

    def test_review_pages_actions_and_review_api_are_forbidden_for_a_user_with_either_policy(self):
        self.login(self.creator)
        for assignment in ("external", "internal"):
            self.creator.external_commission_approver_user_id = self.reviewer.id if assignment == "external" else None
            self.creator.internal_commission_approver_user_id = self.reviewer.id if assignment == "internal" else None
            self.db.session.commit()
            for path in (
                "/admin/commission-reviews", "/admin/performance-reviews", "/admin/work-time-reports", "/admin/time-entries", "/admin/material-movements",
            ):
                with self.subTest(assignment=assignment, path=path):
                    self.assertEqual(self.client.get(path).status_code, 403)
        self.assertEqual(self.client.post("/admin/performance-reviews/1/unassigned/approve").status_code, 403)
        g.pop("api_user", None)
        self.assertEqual(self.client.get("/api/review/time-entries", headers=self.headers).status_code, 403)
        g.pop("api_user", None)
        self.assertEqual(self.client.get("/api/review/materials", headers=self.headers).status_code, 403)
        self.creator.is_admin = True
        self.db.session.commit()
        self.assertEqual(self.client.get("/admin/commission-reviews").status_code, 403)

    def test_the_assigned_reviewer_sees_another_authors_pending_order_in_review(self):
        order = self.create_order()
        self.login(self.reviewer)
        with self.app.test_request_context("/admin/commission-reviews"):
            g._login_user = self.reviewer
            self.assertEqual([item.id for item in self.admin.commission_review_query().all()], [order.id])
        response = self.client.get("/admin/commission-reviews?status=waiting_for_approval")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Freigabe prüfen", response.get_data(as_text=True))
        self.assertEqual(self.client.post(f"/admin/commissions/{order.id}/approve").status_code, 302)
        self.assertEqual(order.approval_state, "approved")

    def test_user_settings_reject_self_inactive_restricted_and_non_office_approvers(self):
        form = {"commission_approval_settings_present": "1", "internal_commission_approver_user_id": ""}
        for user in (self.creator, self.craftsman):
            with self.subTest(user=user.id), self.assertRaises(ValueError):
                self.approvals.save_user_approval_settings(self.creator, {**form, "external_commission_approver_user_id": str(user.id)})
        self.reviewer.is_active_flag = False
        self.db.session.commit()
        with self.assertRaises(ValueError):
            self.approvals.save_user_approval_settings(self.creator, {**form, "external_commission_approver_user_id": str(self.reviewer.id)})
        self.reviewer.is_active_flag = True
        self.reviewer.internal_commission_approver_user_id = self.administrator.id
        self.db.session.commit()
        with self.assertRaises(ValueError):
            self.approvals.save_user_approval_settings(self.creator, {**form, "external_commission_approver_user_id": str(self.reviewer.id)})

    def test_an_assigned_reviewer_cannot_be_made_subject_to_approval_without_resolving_assignments(self):
        with self.assertRaisesRegex(ValueError, "noch als Freigeber"):
            self.approvals.save_user_approval_settings(self.reviewer, {
                "commission_approval_settings_present": "1", "internal_commission_approver_user_id": str(self.administrator.id),
            })

    def test_user_settings_save_each_assignment_independently_and_preserve_absent_fields(self):
        self.approvals.save_user_approval_settings(self.creator, {
            "commission_approval_settings_present": "1",
            "internal_commission_approver_user_id": str(self.administrator.id),
            "external_commission_approver_user_id": str(self.reviewer.id),
        })
        self.db.session.commit()
        self.assertEqual(self.creator.internal_commission_approver_user_id, self.administrator.id)
        self.assertEqual(self.creator.external_commission_approver_user_id, self.reviewer.id)
        self.approvals.save_user_approval_settings(self.creator, {})
        self.assertEqual(self.creator.internal_commission_approver_user_id, self.administrator.id)
        self.approvals.save_user_approval_settings(self.creator, {"commission_approval_settings_present": "1"})
        self.assertFalse(self.creator.requires_commission_approval)

    def test_queued_tenant_sms_completion_and_external_emails_cannot_contact_anyone_before_approval(self):
        for name in ("sms_service", "tenant_sms_notifications", "completion_notifications", "external_craftsman_service"):
            sys.modules.pop(self.package_name + "." + name, None)
        for source_name in ("tenant_sms_notifications", "completion_notifications", "external_craftsman_service"):
            source = ast.parse((self.root / "app" / (source_name + ".py")).read_text())
            for node in ast.walk(source):
                if not isinstance(node, ast.ImportFrom) or node.level != 1:
                    continue
                dependency = sys.modules.get(self.package_name + "." + node.module)
                if dependency is not None and not getattr(dependency, "__file__", None):
                    for name in node.names:
                        if not hasattr(dependency, name.name):
                            setattr(dependency, name.name, Mock())
        sms = importlib.import_module(self.package_name + ".tenant_sms_notifications")
        completion = importlib.import_module(self.package_name + ".completion_notifications")
        external = importlib.import_module(self.package_name + ".external_craftsman_service")
        order = self.create_order()
        order.erp_id = 9000
        order.erp_idnum = "A-9000"
        order.status_id = 12
        order.erp_sync_state = "synced"
        self.settings.settings.set(self.settings.SettingKeys.SMS_ENABLED, True)
        tenant_mail = self.models.CommissionTenantNotification(commission=order, state="sending")
        tenant_sms = self.models.CommissionTenantSmsNotification(commission=order, state="sending")
        completion_mail = self.models.CommissionCompletionNotification(commission=order, state="sending")
        external_mail = self.models.ExternalCommissionAssignmentMail(
            commission=order, external_craftsman=self.external, assignment_version=order.external_assignment_version, state="sending",
        )
        items = [tenant_mail, tenant_sms, completion_mail, external_mail]
        self.db.session.add_all(items)
        self.db.session.commit()
        with (
            patch.object(self.notifications, "send_email") as tenant_send,
            patch.object(sms, "send_sms") as sms_send,
            patch.object(completion, "send_email") as completion_send,
            patch.object(external, "send_external_assignment_email") as external_send,
        ):
            self.notifications.execute_tenant_notification(tenant_mail.id)
            sms.execute_tenant_sms_notification(tenant_sms.id)
            completion.execute_completion_notification(completion_mail.id)
            external.execute_external_assignment_mail(external_mail.id)
            for sender in (tenant_send, sms_send, completion_send, external_send):
                sender.assert_not_called()
        self.assertEqual({item.state for item in items}, {"discarded"})

    def test_urgent_craftsman_email_is_queued_only_after_approval_and_then_uses_the_normal_delivery(self):
        self.service.parse_commission_priority.return_value = True
        order = self.create_order(priority="high")
        self.assertEqual({item.kind for item in self.models.WorkflowNotification.query.all()}, {"approval_request"})
        self.approvals.approve_commission(order.id, self.reviewer)
        order.erp_id = 9000
        order.erp_idnum = "A-9000"
        order.status_id = 12
        order.local_status = "waiting_for_acceptance"
        order.erp_sync_state = "synced"
        self.db.session.commit()
        self.assertEqual({item.kind for item in self.models.WorkflowNotification.query.all()}, {"approval_request", "priority"})
        self.workflow.process_workflow_notification_batch()
        self.mail.assert_called_once()
        self.assertEqual(str(self.mail.call_args.args[0]["To"]), self.external.email)

    def test_worker_and_gateway_refuse_a_pending_order_even_if_an_erp_operation_was_queued(self):
        order = self.create_order()
        self.erp.enqueue_erp_operation(order, self.models.ErpOperationType.CREATE_COMMISSION, "unexpected-operation")
        self.db.session.commit()
        operation = self.models.ErpOutboxOperation.query.one()
        self.assertIsNone(self.erp.claim_outbox_operation())
        service = Mock()
        with self.assertRaisesRegex(RuntimeError, "Freigabe"):
            self.erp.execute_outbox_operation(operation.id, service)
        self.assertEqual(service.mock_calls, [])
        gateway = object.__new__(self.gateway.WowiportService)
        gateway.client = Mock()
        with self.assertRaisesRegex(ValueError, "Freigabe"):
            gateway.create_commission_waiting_for_acceptance(order)
        self.assertEqual(gateway.client.mock_calls, [])

    def test_dashboard_and_user_form_display_the_approval_policy_without_review_links_for_restricted_users(self):
        with self.app.test_request_context():
            g._login_user = self.creator
            html = render_template("admin/dashboard.html", open_count=0, low_stock_count=0, my_open_ticket_count=0)
            self.assertNotIn("commission-reviews", html)
            self.assertNotIn("Zu prüfende Stundenzettel", html)
            g._login_user = self.reviewer
            html = render_template("admin/dashboard.html", approval_review_count=1, open_count=0, low_stock_count=0, my_open_ticket_count=0)
            self.assertIn("waiting_for_approval", html)
            self.assertIn("tenant_charge_review", html)
            self.assertIn("Freigabe ausstehend", html)
            source = (self.root / "app/templates/admin/user_form.html").read_text()
            start = source.index('<h2 class="h5">Auftragsfreigabe')
            end = source.index('<h2 class="h5">Push-Mitteilungen', start)
            template = self.app.jinja_env.from_string(source[start:end])
            html = template.render(item=self.creator, approval_user_choices=self.approvals.approval_user_choices(self.creator))
            self.assertIn('name="internal_commission_approver_user_id"', html)
            self.assertIn('name="external_commission_approver_user_id"', html)
            self.assertIn(f'value="{self.reviewer.id}" selected', html)
            self.assertIn("Kein", html)

    @unittest.skipUnless(os.environ.get("DATLAEUFT_TEST_DATABASE_URL", "").startswith("mysql"), "Benötigt MariaDB für gleichzeitige Freigaben")
    def test_simultaneous_approvals_create_only_one_erp_operation(self):
        database_url = os.environ["DATLAEUFT_TEST_DATABASE_URL"]
        if not (make_url(database_url).database or "").startswith("datlaeuft_test"):
            self.fail("Diese Prüfung benötigt eine separate datlaeuft_test-Datenbank.")
        app = Flask("approval-concurrency")
        app.config.update(SQLALCHEMY_DATABASE_URI=database_url, TESTING=True)
        self.db.init_app(app)
        metadata = sa.MetaData()
        for table in self.tables:
            sa.Table(table.name, metadata, *[
                sa.Column(column.name, column.type, primary_key=column.primary_key, nullable=column.nullable,
                          server_default=column.server_default.arg if column.server_default is not None else None)
                for column in table.columns
            ])
        with app.app_context():
            metadata.create_all(self.db.engine)
            first = self.models.User(email="first@example.invalid", display_name="Freigeber", is_foreman=True)
            second = self.models.User(email="second@example.invalid", display_name="Administration", is_admin=True)
            creator = self.models.User(email="creator@example.invalid", display_name="Ersteller")
            self.db.session.add_all([first, second, creator])
            self.db.session.flush()
            order = self.models.Commission(created_by=creator, approval_state="pending", approval_approver_user_id=first.id, local_status="waiting_for_approval")
            self.db.session.add(order)
            self.db.session.commit()
            order_id, reviewer_ids = order.id, (first.id, second.id)
        barrier = Barrier(2)

        def approve(reviewer_id):
            with app.app_context():
                reviewer = self.db.session.get(self.models.User, reviewer_id)
                barrier.wait(timeout=10)
                _, changed = self.approvals.approve_commission(order_id, reviewer)
                self.db.session.commit()
                return changed

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                self.assertEqual(sorted(executor.map(approve, reviewer_ids)), [False, True])
            with app.app_context():
                self.assertEqual(self.models.ErpOutboxOperation.query.count(), 1)
        finally:
            with app.app_context():
                self.db.session.remove()
                metadata.drop_all(self.db.engine)
                self.db.engine.dispose()


class CommissionApprovalMigrationTests(unittest.TestCase):
    def test_approval_migration_preserves_existing_users_and_orders_and_has_one_head(self):
        root = Path(__file__).resolve().parents[1]
        database_url = os.environ.get("DATLAEUFT_TEST_DATABASE_URL", "sqlite://")
        if make_url(database_url).get_backend_name() != "sqlite" and not (make_url(database_url).database or "").startswith("datlaeuft_test"):
            self.fail("Migrationstests benötigen eine separate datlaeuft_test-Datenbank.")
        engine = sa.create_engine(database_url)
        spec = importlib.util.spec_from_file_location("_approval_migration", root / "migrations/versions/a3c5e7f9b1d2_commission_approval.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        try:
            with engine.begin() as connection:
                metadata = sa.MetaData()
                users = sa.Table("user_account", metadata, sa.Column("id", sa.Integer(), primary_key=True))
                commissions = sa.Table("commission", metadata, sa.Column("id", sa.Integer(), primary_key=True))
                metadata.create_all(connection)
                connection.execute(users.insert().values(id=1))
                connection.execute(commissions.insert().values(id=1))
                with patch.object(migration, "op", Operations(MigrationContext.configure(connection))):
                    migration.upgrade()
                    self.assertEqual(connection.scalar(sa.text("SELECT approval_state FROM commission WHERE id=1")), "not_required")
                    self.assertIsNone(connection.scalar(sa.text("SELECT external_commission_approver_user_id FROM user_account WHERE id=1")))
                    self.assertEqual(len(sa.inspect(connection).get_foreign_keys("commission")), 2)
                    migration.downgrade()
                self.assertEqual(connection.scalar(sa.select(sa.func.count()).select_from(users)), 1)
                self.assertEqual(connection.scalar(sa.select(sa.func.count()).select_from(commissions)), 1)
                metadata.drop_all(connection)
        finally:
            engine.dispose()
        scripts = ScriptDirectory(str(root / "migrations"))
        self.assertEqual(len(scripts.get_heads()), 1)
        self.assertIn(migration.revision, {item.revision for item in scripts.walk_revisions()})


if __name__ == "__main__":
    unittest.main()
