import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

from flask import Flask
from flask_sqlalchemy import SQLAlchemy


class CommissionPushPreferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = SQLAlchemy()
        cls.app = Flask(__name__)
        cls.app.config.update(SQLALCHEMY_DATABASE_URI="sqlite://", TESTING=True)
        cls.db.init_app(cls.app)
        cls.package_name = "_push_service_test_app"
        package = types.ModuleType(cls.package_name)
        package.__path__ = [str(Path(__file__).resolve().parents[1] / "app")]
        extensions = types.ModuleType(cls.package_name + ".extensions")
        extensions.db = cls.db
        extensions.jwt = Mock()
        settings_module = types.ModuleType(cls.package_name + ".settings")
        setting_values = {
            "PUSH_MAX_ATTEMPTS": 3,
            "PUSH_RELAY_URL": "https://push.example.invalid",
            "PUSH_RELAY_API_KEY": "test-key",
            "PUSH_RELAY_TIMEOUT_SECONDS": 5,
        }
        settings_module.SettingKeys = types.SimpleNamespace(
            **{name: name for name in setting_values}
        )
        settings_module.settings = Mock()
        settings_module.settings.get.side_effect = setting_values.get
        cls.module_patch = patch.dict(sys.modules, {
            cls.package_name: package,
            extensions.__name__: extensions,
            settings_module.__name__: settings_module,
        })
        cls.module_patch.start()
        cls.addClassCleanup(cls.module_patch.stop)
        cls.models = importlib.import_module(cls.package_name + ".models")
        cls.push = importlib.import_module(cls.package_name + ".push_service")
        cls.tables = [model.__table__ for model in (
            cls.models.User, cls.models.Commission,
            cls.models.CommissionPosition, cls.models.BlacklistEntry,
            cls.models.PushDevice, cls.models.PushDelivery,
        )]

    def setUp(self):
        context = self.app.app_context()
        context.push()
        self.addCleanup(context.pop)
        self.addCleanup(self.db.session.remove)
        self.db.metadata.drop_all(self.db.engine, tables=self.tables)
        self.db.metadata.create_all(self.db.engine, tables=self.tables)
        self.user = self.models.User(
            email="craftsman@example.invalid", display_name="Testhandwerker",
        )
        self.commission = self.models.Commission(
            user=self.user, title="Wasserschaden", source="web",
            is_high_priority=True, priority_is_manual=True,
        )
        self.device = self.models.PushDevice(
            user=self.user, installation_id=str(uuid4()), device_token="a" * 64,
        )
        self.db.session.add_all([self.user, self.commission, self.device])
        self.db.session.commit()
        response = Mock(status_code=202)
        response.json.return_value = {"accepted": True}
        request_patch = patch.object(self.push.requests, "post", return_value=response)
        self.post = request_patch.start()
        self.addCleanup(request_patch.stop)

    def queue(self, event_type):
        batch = self.push.enqueue_commission_push(self.commission, event_type)
        self.db.session.commit()
        return batch

    def assert_delivery(self, batch, *, sent):
        self.assertEqual(
            self.push.process_push_batch(limit=1, batch_id=batch.batch_id), 1,
        )
        delivery = self.models.PushDelivery.query.filter_by(
            batch_id=batch.batch_id,
        ).one()
        expected_state = (
            self.models.PushDeliveryState.SUCCEEDED
            if sent else self.models.PushDeliveryState.DISCARDED
        )
        self.assertEqual(delivery.state, expected_state.value)
        if sent:
            self.post.assert_called_once()
            self.assertEqual(self.post.call_args.kwargs["json"]["payload"], delivery.payload)
        else:
            self.post.assert_not_called()
        return delivery

    def assert_new_commission_preferences(self, *, high_priority):
        event_type = self.models.PushEventType.NEW_COMMISSION
        priority_event = self.models.PushEventType.COMMISSION_PRIORITY_RAISED
        for use_event_preferences in (False, True):
            for new_enabled in (False, True):
                for priority_enabled in (False, True):
                    with self.subTest(
                        use_event_preferences=use_event_preferences,
                        new_enabled=new_enabled, priority_enabled=priority_enabled,
                    ):
                        self.post.reset_mock()
                        self.commission.is_high_priority = high_priority
                        if use_event_preferences:
                            self.user.push_new_commission_enabled = not new_enabled
                            self.user.push_changed_commission_enabled = not priority_enabled
                            self.user.push_event_preferences = {
                                event_type.value: new_enabled,
                                priority_event.value: priority_enabled,
                            }
                        else:
                            self.user.push_new_commission_enabled = new_enabled
                            self.user.push_changed_commission_enabled = priority_enabled
                            self.user.push_event_preferences = None
                        enabled = priority_enabled if high_priority else new_enabled
                        batch = self.queue(event_type)
                        self.assertEqual(batch.queued_count, int(enabled))
                        if not enabled:
                            self.assertEqual(self.models.PushDelivery.query.filter_by(
                                batch_id=batch.batch_id,
                            ).count(), 0)
                            continue
                        delivery = self.assert_delivery(batch, sent=True)
                        self.assertEqual(delivery.event_type, event_type.value)
                        self.assertEqual(delivery.payload["event_type"], event_type.value)
                        self.assertEqual(delivery.payload["is_high_priority"], high_priority)
                        expected_title = (
                            "Neuer Auftrag – hohe Priorität"
                            if high_priority else "Neuer Auftrag"
                        )
                        self.assertEqual(delivery.title, expected_title)
                        self.assertEqual(delivery.payload["aps"]["alert"]["title"], expected_title)

    def test_high_priority_new_commissions_use_priority_preferences(self):
        self.assert_new_commission_preferences(high_priority=True)

    def test_normal_new_commissions_use_new_commission_preferences(self):
        self.assert_new_commission_preferences(high_priority=False)

    def test_priority_preference_is_rechecked_before_delivery(self):
        self.user.push_event_preferences = {
            "new_commission": True, "commission_priority_raised": True,
        }
        batch = self.queue(self.models.PushEventType.NEW_COMMISSION)
        self.assertEqual(batch.queued_count, 1)
        self.user.push_event_preferences = {
            "new_commission": True, "commission_priority_raised": False,
        }
        self.db.session.commit()
        self.assert_delivery(batch, sent=False)

    def test_disabling_new_commission_push_does_not_discard_urgent_delivery(self):
        self.user.push_event_preferences = {
            "new_commission": True, "commission_priority_raised": True,
        }
        batch = self.queue(self.models.PushEventType.NEW_COMMISSION)
        self.assertEqual(batch.queued_count, 1)
        self.user.push_event_preferences = {
            "new_commission": False, "commission_priority_raised": True,
        }
        self.db.session.commit()
        self.assert_delivery(batch, sent=True)

    def test_delivery_uses_priority_recorded_in_the_notification(self):
        self.user.push_event_preferences = {
            "new_commission": False, "commission_priority_raised": True,
        }
        batch = self.queue(self.models.PushEventType.NEW_COMMISSION)
        self.assertEqual(batch.queued_count, 1)
        self.commission.is_high_priority = False
        self.db.session.commit()
        delivery = self.assert_delivery(batch, sent=True)
        self.assertTrue(delivery.payload["is_high_priority"])

    def test_normal_delivery_still_rechecks_new_commission_preference(self):
        self.commission.is_high_priority = False
        self.user.push_event_preferences = {
            "new_commission": True, "commission_priority_raised": True,
        }
        batch = self.queue(self.models.PushEventType.NEW_COMMISSION)
        self.assertEqual(batch.queued_count, 1)
        self.user.push_event_preferences = {
            "new_commission": False, "commission_priority_raised": True,
        }
        self.db.session.commit()
        self.assert_delivery(batch, sent=False)

    def test_priority_raise_still_uses_priority_preference(self):
        event_type = self.models.PushEventType.COMMISSION_PRIORITY_RAISED
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                self.post.reset_mock()
                self.user.push_event_preferences = {
                    "new_commission": not enabled, event_type.value: enabled,
                }
                batch = self.queue(event_type)
                self.assertEqual(batch.queued_count, int(enabled))
                if enabled:
                    delivery = self.assert_delivery(batch, sent=True)
                    self.assertEqual(delivery.title, "Auftrag jetzt mit hoher Priorität")

    def test_other_events_keep_their_preferences_for_high_priority_commissions(self):
        event_type = self.models.PushEventType.APPOINTMENT_ACCEPTED
        self.user.push_changed_commission_enabled = False
        self.user.push_event_preferences = {
            "new_commission": False, "commission_priority_raised": False,
            event_type.value: True,
        }
        batch = self.queue(event_type)
        self.assertEqual(batch.queued_count, 1)
        delivery = self.assert_delivery(batch, sent=True)
        self.assertEqual(delivery.title, "Termin angenommen")


if __name__ == "__main__":
    unittest.main()
