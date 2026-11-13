import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock, patch

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from flask import Flask, render_template
from flask_sqlalchemy import SQLAlchemy
from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader
import requests
import sqlalchemy as sa
from sqlalchemy.engine import make_url


PHONE_A = "+4915123456789"
PHONE_B = "+491701234567"


class TenantSmsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.db = SQLAlchemy()
        cls.app = Flask(__name__)
        cls.app.config.update(SQLALCHEMY_DATABASE_URI="sqlite://", TESTING=True)
        cls.db.init_app(cls.app)
        cls.package_name = "_tenant_sms_test_app"
        package = types.ModuleType(cls.package_name)
        package.__path__ = [str(cls.root / "app")]
        extensions = types.ModuleType(cls.package_name + ".extensions")
        extensions.db = cls.db
        gateway = types.ModuleType(cls.package_name + ".wowiport")
        gateway.WowiportService = Mock()
        user_sync = types.ModuleType(cls.package_name + ".user_sync")
        user_sync.extract_craftsman_display_name = Mock(return_value="Handwerker")
        modules = {cls.package_name: package, extensions.__name__: extensions, gateway.__name__: gateway, user_sync.__name__: user_sync}
        module_patch = patch.dict(sys.modules, modules)
        module_patch.start()
        cls.addClassCleanup(module_patch.stop)
        cls.models = importlib.import_module(cls.package_name + ".models")
        cls.settings_module = importlib.import_module(cls.package_name + ".settings")
        cls.mail = importlib.import_module(cls.package_name + ".tenant_notifications")
        cls.sms = importlib.import_module(cls.package_name + ".tenant_sms_notifications")
        cls.provider = importlib.import_module(cls.package_name + ".sms_service")
        cls.gateway = gateway
        cls.tables = [model.__table__ for model in (
            cls.models.AppSetting, cls.models.User, cls.models.ExternalCraftsman,
            cls.models.CraftActivity, cls.models.Commission, cls.models.CommissionPosition,
            cls.models.CommissionTenantNotification, cls.models.CommissionTenantSmsNotification,
            cls.models.CommissionTicketLink, cls.models.TicketMirror,
        )]
        cls.app.jinja_loader = ChoiceLoader([
            DictLoader({"base.html": "{% block content %}{% endblock %}"}),
            FileSystemLoader(str(cls.root / "app" / "templates")),
        ])
        cls.app.jinja_env.globals["csrf_token"] = lambda: "test-token"
        cls.app.add_url_rule("/templates", endpoint="admin.email_templates_page", view_func=lambda: "")

    def setUp(self):
        context = self.app.app_context()
        context.push()
        self.addCleanup(context.pop)
        self.addCleanup(self.db.session.remove)
        self.db.metadata.drop_all(self.db.engine, tables=self.tables)
        self.db.metadata.create_all(self.db.engine, tables=self.tables)
        self.settings = self.settings_module.settings
        self.keys = self.settings_module.SettingKeys
        for definition, value in (
            (self.keys.SMS_ENABLED, True), (self.keys.SMS_API_KEY, "test-seven-key"),
            (self.keys.SMS_SENDER, "Wohnbau"), (self.keys.SMTP_HOST, "smtp.example.invalid"),
            (self.keys.SMTP_SENDER, "app@example.invalid"),
        ):
            self.settings.set(definition, value)
        self.craftsman = self.models.User(email="craftsman@example.invalid", display_name="Installateur", craftsman_erp_id=2020)
        self.db.session.add_all([
            self.craftsman, self.models.CraftActivity(erp_id=500, name="Sanitär"),
            self.models.AppSetting(key="commissions.tenant_notifications_start_at", value_text="2020-01-01T00:00:00+00:00",
                                   value_type="string", label="Beginn", group="Test", is_secret=False),
        ])
        self.db.session.commit()
        self.service = Mock()
        self.service.client.get_contractors.return_value = [self.contractor()]
        self.gateway.WowiportService.return_value = self.service
        self.mail_sender = self.start_patch(patch.object(self.mail, "send_email", return_value=None))
        self.sms_sender = self.start_patch(patch.object(self.sms, "send_sms", side_effect=self.accept_sms))
        self.start_patch(patch.object(self.mail, "current_tenant_date", return_value=date(2026, 11, 13)))
        self.get = self.start_patch(patch.object(self.provider.requests, "get"))
        self.post = self.start_patch(patch.object(self.provider.requests, "post"))
        self.get.return_value = self.response({"success": True, "network_type": "mobile", "international": PHONE_A})
        self.post.return_value = self.response({"success": "100", "debug": "false", "messages": [{"id": "sms-1", "success": True, "recipient": PHONE_A}]})
        self.next_erp_id = 99

    def start_patch(self, value):
        result = value.start()
        self.addCleanup(value.stop)
        return result

    def response(self, payload, status=200):
        value = Mock(status_code=status)
        value.json.return_value = payload
        if status >= 400:
            value.raise_for_status.side_effect = requests.HTTPError("Providerfehler")
        return value

    def contractor(self, phone=PHONE_A, email="tenant@example.invalid", **values):
        communications = []
        if phone:
            communications.append({"CommunicationType": {"Id": 3}, "Content": phone})
        if email:
            communications.append({"CommunicationType": {"Id": 5}, "Content": email})
        return {"Person": {"Communications": communications}, **values}

    def commission(self, **values):
        self.next_erp_id += 1
        defaults = {"source": "web", "notify_tenants": True, "erp_id": self.next_erp_id,
                    "erp_idnum": f"A-{self.next_erp_id}", "license_agreement_id": 4004,
                    "craftsman_erp_id": 2020, "erp_sync_state": "synced", "status_id": 12,
                    "recording_date": datetime(2026, 11, 13, tzinfo=timezone.utc)}
        defaults.update(values)
        item = self.models.Commission(**defaults)
        item.positions.append(self.models.CommissionPosition(position_index=0, craft_activity_erp_id=500, commission_text="Reparieren", units=1))
        self.db.session.add(item)
        self.db.session.commit()
        return item

    def enqueue(self, **values):
        commission = self.commission(**values)
        self.mail.ensure_tenant_notifications()
        self.db.session.commit()
        return commission, self.models.CommissionTenantSmsNotification.query.filter_by(commission_id=commission.id).one_or_none()

    def accept_sms(self, number, text, *, foreign_id=None, before_send=None):
        if before_send:
            before_send()
        return self.provider.SmsResult("sent", "sms-" + number[-3:])

    def test_sms_and_email_are_created_once_and_sent_independently(self):
        commission, item = self.enqueue()
        self.assertEqual(self.mail.ensure_tenant_notifications(), 0)
        self.assertEqual(self.models.CommissionTenantSmsNotification.query.count(), 1)
        self.assertEqual(self.mail.process_tenant_notification_batch(), 1)
        self.assertEqual(self.sms.process_tenant_sms_notification_batch(), 1)
        self.assertEqual(self.models.CommissionTenantNotification.query.one().state, "sent")
        self.assertEqual(item.state, "sent")
        self.assertIsNotNone(item.sent_at)
        self.assertEqual(self.sms.process_tenant_sms_notification_batch(), 0)
        self.sms_sender.assert_called_once()
        self.mail_sender.assert_called_once()
        text = self.sms_sender.call_args.args[1]
        for value in ("A-100", "Installateur", "Sanitär"):
            self.assertIn(value, text)
        self.assertNotIn("<", text)

    def test_smtp_failure_does_not_block_sms(self):
        commission, item = self.enqueue()
        self.mail_sender.side_effect = RuntimeError("SMTP nicht erreichbar")
        with self.assertLogs(self.app.logger, level="ERROR"):
            self.mail.process_tenant_notification_batch()
        self.sms.process_tenant_sms_notification_batch()
        self.assertEqual(self.models.CommissionTenantNotification.query.one().state, "failed")
        self.assertEqual(item.state, "sent")

    def test_the_worker_submits_through_the_real_provider_client_with_a_saved_submission_marker(self):
        commission, item = self.enqueue()
        self.sms_sender.side_effect = self.provider.send_sms
        response = self.post.return_value
        def submit(*args, **values):
            self.assertEqual(item.payload["submitting"], PHONE_A)
            self.assertEqual(values["json"]["to"], PHONE_A)
            self.assertIn("A-100", values["json"]["text"])
            self.assertTrue(values["json"]["foreign_id"].startswith("datlaeuft-"))
            return response
        self.post.side_effect = submit
        self.sms.process_tenant_sms_notification_batch()
        self.assertEqual(item.state, "sent")
        self.assertIsNone(item.payload["submitting"])
        self.assertEqual(item.payload["results"][PHONE_A]["message_id"], "sms-1")
        self.post.assert_called_once()

    def test_sms_is_sent_without_an_email_address_or_smtp_configuration(self):
        self.service.client.get_contractors.return_value = [self.contractor(email=None)]
        self.settings.set(self.keys.SMTP_HOST, "")
        self.settings.set(self.keys.SMTP_SENDER, "")
        commission, item = self.enqueue()
        self.mail.process_tenant_notification_batch()
        self.sms.process_tenant_sms_notification_batch()
        self.assertEqual(self.models.CommissionTenantNotification.query.one().state, "no_recipients")
        self.assertEqual(item.state, "sent")
        self.mail_sender.assert_not_called()

    def test_sms_failure_does_not_repeat_a_sent_email(self):
        commission, item = self.enqueue()
        self.sms_sender.side_effect = self.provider.SmsSendError("seven Guthaben fehlt")
        self.mail.process_tenant_notification_batch()
        with self.assertLogs(self.app.logger, level="ERROR"):
            self.sms.process_tenant_sms_notification_batch()
        self.assertEqual(item.state, "failed")
        item.available_at = self.models.utc_now() - timedelta(seconds=1)
        self.db.session.commit()
        self.sms_sender.side_effect = self.accept_sms
        self.assertEqual(self.mail.process_tenant_notification_batch(), 0)
        self.sms.process_tenant_sms_notification_batch()
        self.assertEqual(item.state, "sent")
        self.mail_sender.assert_called_once()

    def test_global_disable_does_not_queue_sms_or_backfill_old_email_notifications(self):
        self.settings.set(self.keys.SMS_ENABLED, False)
        commission, item = self.enqueue()
        self.assertIsNone(item)
        self.mail.process_tenant_notification_batch()
        self.settings.set(self.keys.SMS_ENABLED, True)
        self.db.session.commit()
        self.assertEqual(self.mail.ensure_tenant_notifications(), 0)
        self.assertEqual(self.sms.process_tenant_sms_notification_batch(), 0)
        self.sms_sender.assert_not_called()

    def test_debug_email_allows_email_but_never_queues_or_backfills_sms(self):
        self.settings.set(self.keys.SMTP_DEBUG_RECIPIENT, "debug@example.invalid")
        commission, item = self.enqueue()
        self.assertIsNone(item)
        self.assertEqual(self.mail.process_tenant_notification_batch(), 1)
        self.assertEqual(self.models.CommissionTenantNotification.query.one().state, "sent")
        self.assertEqual(self.sms.process_tenant_sms_notification_batch(), 0)
        self.settings.set_from_form(self.keys.SMTP_DEBUG_RECIPIENT, "")
        self.db.session.commit()
        self.assertEqual(self.mail.ensure_tenant_notifications(), 0)
        self.assertEqual(self.sms.process_tenant_sms_notification_batch(), 0)
        self.assertEqual(self.models.CommissionTenantSmsNotification.query.count(), 0)
        self.sms_sender.assert_not_called()
        self.get.assert_not_called()
        self.post.assert_not_called()

    def test_debug_email_discards_pending_failed_and_interrupted_sms_even_when_sms_are_disabled(self):
        items = []
        for state in ("pending", "failed", "sending"):
            commission, item = self.enqueue()
            item.state = state
            item.locked_at = self.models.utc_now() - timedelta(minutes=20)
            items.append(item)
        self.db.session.commit()
        self.settings.set(self.keys.SMS_ENABLED, False)
        self.settings.set(self.keys.SMTP_DEBUG_RECIPIENT, "debug@example.invalid")
        self.db.session.commit()
        self.assertEqual(self.sms.process_tenant_sms_notification_batch(), 0)
        for item in items:
            self.assertEqual(item.state, "discarded")
            self.assertIsNone(item.locked_at)
        self.settings.set(self.keys.SMTP_DEBUG_RECIPIENT, "")
        self.settings.set(self.keys.SMS_ENABLED, True)
        self.db.session.commit()
        self.assertEqual(self.sms.process_tenant_sms_notification_batch(), 0)
        self.sms_sender.assert_not_called()
        self.get.assert_not_called()
        self.post.assert_not_called()
        commission, item = self.enqueue()
        self.assertIsNotNone(item)
        self.assertEqual(self.sms.process_tenant_sms_notification_batch(), 1)
        self.assertEqual(item.state, "sent")

    def test_debug_toggle_without_a_worker_run_permanently_blocks_existing_and_delayed_jobs(self):
        commission, item = self.enqueue()
        self.settings.set(self.keys.SMTP_DEBUG_RECIPIENT, "debug@example.invalid")
        self.db.session.commit()
        delayed = self.commission(erp_sync_state="pending")
        self.settings.set_from_form(self.keys.SMTP_DEBUG_RECIPIENT, "")
        self.db.session.commit()
        self.db.session.expire_all()
        delayed.erp_sync_state = "synced"
        self.db.session.commit()
        self.assertEqual(self.mail.ensure_tenant_notifications(), 1)
        self.db.session.commit()
        self.assertIsNone(self.models.CommissionTenantSmsNotification.query.filter_by(commission_id=delayed.id).one_or_none())
        self.assertEqual(self.sms.process_tenant_sms_notification_batch(), 0)
        self.assertEqual(item.state, "discarded")
        self.sms_sender.assert_not_called()
        self.get.assert_not_called()
        self.post.assert_not_called()

    def test_debug_address_already_present_before_the_sms_feature_blocks_all_gateway_requests(self):
        self.db.session.add(self.models.AppSetting(
            key=self.keys.SMTP_DEBUG_RECIPIENT.key, value_text="debug@example.invalid",
            value_type="string", label="Debug", group="E-Mail", is_secret=False,
        ))
        self.db.session.commit()
        with self.assertRaises(self.provider.SmsSuppressed):
            self.provider.send_sms(PHONE_A, "Beauftragung")
        self.get.assert_not_called()
        self.post.assert_not_called()

    def test_debug_activation_during_lookup_stops_submission_and_discards_the_job(self):
        commission, item = self.enqueue()
        self.sms_sender.side_effect = self.provider.send_sms
        response = self.get.return_value
        def lookup(*args, **values):
            self.settings.set(self.keys.SMTP_DEBUG_RECIPIENT, "debug@example.invalid")
            self.db.session.commit()
            return response
        self.get.side_effect = lookup
        self.assertEqual(self.sms.process_tenant_sms_notification_batch(), 1)
        self.assertEqual(item.state, "discarded")
        self.post.assert_not_called()
        self.settings.set(self.keys.SMTP_DEBUG_RECIPIENT, "")
        self.db.session.commit()
        self.assertEqual(self.sms.process_tenant_sms_notification_batch(), 0)
        self.post.assert_not_called()

    def test_a_debug_toggle_during_lookup_still_blocks_submission_after_the_address_is_cleared(self):
        commission, item = self.enqueue()
        self.sms_sender.side_effect = self.provider.send_sms
        response = self.get.return_value
        def lookup(*args, **values):
            self.settings.set(self.keys.SMTP_DEBUG_RECIPIENT, "debug@example.invalid")
            self.settings.set(self.keys.SMTP_DEBUG_RECIPIENT, "")
            self.db.session.commit()
            return response
        self.get.side_effect = lookup
        self.sms.process_tenant_sms_notification_batch()
        self.assertEqual(item.state, "discarded")
        self.assertIsNone(item.payload["submitting"])
        self.post.assert_not_called()
        self.assertEqual(self.sms.process_tenant_sms_notification_batch(), 0)

    def test_sms_failure_handling_cannot_requeue_a_debug_suppressed_job(self):
        commission, item = self.enqueue()
        item_id = self.sms.claim_tenant_sms_notification()
        self.settings.set(self.keys.SMTP_DEBUG_RECIPIENT, "debug@example.invalid")
        self.settings.set(self.keys.SMTP_DEBUG_RECIPIENT, "")
        self.db.session.commit()
        self.sms.fail_tenant_sms_notification(item_id, self.provider.SmsSendError("Fehler"))
        self.assertEqual(item.state, "discarded")
        self.assertEqual(self.sms.process_tenant_sms_notification_batch(), 0)
        self.sms_sender.assert_not_called()

    def test_global_disable_pauses_already_queued_sms(self):
        commission, item = self.enqueue()
        self.settings.set(self.keys.SMS_ENABLED, False)
        self.db.session.commit()
        self.assertEqual(self.sms.process_tenant_sms_notification_batch(), 0)
        self.assertEqual(item.state, "pending")
        self.settings.set(self.keys.SMS_ENABLED, True)
        self.db.session.commit()
        self.sms.process_tenant_sms_notification_batch()
        self.assertEqual(item.state, "sent")

    def test_opt_out_contractless_and_unsynced_orders_do_not_queue_notifications(self):
        for values in ({"notify_tenants": False}, {"license_agreement_id": None}, {"erp_sync_state": "pending"}, {"status_id": 9}):
            self.commission(**values)
        self.assertEqual(self.mail.ensure_tenant_notifications(), 0)
        self.assertEqual(self.models.CommissionTenantSmsNotification.query.count(), 0)

    def test_opt_out_after_queuing_discards_the_sms(self):
        commission, item = self.enqueue()
        commission.notify_tenants = False
        self.db.session.commit()
        self.sms.process_tenant_sms_notification_batch()
        self.assertEqual(item.state, "discarded")
        self.sms_sender.assert_not_called()

    def test_current_tenants_are_deduplicated_and_former_or_future_tenants_are_excluded(self):
        self.service.client.get_contractors.return_value = [
            self.contractor(phone="0151 23456789"), self.contractor(),
            self.contractor(phone=PHONE_B, email="former@example.invalid", ContractualUseValidTo="2026-11-12"),
            self.contractor(phone=PHONE_B, email="future@example.invalid", ContractualUseValidFrom="2026-11-14"),
            self.contractor(phone=PHONE_B, email="invalid@example.invalid", ContractualUseValidFrom="ungültig"),
            self.contractor(phone=PHONE_B, email="invalid-number@example.invalid", ContractualUseValidFrom=0),
        ]
        commission = self.commission()
        self.assertEqual(self.sms.tenant_phone_numbers(commission, self.service), [PHONE_A])
        self.assertEqual(self.mail.tenant_email_addresses(commission, self.service), ["tenant@example.invalid"])
        self.assertTrue(self.service.client.get_contractors.call_args.kwargs["fetch_all"])

    def test_contractor_date_boundaries_and_preferred_mobile_contact_are_supported(self):
        self.service.client.get_contractors.return_value = [{
            "valid_from": "2026-11-13", "valid_to": "2026-11-13",
            "Person": {"first_mobile_phone_communication": {"Content": PHONE_B}, "communications": []},
        }]
        self.assertEqual(self.sms.tenant_phone_numbers(self.commission(), self.service), [PHONE_B])

    def test_partial_failures_retry_only_the_failed_phone_number(self):
        self.service.client.get_contractors.return_value = [self.contractor(), self.contractor(phone=PHONE_B)]
        commission, item = self.enqueue()
        def first_attempt(number, text, **values):
            values["before_send"]()
            if number == PHONE_B:
                raise self.provider.SmsSendError("seven hat abgelehnt")
            return self.provider.SmsResult("sent", "sms-a")
        self.sms_sender.side_effect = first_attempt
        with self.assertLogs(self.app.logger, level="ERROR"):
            self.sms.process_tenant_sms_notification_batch()
        self.assertEqual(item.state, "failed")
        self.assertEqual(item.payload["results"][PHONE_A]["state"], "sent")
        item.available_at = self.models.utc_now() - timedelta(seconds=1)
        self.db.session.commit()
        self.sms_sender.reset_mock()
        self.sms_sender.side_effect = self.accept_sms
        self.sms.process_tenant_sms_notification_batch()
        self.assertEqual(item.state, "sent")
        self.assertEqual(self.sms_sender.call_args.args[0], PHONE_B)
        self.sms_sender.assert_called_once()

    def test_an_uncertain_submission_is_not_retried_but_other_recipients_are_attempted(self):
        self.service.client.get_contractors.return_value = [self.contractor(), self.contractor(phone=PHONE_B)]
        commission, item = self.enqueue()
        def submit(number, text, **values):
            values["before_send"]()
            if number == PHONE_A:
                raise self.provider.SmsDeliveryUncertain("Unklare Übergabe")
            return self.provider.SmsResult("sent", "sms-b")
        self.sms_sender.side_effect = submit
        with self.assertLogs(self.app.logger, level="WARNING"):
            self.sms.process_tenant_sms_notification_batch()
        self.assertEqual(item.state, "uncertain")
        self.assertEqual(item.payload["results"][PHONE_B]["state"], "sent")
        self.assertEqual(self.sms.process_tenant_sms_notification_batch(), 0)
        self.assertEqual(self.sms_sender.call_count, 2)

    def test_stale_inflight_messages_are_not_resent_and_unsent_numbers_continue(self):
        self.service.client.get_contractors.return_value = [self.contractor(), self.contractor(phone=PHONE_B)]
        commission, item = self.enqueue()
        item.payload = {"recipients": [PHONE_A, PHONE_B], "text": "Beauftragung", "results": {}, "submitting": PHONE_A}
        item.state = "sending"
        item.locked_at = self.models.utc_now() - timedelta(minutes=16)
        self.db.session.commit()
        with self.assertLogs(self.app.logger, level="WARNING"):
            self.sms.process_tenant_sms_notification_batch()
        self.assertEqual(item.state, "uncertain")
        self.sms_sender.assert_called_once()
        self.assertEqual(self.sms_sender.call_args.args[0], PHONE_B)

    def test_recovery_after_a_committed_receipt_finishes_without_resending(self):
        commission, item = self.enqueue()
        item.payload = {"recipients": [PHONE_A], "text": "Beauftragung", "results": {PHONE_A: {"state": "sent"}}, "submitting": None}
        item.state = "sending"
        item.locked_at = self.models.utc_now() - timedelta(minutes=16)
        self.db.session.commit()
        self.sms.process_tenant_sms_notification_batch()
        self.assertEqual(item.state, "sent")
        self.sms_sender.assert_not_called()

    def test_a_recipient_who_left_during_retries_is_not_contacted(self):
        commission, item = self.enqueue()
        item.payload = {"recipients": [PHONE_A], "text": "Beauftragung", "results": {PHONE_A: {"state": "failed"}}, "submitting": None}
        self.service.client.get_contractors.return_value = [self.contractor(ContractualUseValidTo="2026-11-12")]
        self.db.session.commit()
        self.sms.process_tenant_sms_notification_batch()
        self.assertEqual(item.state, "no_recipients")
        self.sms_sender.assert_not_called()

    def test_simulation_is_not_recorded_as_live_delivery(self):
        commission, item = self.enqueue()
        self.sms_sender.side_effect = lambda *args, **kwargs: self.provider.SmsResult("simulated")
        self.sms.process_tenant_sms_notification_batch()
        self.assertEqual(item.state, "simulated")
        self.assertIsNone(item.sent_at)

    def test_provider_normalizes_and_confirms_the_exact_mobile_number(self):
        before = Mock()
        result = self.provider.send_sms("0151 / 23456789", "Beauftragung", foreign_id="assignment-1", before_send=before)
        self.assertEqual(result.state, "sent")
        self.assertEqual(result.message_id, "sms-1")
        before.assert_called_once()
        self.get.assert_called_once_with("https://gateway.seven.io/api/lookup/format", params={"number": PHONE_A}, headers={"X-Api-Key": "test-seven-key", "Accept": "application/json"}, timeout=10)
        self.assertEqual(self.post.call_args.kwargs["json"], {"to": PHONE_A, "text": "Beauftragung", "from": "Wohnbau", "foreign_id": "assignment-1"})

    def test_provider_never_sends_to_invalid_fixed_line_or_group_inputs(self):
        for number in ("Hausmeister", "+492311234567", PHONE_A + "," + PHONE_B, "0151 23456789 ext 2"):
            with self.subTest(number=number):
                self.assertEqual(self.provider.send_sms(number, "Beauftragung").state, "skipped")
        self.get.assert_not_called()
        self.post.assert_not_called()

    def test_lookup_mismatch_or_nonmobile_result_prevents_sms_submission(self):
        self.get.return_value = self.response({"success": True, "network_type": "fixed_line", "international": PHONE_A})
        self.assertEqual(self.provider.send_sms(PHONE_A, "Beauftragung").state, "skipped")
        self.get.return_value = self.response({"success": True, "network_type": "mobile", "international": PHONE_B})
        with self.assertRaises(self.provider.SmsSendError):
            self.provider.send_sms(PHONE_A, "Beauftragung")
        self.post.assert_not_called()

    def test_disabled_or_unconfigured_provider_never_calls_the_gateway(self):
        self.settings.set(self.keys.SMS_ENABLED, False)
        with self.assertRaises(self.provider.SmsDisabled):
            self.provider.send_sms(PHONE_A, "Beauftragung")
        self.settings.set(self.keys.SMS_ENABLED, True)
        self.settings.set(self.keys.SMS_API_KEY, "")
        with self.assertRaises(self.provider.SmsSendError):
            self.provider.send_sms(PHONE_A, "Beauftragung")
        self.get.assert_not_called()
        self.post.assert_not_called()

    def test_sms_can_be_disabled_between_lookup_and_submission(self):
        def disable():
            self.settings.set(self.keys.SMS_ENABLED, False)
            self.db.session.commit()
        with self.assertRaises(self.provider.SmsDisabled):
            self.provider.send_sms(PHONE_A, "Beauftragung", before_send=disable)
        self.post.assert_not_called()

    def test_post_timeouts_are_uncertain_and_are_not_automatically_replayed(self):
        self.post.side_effect = requests.ReadTimeout("test-seven-key")
        with self.assertRaises(self.provider.SmsDeliveryUncertain) as error:
            self.provider.send_sms(PHONE_A, "Beauftragung")
        self.post.assert_called_once()
        self.assertNotIn("test-seven-key", str(error.exception))

    def test_gateway_rejections_simulation_and_incomplete_receipts_are_distinguished(self):
        for payload, expected in (
            ({"success": "500"}, self.provider.SmsSendError),
            ({"success": "100", "messages": []}, self.provider.SmsDeliveryUncertain),
            ({"success": "101", "messages": [{"success": False, "recipient": PHONE_A}]}, self.provider.SmsSendError),
        ):
            with self.subTest(payload=payload):
                self.post.return_value = self.response(payload)
                with self.assertRaises(expected):
                    self.provider.send_sms(PHONE_A, "Beauftragung")
        self.post.return_value = self.response({"success": "100", "debug": "true"})
        self.assertEqual(self.provider.send_sms(PHONE_A, "Beauftragung").state, "simulated")

    def test_http_errors_and_bad_json_never_become_successful_sms_receipts(self):
        for status, expected in ((401, self.provider.SmsSendError), (500, self.provider.SmsDeliveryUncertain)):
            self.post.return_value = self.response({}, status)
            with self.subTest(status=status), self.assertRaises(expected):
                self.provider.send_sms(PHONE_A, "Beauftragung")
        self.post.return_value = self.response({})
        self.post.return_value.json.side_effect = ValueError("Keine JSON-Antwort")
        with self.assertRaises(self.provider.SmsDeliveryUncertain):
            self.provider.send_sms(PHONE_A, "Beauftragung")
        self.post.reset_mock()
        self.get.side_effect = requests.ReadTimeout()
        with self.assertRaises(self.provider.SmsSendError):
            self.provider.send_sms(PHONE_A, "Beauftragung")
        self.post.assert_not_called()

    def test_global_settings_keep_the_api_key_secret_and_have_no_profile_options(self):
        self.assertFalse(self.keys.SMS_ENABLED.default)
        self.settings.set_from_form(self.keys.SMS_API_KEY, "")
        self.assertEqual(self.settings.get(self.keys.SMS_API_KEY), "test-seven-key")
        row = self.models.AppSetting.query.filter_by(key="sms.api_key").one()
        self.assertTrue(row.is_secret)
        definitions = self.settings_module.grouped_definitions()["SMS"]
        self.assertEqual({item.key for item in definitions}, {"sms.enabled", "sms.api_key", "sms.sender"})
        with self.app.test_request_context():
            html = render_template("admin/settings.html", groups={"SMS": definitions}, settings=self.settings)
        self.assertIn('type="password"', html)
        self.assertIn('name="sms.enabled"', html)
        self.assertNotIn("test-seven-key", html)
        with self.assertRaises(ValueError):
            self.settings.set(self.keys.SMS_API_KEY, "key\nvalue")


class TenantSmsMigrationTests(unittest.TestCase):
    def test_sms_queue_migration_preserves_existing_commissions_and_enforces_uniqueness(self):
        root = Path(__file__).resolve().parents[1]
        database_url = os.environ.get("DATLAEUFT_TEST_DATABASE_URL", "sqlite://")
        url = make_url(database_url)
        if url.get_backend_name() != "sqlite" and not (url.database or "").startswith("datlaeuft_test"):
            self.fail("Migrationstests benötigen eine separate datlaeuft_test-Datenbank.")
        engine = sa.create_engine(database_url)
        self.addCleanup(engine.dispose)
        spec = importlib.util.spec_from_file_location("_tenant_sms_migration", root / "migrations/versions/e5f7a9b1c3d5_tenant_commission_sms_notifications.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with engine.begin() as connection:
            metadata = sa.MetaData()
            commission = sa.Table("commission", metadata, sa.Column("id", sa.Integer(), primary_key=True))
            metadata.create_all(connection)
            connection.execute(commission.insert().values(id=1))
            with patch.object(migration, "op", Operations(MigrationContext.configure(connection))):
                migration.upgrade()
                table = sa.Table("commission_tenant_sms_notification", sa.MetaData(), autoload_with=connection)
                payload = {"results": {PHONE_A: {"state": "sent"}}}
                stored_payload = payload if isinstance(table.c.payload.type, sa.JSON) else json.dumps(payload)
                connection.execute(table.insert().values(commission_id=1, available_at=datetime.now(timezone.utc), payload=stored_payload))
                self.assertEqual(connection.scalar(sa.select(table.c.state)), "pending")
                read_payload = connection.scalar(sa.select(table.c.payload))
                self.assertEqual(json.loads(read_payload) if isinstance(read_payload, str) else read_payload, payload)
                with self.assertRaises(sa.exc.IntegrityError), connection.begin_nested():
                    connection.execute(table.insert().values(commission_id=1, available_at=datetime.now(timezone.utc)))
                self.assertIn("ix_tenant_sms_notification_due", {item["name"] for item in sa.inspect(connection).get_indexes(table.name)})
                migration.downgrade()
                self.assertNotIn(table.name, sa.inspect(connection).get_table_names())
                self.assertEqual(connection.scalar(sa.select(sa.func.count()).select_from(commission)), 1)
            commission.drop(connection)
        self.assertEqual(ScriptDirectory(str(root / "migrations")).get_heads(), [migration.revision])


if __name__ == "__main__":
    unittest.main()
