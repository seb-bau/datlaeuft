from datetime import datetime, timezone
import importlib
import unittest
from uuid import uuid4

import test_mobile_appointment_features as fixtures


class CompletedCommissionTimeTests(unittest.TestCase):
    headers = fixtures.MobileAppointmentFeaturesTests.headers
    commission = fixtures.MobileAppointmentFeaturesTests.commission
    start_patch = fixtures.MobileAppointmentFeaturesTests.start_patch

    @classmethod
    def setUpClass(cls):
        fixtures.MobileAppointmentFeaturesTests.setUpClass.__func__(cls)
        cls.sync = importlib.import_module(cls.package_name + ".mobile_sync")
        cls.app.register_blueprint(cls.sync.mobile_sync_bp)
        cls.tables.extend([cls.models.MobileSyncOperation.__table__, cls.models.MobileSyncDelivery.__table__])

    def setUp(self):
        fixtures.MobileAppointmentFeaturesTests.setUp(self)

    def set_allowed(self, value):
        self.settings.settings.set(self.settings.SettingKeys.TIME_ALLOW_ON_COMPLETED_COMMISSIONS, value)
        self.db.session.commit()

    def book(self, commission, **values):
        return self.client.post(f"/api/commissions/{commission.id}/time", headers=self.headers(), json={"minutes": 30, **values})

    def test_default_and_mobile_configuration_preserve_existing_behavior(self):
        self.assertTrue(self.settings.settings.get(self.settings.SettingKeys.TIME_ALLOW_ON_COMPLETED_COMMISSIONS))
        response = self.client.get("/api/mobile/config")
        self.assertTrue(response.json["allow_time_on_completed_commissions"])
        self.set_allowed(False)
        self.assertFalse(self.client.get("/api/mobile/config").json["allow_time_on_completed_commissions"])

    def test_disabling_blocks_new_entries_and_keeps_existing_entries_visible(self):
        commission = self.commission(local_status="done")
        first = self.book(commission)
        self.assertEqual(first.status_code, 201, first.data)
        self.set_allowed(False)
        rejected = self.book(commission, service_date="2020-01-01")
        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(rejected.json["code"], "completed_commission_time_disabled")
        self.assertIn("keine neuen Zeiten", rejected.json["message"])
        entries = self.client.get(f"/api/commissions/{commission.id}/time", headers=self.headers())
        self.assertEqual(entries.status_code, 200)
        self.assertEqual([item["id"] for item in entries.json["items"]], [first.json["item"]["id"]])
        self.assertEqual(self.models.TimeEntry.query.count(), 1)
        self.set_allowed(True)
        self.assertEqual(self.book(commission).status_code, 201)
        self.assertEqual(self.models.TimeEntry.query.count(), 2)

    def test_all_completed_states_obey_the_policy_for_regular_users_and_administrators(self):
        self.set_allowed(False)
        for administrator in (False, True):
            self.user.is_admin = administrator
            self.db.session.commit()
            for status in ("done", "awaiting_invoice", "tenant_charge_review"):
                with self.subTest(administrator=administrator, status=status):
                    commission = self.commission(local_status=status)
                    response = self.book(commission, work_minutes=10, waiting_minutes=5)
                    self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(self.models.TimeEntry.query.count(), 0)

    def test_open_and_reopened_orders_allow_booking_when_late_entries_are_disabled(self):
        self.set_allowed(False)
        for status in ("waiting_for_acceptance", "open", "in_progress", "completion_review"):
            with self.subTest(status=status):
                commission = self.commission(local_status=status, completed_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
                self.assertEqual(self.book(commission).status_code, 201)

    def test_existing_entry_updates_and_deletions_keep_their_existing_permissions(self):
        commission = self.commission(local_status="done")
        entry = self.book(commission).json["item"]
        self.set_allowed(False)
        response = self.client.patch(f"/api/commissions/{commission.id}/time/{entry['id']}", headers=self.headers(), json={"minutes": 45})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["item"]["minutes"], 45)
        response = self.client.delete(f"/api/commissions/{commission.id}/time/{entry['id']}", headers=self.headers())
        self.assertEqual(response.status_code, 200)

    def test_enabling_late_entries_does_not_bypass_colleague_permissions(self):
        self.set_allowed(True)
        commission = self.commission(user=self.other_user, local_status="done")
        self.user.visible_commission_users = [self.other_user]
        self.db.session.commit()
        self.assertEqual(self.client.get(f"/api/commissions/{commission.id}/time", headers=self.headers()).status_code, 200)
        self.assertEqual(self.book(commission).status_code, 403)

    def test_offline_replay_checks_the_current_policy_and_preserves_successful_receipts(self):
        commission = self.commission(local_status="done")
        operation = {"id": str(uuid4()), "path": f"/api/commissions/{commission.id}/time", "method": "POST", "body": {"minutes": 30}}
        first = self.client.post("/api/mobile/offline-operations", headers=self.headers(), json=operation)
        self.assertEqual(first.status_code, 200, first.data)
        self.set_allowed(False)
        replay = self.client.post("/api/mobile/offline-operations", headers=self.headers(), json=operation)
        self.assertEqual(replay.status_code, 200, replay.data)
        rejected = self.client.post("/api/mobile/offline-operations", headers=self.headers(), json={**operation, "id": str(uuid4())})
        self.assertEqual(rejected.status_code, 409, rejected.data)
        self.assertEqual(rejected.json["code"], "completed_commission_time_disabled")
        self.assertEqual(self.models.TimeEntry.query.count(), 1)
        self.assertEqual(self.models.MobileSyncOperation.query.count(), 1)
