import ast
import importlib
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from flask import Flask
from flask_sqlalchemy import SQLAlchemy


class CommissionDateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = SQLAlchemy()
        cls.app = Flask(__name__)
        cls.app.config.update(SQLALCHEMY_DATABASE_URI="sqlite://", TESTING=True)
        cls.db.init_app(cls.app)
        cls.package_name = "_commission_dates_test_app"
        source_directory = Path(__file__).resolve().parents[1] / "app"
        package = types.ModuleType(cls.package_name)
        package.__path__ = [str(source_directory)]
        extensions = types.ModuleType(cls.package_name + ".extensions")
        extensions.db = cls.db
        cls.modules = {cls.package_name: package, extensions.__name__: extensions}
        source_names = ("wowiport", "erp_sync", "commission_workflow_service")
        for source_name in source_names:
            source = ast.parse((source_directory / (source_name + ".py")).read_text())
            for node in ast.walk(source):
                if not isinstance(node, ast.ImportFrom) or node.level != 1:
                    continue
                if node.module in {"models", "extensions", "utils", *source_names}:
                    continue
                module_name = cls.package_name + "." + node.module
                module = cls.modules.setdefault(module_name, types.ModuleType(module_name))
                for item in node.names:
                    setattr(module, item.name, Mock())
        cls.modules[cls.package_name + ".settings"].settings.get.return_value = False
        module_patch = patch.dict(sys.modules, cls.modules)
        module_patch.start()
        cls.addClassCleanup(module_patch.stop)
        cls.models = importlib.import_module(cls.package_name + ".models")
        sys.modules["app.models"] = cls.models
        cls.wowiport = importlib.import_module(cls.package_name + ".wowiport")
        cls.outbox = importlib.import_module(cls.package_name + ".erp_sync")
        cls.workflow = importlib.import_module(cls.package_name + ".commission_workflow_service")
        cls.tables = [model.__table__ for model in (
            cls.models.User, cls.models.ExternalCraftsman, cls.models.Commission,
            cls.models.CommissionPosition, cls.models.ErpOutboxOperation,
        )]

    def setUp(self):
        context = self.app.app_context()
        context.push()
        self.addCleanup(context.pop)
        self.addCleanup(self.db.session.remove)
        self.db.metadata.drop_all(self.db.engine, tables=self.tables)
        self.db.metadata.create_all(self.db.engine, tables=self.tables)
        self.now = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
        self.previous_date = datetime(2026, 11, 5, 10, tzinfo=timezone.utc)
        self.user = self.models.User(email="dates@example.invalid", display_name="Testbenutzer")
        self.db.session.add(self.user)
        self.db.session.commit()

    def commission(self, **values):
        defaults = {
            "erp_id": 100, "erp_idnum": "A-100", "status_id": 7,
            "status_code": "Accepted", "local_status": "in_progress",
            "status_changed_at": self.previous_date,
        }
        defaults.update(values)
        commission = self.models.Commission(**defaults)
        self.db.session.add(commission)
        self.db.session.commit()
        return commission

    def synchronize(self, **values):
        payload = {
            "Id": 100, "IdNum": "A-100", "RecordingDate": "2026-10-01",
            "CommissionStatus": {"Id": 7, "Code": "Accepted"},
            "AcceptanceDate": "2026-11-05",
        }
        payload.update(values)
        with patch.object(self.wowiport, "utc_now", return_value=self.now):
            commission = self.wowiport.upsert_commission(payload)
            self.db.session.commit()
        return commission

    def assert_status_date(self, commission, expected):
        actual = commission.status_changed_at
        self.assertEqual(actual.replace(tzinfo=timezone.utc) if actual else None, expected)

    def test_import_uses_the_date_for_the_current_erp_status(self):
        for status_id, status_code, date_field in (
            (12, "WaitForCraftsmanFeedback", "PlacingDate"),
            (7, "Accepted", "AcceptanceDate"),
            (9, "Done", "CompletionDate"),
            (11, "AwaitInvoice", "CompletionDate"),
        ):
            with self.subTest(status=status_code):
                commission = self.synchronize(**{
                    "Id": status_id,
                    "CommissionStatus": {"Id": status_id, "Code": status_code},
                    date_field: "2026-11-06",
                })
                self.assert_status_date(commission, datetime(2026, 11, 6, tzinfo=timezone.utc))
                self.assertEqual(commission.recording_date.date().isoformat(), "2026-10-01")

    def test_synchronizing_unchanged_status_preserves_the_original_timestamp(self):
        commission = self.commission()
        self.synchronize(Description="Geänderter Auftragstext")
        self.assert_status_date(commission, self.previous_date)
        self.assertEqual(commission.synced_at.replace(tzinfo=timezone.utc), self.now)

    def test_legacy_status_date_is_filled_from_erp_without_inventing_a_date(self):
        commission = self.commission(status_changed_at=None)
        self.synchronize()
        self.assert_status_date(commission, datetime(2026, 11, 5, tzinfo=timezone.utc))
        unknown = self.synchronize(Id=200, CommissionStatus={"Id": 10, "Code": "Canceled"})
        self.assertIsNone(unknown.status_changed_at)

    def test_changed_status_uses_erp_date_or_the_time_the_change_is_observed(self):
        commission = self.commission()
        self.synchronize(CommissionStatus={"Id": 9, "Code": "Done"}, CompletionDate="2026-11-10")
        self.assert_status_date(commission, datetime(2026, 11, 10, tzinfo=timezone.utc))
        self.synchronize(CommissionStatus={"Id": 10, "Code": "Canceled"})
        self.assert_status_date(commission, self.now)

    def test_unchanged_single_and_regular_sync_fill_missing_historical_dates(self):
        payload = types.SimpleNamespace(
            id_=100, id_num="A-100", recording_date=datetime(2026, 10, 1),
            acceptance_date=datetime(2026, 11, 5),
            commission_status=types.SimpleNamespace(id_=7, code="Accepted"),
            commission_type=types.SimpleNamespace(id_=1),
            craftsman=types.SimpleNamespace(id_=101),
        )
        signature = self.wowiport._commission_sync_signature(payload)
        commission = self.commission(status_changed_at=None, raw_payload={"sync_signature": signature})
        service = Mock()
        service.get_commission.return_value = payload
        for mode in ("single", "regular"):
            with self.subTest(mode=mode):
                commission.status_changed_at = None
                self.db.session.commit()
                with patch.object(self.wowiport, "WowiportService", return_value=service):
                    with patch.object(self.wowiport, "configured_commission_type_ids", return_value={1}):
                        with patch.object(self.wowiport, "configured_commission_status_ids", return_value={7}):
                            if mode == "single":
                                self.assertIs(self.wowiport.sync_commission_from_erp(100), commission)
                            else:
                                result = self.wowiport.sync_commission_payload_from_erp(
                                    payload, {7}, {1}, datetime(2026, 1, 1), datetime(2027, 1, 1),
                                    "recording_date", {101}, service,
                                )
                                self.assertEqual(result, "unchanged")
                self.db.session.commit()
                self.assert_status_date(commission, datetime(2026, 11, 5, tzinfo=timezone.utc))

    def test_pending_and_failed_local_changes_keep_their_timestamp_during_sync(self):
        commission = self.commission()
        for state in ("pending", "failed"):
            with self.subTest(state=state):
                commission.erp_sync_state = state
                self.db.session.commit()
                self.synchronize(CommissionStatus={"Id": 12, "Code": "WaitForCraftsmanFeedback"})
                self.assert_status_date(commission, self.previous_date)
                self.assertEqual(commission.local_status, "in_progress")

    def test_acceptance_records_the_local_change_and_erp_confirmation_preserves_it(self):
        commission = self.commission(
            local_status="waiting_for_acceptance", status_id=12, status_code="WaitForCraftsmanFeedback",
        )
        with patch.object(self.workflow, "utc_now", return_value=self.now):
            self.workflow.accept_commission(commission, user=self.user)
        self.db.session.commit()
        self.assert_status_date(commission, self.now)
        operation = self.models.ErpOutboxOperation(
            commission=commission, operation_type="accept_commission", idempotency_key="date-accept",
        )
        self.db.session.add(operation)
        self.db.session.commit()
        later = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
        with patch.object(self.outbox, "utc_now", return_value=later):
            self.outbox.execute_outbox_operation(operation.id, Mock())
        self.db.session.commit()
        self.assert_status_date(commission, self.now)

    def test_rejection_and_completion_record_the_status_change(self):
        commission = self.commission(local_status="waiting_for_acceptance")
        with patch.object(self.workflow, "utc_now", return_value=self.now):
            self.workflow.reject_commission(commission, "Kein Zugang", user=self.user)
        self.db.session.commit()
        self.assert_status_date(commission, self.now)
        for completed, tenant_charge, status in (
            (False, False, "completion_review"),
            (True, False, "done"),
            (True, True, "tenant_charge_review"),
        ):
            with self.subTest(status=status):
                commission.local_status = "in_progress"
                commission.status_changed_at = self.previous_date
                self.db.session.commit()
                with patch.object(self.workflow, "utc_now", return_value=self.now):
                    self.workflow.complete_commission(
                        commission, completed=completed, reason="Testabschluss", tenant_charge=tenant_charge,
                        enforce_material_confirmation=False, user=self.user,
                    )
                self.db.session.commit()
                self.assertEqual(commission.local_status, status)
                self.assert_status_date(commission, self.now)

    def test_repeated_deletion_does_not_move_the_status_date(self):
        commission = self.commission()
        with patch.object(self.wowiport, "utc_now", return_value=self.now):
            self.wowiport.mark_commission_deleted(commission.erp_id)
        self.db.session.commit()
        with patch.object(self.wowiport, "utc_now", return_value=datetime(2026, 11, 13, tzinfo=timezone.utc)):
            self.wowiport.mark_commission_deleted(commission.erp_id)
        self.db.session.commit()
        self.assert_status_date(commission, self.now)


if __name__ == "__main__":
    unittest.main()
