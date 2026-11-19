import importlib
import io
import json
import sys
import tempfile
import types
import unittest
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch
from uuid import uuid4

from flask import Flask, abort, jsonify, request
from flask_jwt_extended import JWTManager, create_access_token, get_jwt_identity
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
from sqlalchemy.engine import make_url


class MobileSynchronizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.db = SQLAlchemy()
        cls.app = Flask(__name__)
        database_url = os.environ.get("DATLAEUFT_TEST_DATABASE_URL", "sqlite:///" + str(Path(cls.directory.name) / "mobile.sqlite"))
        database = make_url(database_url)
        if database.get_backend_name() != "sqlite" and not (database.database or "").startswith("datlaeuft_test_"):
            raise ValueError("Für diese Tests ist eine eigene Testdatenbank erforderlich.")
        cls.app.config.update(SQLALCHEMY_DATABASE_URI=database_url, JWT_SECRET_KEY="mobile-test-secret-at-least-thirty-two-characters", TESTING=True)
        cls.db.init_app(cls.app)
        JWTManager(cls.app)
        cls.package_name = "_mobile_sync_test_app"
        package = types.ModuleType(cls.package_name)
        package.__path__ = [str(Path(__file__).resolve().parents[1] / "app")]
        extensions = types.ModuleType(cls.package_name + ".extensions")
        extensions.db = cls.db
        api = types.ModuleType(cls.package_name + ".api")

        def current_user():
            user = cls.db.session.get(cls.models.User, int(get_jwt_identity()))
            if user is None or not user.is_active_flag:
                abort(401)
            return user

        def viewable(commission_id, user):
            commission = cls.db.session.get(cls.models.Commission, commission_id)
            if commission is None:
                abort(404)
            if commission.user_id != user.id:
                abort(403)
            return commission

        api.current_api_user = current_user
        api.viewable_commission = viewable
        api.can_edit_commission = lambda commission, user: commission.user_id == user.id
        cls.module_patch = patch.dict(sys.modules, {cls.package_name: package, extensions.__name__: extensions, api.__name__: api})
        cls.module_patch.start()
        cls.addClassCleanup(cls.module_patch.stop)
        cls.models = importlib.import_module(cls.package_name + ".models")
        cls.transactions = importlib.import_module(cls.package_name + ".mobile_transaction")
        cls.sync = importlib.import_module(cls.package_name + ".mobile_sync")
        cls.app.register_blueprint(cls.sync.mobile_sync_bp)

        @cls.app.get("/api/commissions/<int:commission_id>")
        def detail(commission_id):
            commission = viewable(commission_id, current_user())
            return jsonify(item={"id": commission.id, "local_status": commission.local_status, "assigned_user": {"id": commission.user_id}, "can_edit": True, "is_actionable": True, "accepted_appointment_comment": commission.accepted_appointment_comment})

        @cls.app.post("/api/commissions/<int:commission_id>/time")
        def create_time(commission_id):
            user = current_user()
            viewable(commission_id, user)
            data = request.get_json()
            entry = cls.models.TimeEntry(user_id=user.id, commission_id=commission_id, minutes=data["minutes"])
            cls.db.session.add(entry)
            cls.transactions.commit_mobile_changes()
            if data.get("fail_after_commit"):
                return jsonify(message="Abgelehnt"), 422
            if data.get("raise_after_commit"):
                raise RuntimeError("Abbruch")
            if data.get("delivery"):
                cls.transactions.defer_mobile_delivery("commission_status", {"commission_id": commission_id, "event_name": "Test", "reason": "Test", "submitted_by_name": "Test"})
            if data.get("file"):
                path = Path(cls.directory.name) / "upload.jpg"
                path.write_bytes(b"photo")
                cls.transactions.track_mobile_files(path)
                if data.get("fail_file"):
                    return jsonify(message="Abgelehnt"), 422
            return jsonify(item={"id": entry.id, "minutes": entry.minutes}), 201

        @cls.app.post("/api/commissions/<int:commission_id>/start")
        def start(commission_id):
            commission = viewable(commission_id, current_user())
            if commission.local_status != "waiting_for_acceptance":
                return jsonify(message="Auftrag schon bearbeitet."), 409
            commission.local_status = "in_progress"
            cls.transactions.commit_mobile_changes()
            return jsonify(item={"id": commission.id, "local_status": commission.local_status})

        @cls.app.post("/api/commissions/<int:commission_id>/complete")
        def complete(commission_id):
            commission = viewable(commission_id, current_user())
            if commission.local_status != "in_progress":
                return jsonify(message="Auftrag schon bearbeitet."), 409
            body = request.get_json()
            commission.local_status = "done"
            commission.completion_successful = body["completed"]
            commission.completion_reason = body.get("reason")
            commission.completion_submitted_at = cls.sync.utc_now()
            cls.transactions.commit_mobile_changes()
            return jsonify(item={"id": commission.id, "local_status": commission.local_status}), 202

        @cls.app.post("/api/commissions/simple")
        def existing_simple_commission():
            cls.db.session.rollback()
            return jsonify(item={"id": 10})

        @cls.app.post("/api/commissions/<int:commission_id>/materials")
        def material(commission_id):
            commission = viewable(commission_id, current_user())
            quantity = request.get_json()["quantity"]
            stock = (commission.raw_payload or {}).get("stock", 10)
            if stock < quantity:
                return jsonify(message="Nicht genügend Bestand."), 409
            commission.raw_payload = {"stock": stock - quantity}
            cls.transactions.commit_mobile_changes()
            return jsonify(item={"id": 20, "quantity": quantity})

        @cls.app.post("/api/commissions/<int:commission_id>/photos")
        def photo(commission_id):
            user = current_user()
            viewable(commission_id, user)
            content = request.files["photo"].read()
            cls.db.session.add(cls.models.TimeEntry(user_id=user.id, commission_id=commission_id, minutes=1))
            cls.transactions.commit_mobile_changes()
            return jsonify(item={"id": 50, "content": content.hex(), "caption": request.form["caption"]}), 201

        @cls.app.get("/api/commissions/<int:commission_id>/time")
        def times(commission_id):
            viewable(commission_id, current_user())
            return jsonify(items=[{"id": entry.id, "minutes": entry.minutes} for entry in cls.models.TimeEntry.query.filter_by(commission_id=commission_id).all()])

        cls.tables = [model.__table__ for model in (cls.models.User, cls.models.Commission, cls.models.TimeEntry, cls.models.MobileSyncOperation, cls.models.MobileSyncDelivery, cls.models.ErpOutboxOperation, cls.models.ProcessInfo)]

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
        self.db.session.add_all([self.models.User(id=1, email="one@example.invalid", display_name="One"), self.models.User(id=2, email="two@example.invalid", display_name="Two")])
        self.db.session.add(self.models.Commission(id=10, user_id=1, erp_id=100, erp_idnum="100", local_status="waiting_for_acceptance"))
        self.db.session.commit()
        self.headers = {"Authorization": "Bearer " + create_access_token(identity="1")}
        self.client = self.app.test_client()
        self.base = {"path": "/api/commissions/10", "query": [], "value": {"local_status": "waiting_for_acceptance", "assigned_user": {"id": 1}, "can_edit": True, "is_actionable": True, "accepted_appointment_from": None, "accepted_appointment_to": None, "accepted_appointment_comment": None, "accepted_appointment_is_manual": None, "accepted_appointment_has_time": None}}

    def operation(self, **values):
        result = {"id": str(uuid4()), "path": "/api/commissions/10/time", "method": "POST", "body": {"minutes": 60}, "base": self.base}
        result.update(values)
        return result

    def post(self, operation):
        return self.client.post("/api/mobile/offline-operations", json=operation, headers=self.headers)

    def test_retry_returns_receipt_without_duplicate_booking(self):
        operation = self.operation()
        first = self.post(operation)
        second = self.post(operation)
        self.assertEqual(first.status_code, 200, first.json)
        self.assertEqual(second.json, first.json)
        self.assertEqual(self.models.TimeEntry.query.count(), 1)
        self.assertEqual(self.models.MobileSyncOperation.query.count(), 1)

    def test_operation_id_cannot_be_reused_for_changed_data(self):
        operation = self.operation()
        self.assertEqual(self.post(operation).status_code, 200)
        operation["body"]["minutes"] = 120
        self.assertEqual(self.post(operation).status_code, 409)
        self.assertEqual(self.models.TimeEntry.query.count(), 1)

    def test_rejection_rolls_back_even_after_inner_commit(self):
        response = self.post(self.operation(body={"minutes": 60, "fail_after_commit": True}))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.models.TimeEntry.query.count(), 0)
        self.assertEqual(self.models.MobileSyncOperation.query.count(), 0)

    def test_existing_order_returned_after_inner_rollback_still_receives_confirmation(self):
        operation = self.operation(path="/api/commissions/simple", body={})
        operation.pop("base")
        result = self.post(operation)
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(self.models.MobileSyncOperation.query.count(), 1)
        self.assertEqual(self.post(operation).json, result.json)

    def test_exception_rolls_back_even_after_inner_commit(self):
        with self.assertLogs(self.app.logger, level="ERROR"):
            response = self.post(self.operation(body={"minutes": 60, "raise_after_commit": True}))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.models.TimeEntry.query.count(), 0)

    def test_server_change_is_reported_without_overwriting(self):
        commission = self.db.session.get(self.models.Commission, 10)
        commission.local_status = "done"
        self.db.session.commit()
        response = self.post(self.operation())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json["code"], "conflict")
        self.assertEqual(self.models.TimeEntry.query.count(), 0)

    def test_own_previous_change_can_be_followed_by_booking(self):
        start = self.operation(path="/api/commissions/10/start", body={})
        self.assertEqual(self.post(start).status_code, 200)
        response = self.post(self.operation(predecessor_id=start["id"]))
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.models.TimeEntry.query.count(), 1)

    def test_missing_predecessor_blocks_booking(self):
        response = self.post(self.operation(predecessor_id=str(uuid4())))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json["code"], "dependency_pending")
        self.assertEqual(self.models.TimeEntry.query.count(), 0)

    def test_comparison_stand_is_required(self):
        operation = self.operation(path="/api/commissions/10/start", body={})
        operation.pop("base")
        self.assertEqual(self.post(operation).status_code, 409)
        self.assertEqual(self.models.TimeEntry.query.count(), 0)

    def test_unlisted_paths_cannot_be_dispatched(self):
        for path in ["https://example.invalid/api/commissions/10/time", "/api/inventory/transfer", "/api/commissions/10/../../admin", "/api/mobile/offline-operations"]:
            self.assertEqual(self.post(self.operation(path=path)).status_code, 400)

    def test_snapshot_obeys_permissions(self):
        self.headers = {"Authorization": "Bearer " + create_access_token(identity="2")}
        response = self.client.post("/api/mobile/offline-data", json={"resources": [{"path": "/api/commissions/10", "query": []}]}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["items"][0]["status"], 403)
        self.assertNotIn("item", response.json["items"][0]["payload"] or {})

    def test_delivery_is_atomic_and_not_created_again_on_retry(self):
        operation = self.operation(body={"minutes": 60, "delivery": True})
        self.assertEqual(self.post(operation).status_code, 200)
        self.assertEqual(self.post(operation).status_code, 200)
        self.assertEqual(self.models.MobileSyncDelivery.query.count(), 1)

    def test_failed_upload_files_are_removed(self):
        response = self.post(self.operation(body={"minutes": 60, "file": True, "fail_file": True}))
        self.assertEqual(response.status_code, 422)
        self.assertFalse((Path(self.directory.name) / "upload.jpg").exists())

    def test_successful_upload_files_remain(self):
        response = self.post(self.operation(body={"minutes": 60, "file": True}))
        self.assertEqual(response.status_code, 200)
        self.assertTrue((Path(self.directory.name) / "upload.jpg").exists())
        (Path(self.directory.name) / "upload.jpg").unlink()

    def test_receipts_are_scoped_to_the_user(self):
        operation = self.operation()
        self.assertEqual(self.post(operation).status_code, 200)
        self.headers = {"Authorization": "Bearer " + create_access_token(identity="2")}
        response = self.post(operation)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.models.TimeEntry.query.count(), 1)

    def test_material_is_not_booked_twice(self):
        operation = self.operation(path="/api/commissions/10/materials", body={"quantity": 3})
        self.assertEqual(self.post(operation).status_code, 200)
        self.assertEqual(self.post(operation).status_code, 200)
        self.db.session.expire_all()
        self.assertEqual(self.db.session.get(self.models.Commission, 10).raw_payload["stock"], 7)

    def test_concurrent_retries_create_only_one_booking(self):
        operation = self.operation()
        def submit():
            with self.app.test_client() as client:
                return client.post("/api/mobile/offline-operations", json=operation, headers=self.headers).status_code
        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = list(executor.map(lambda _: submit(), range(2)))
        for status in statuses:
            if status == 503:
                self.assertEqual(self.post(operation).status_code, 200)
            else:
                self.assertEqual(status, 200)
        self.db.session.expire_all()
        self.assertEqual(self.models.TimeEntry.query.count(), 1)
        self.assertEqual(self.models.MobileSyncOperation.query.count(), 1)

    def test_receipt_query_does_not_expose_other_users(self):
        operation = self.operation()
        self.assertEqual(self.post(operation).status_code, 200)
        response = self.client.get("/api/mobile/offline-operations/" + operation["id"], headers=self.headers)
        self.assertEqual(response.status_code, 200)
        other = {"Authorization": "Bearer " + create_access_token(identity="2")}
        response = self.client.get("/api/mobile/offline-operations/" + operation["id"], headers=other)
        self.assertEqual(response.status_code, 404)

    def test_numeric_versions_allow_json_number_roundtrips(self):
        self.assertTrue(self.sync.same_value({"quantity": 1.0}, {"quantity": 1}))
        self.assertFalse(self.sync.same_value({"can_edit": True}, {"can_edit": 1}))

    def test_additive_booking_does_not_require_unchanged_parent(self):
        commission = self.db.session.get(self.models.Commission, 10)
        commission.local_status = "in_progress"
        self.db.session.commit()
        operation = self.operation()
        operation.pop("base")
        self.assertEqual(self.post(operation).status_code, 200)
        self.assertEqual(self.models.TimeEntry.query.count(), 1)

    def test_booking_waits_for_erp_confirmation_and_can_retry(self):
        commission = self.db.session.get(self.models.Commission, 10)
        commission.erp_idnum = None
        self.db.session.commit()
        operation = self.operation()
        response = self.post(operation)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json["code"], "awaiting_erp")
        self.assertEqual(self.models.MobileSyncOperation.query.count(), 0)
        commission.erp_idnum = "100"
        self.db.session.commit()
        self.assertEqual(self.post(operation).status_code, 200)
        self.assertEqual(self.models.TimeEntry.query.count(), 1)

    def test_additive_booking_still_waits_for_its_predecessor(self):
        operation = self.operation(predecessor_id=str(uuid4()))
        operation.pop("base")
        response = self.post(operation)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json["code"], "dependency_pending")
        self.assertEqual(self.models.TimeEntry.query.count(), 0)

    def test_same_start_from_two_devices_creates_a_receipt_for_both(self):
        tablet = self.operation(path="/api/commissions/10/start", body={})
        phone = self.operation(path=tablet["path"], body={})
        self.assertEqual(self.post(tablet).status_code, 200)
        response = self.post(phone)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertTrue(response.json["already_applied"])
        self.assertEqual(self.post(phone).json, response.json)
        close = self.operation(path="/api/commissions/10/complete", body={"completed": True}, predecessor_id=phone["id"])
        self.assertEqual(self.post(close).status_code, 200)
        self.assertEqual(self.models.MobileSyncOperation.query.count(), 3)

    def test_completion_requires_the_same_result_to_be_reconciled(self):
        commission = self.db.session.get(self.models.Commission, 10)
        commission.local_status = "in_progress"
        self.db.session.commit()
        self.base["value"]["local_status"] = "in_progress"
        body = {"completed": True, "reason": "Erledigt"}
        path = "/api/commissions/10/complete"
        self.assertEqual(self.post(self.operation(path=path, body=body)).status_code, 200)
        response = self.post(self.operation(path=path, body=body))
        self.assertEqual(response.status_code, 200, response.json)
        self.assertTrue(response.json["already_applied"])
        for different in ({**body, "reason": "Andere Angaben"}, {**body, "tenant_charge": True}, {**body, "completed": False}):
            response = self.post(self.operation(path=path, body=different))
            self.assertEqual(response.status_code, 409)
        self.assertEqual(self.models.MobileSyncOperation.query.count(), 2)

    def test_fresh_baseline_wins_over_historical_receipt(self):
        start = self.operation(path="/api/commissions/10/start", body={})
        self.assertEqual(self.post(start).status_code, 200)
        commission = self.db.session.get(self.models.Commission, 10)
        commission.accepted_appointment_comment = "Neuer Termin"
        self.db.session.commit()
        self.base["value"].update(local_status="in_progress", accepted_appointment_comment="Neuer Termin")
        result = self.post(self.operation(predecessor_id=start["id"]))
        self.assertEqual(result.status_code, 200, result.json)

    def test_unrelated_appointment_does_not_prevent_start(self):
        commission = self.db.session.get(self.models.Commission, 10)
        commission.accepted_appointment_comment = "Neuer Termin"
        self.db.session.commit()
        response = self.post(self.operation(path="/api/commissions/10/start", body={}))
        self.assertEqual(response.status_code, 200, response.json)

    def test_removed_and_closed_orders_have_terminal_results(self):
        commission = self.db.session.get(self.models.Commission, 10)
        for status in ("canceled", "deleted"):
            commission.local_status = status
            self.db.session.commit()
            response = self.post(self.operation())
            self.assertEqual((response.status_code, response.json["code"]), (404, "commission_unavailable"))
        commission.local_status = "completion_review"
        commission.review_resolved_at = self.sync.utc_now()
        self.db.session.commit()
        response = self.post(self.operation())
        self.assertEqual((response.status_code, response.json["code"]), (409, "commission_closed"))
        self.assertEqual(self.models.MobileSyncOperation.query.count(), 0)

    def test_identical_unseen_booking_from_second_device_requires_confirmation(self):
        first_device, second_device = str(uuid4()), str(uuid4())
        first = self.post(self.operation(device_id=first_device))
        self.assertEqual(first.status_code, 200, first.json)
        second = self.operation(device_id=second_device)
        conflict = self.post(second)
        self.assertEqual((conflict.status_code, conflict.json["code"]), (409, "duplicate_booking"))
        self.assertEqual(self.models.TimeEntry.query.count(), 1)
        second["confirm_duplicate"] = True
        confirmed = self.post(second)
        self.assertEqual(confirmed.status_code, 200, confirmed.json)
        self.assertEqual(self.post(second).json, confirmed.json)
        later = self.operation(device_id=first_device, known_booking_ids=[confirmed.json["payload"]["item"]["id"]])
        self.assertEqual(self.post(later).status_code, 200)
        self.assertEqual(self.models.TimeEntry.query.count(), 3)

    def test_order_does_not_hold_same_users_other_order(self):
        if self.db.engine.dialect.name == "sqlite":
            self.skipTest("SQLite sperrt Schreibtransaktionen datenbankweit.")
        self.db.session.add(self.models.Commission(id=11, user_id=1, erp_id=101, erp_idnum="101", local_status="in_progress"))
        self.db.session.commit()
        first = self.operation()
        second = self.operation(path="/api/commissions/11/time")
        first.pop("base")
        second.pop("base")
        waiting, release = Event(), Event()
        dispatch = self.sync.dispatch

        def blocked_dispatch(path, method="GET", **options):
            if path == first["path"] and method == "POST":
                waiting.set()
                if not release.wait(12):
                    raise TimeoutError()
            return dispatch(path, method, **options)

        def submit(operation):
            with self.app.test_client() as client:
                return client.post("/api/mobile/offline-operations", json=operation, headers=self.headers)

        with patch.object(self.sync, "dispatch", side_effect=blocked_dispatch), ThreadPoolExecutor(max_workers=2) as executor:
            paused = executor.submit(submit, first)
            try:
                self.assertTrue(waiting.wait(5))
                other = executor.submit(submit, second).result(timeout=5)
                self.assertEqual(other.status_code, 200, other.json)
            finally:
                release.set()
            self.assertEqual(paused.result(timeout=5).status_code, 200)

    def test_server_issues_and_retries_are_scoped_to_user(self):
        self.db.session.add(self.models.Commission(id=11, user_id=2))
        self.db.session.flush()
        for user_id, commission_id in [(1, 10), (2, 11)]:
            self.db.session.add(self.models.ErpOutboxOperation(commission_id=commission_id, operation_type="create_commission", idempotency_key=str(uuid4()), state="failed", last_error=f"ERP {user_id}", available_at=self.sync.utc_now() + timedelta(hours=1)))
            self.db.session.add(self.models.ProcessInfo(id=str(uuid4()), process_erp_id=commission_id, sender_user_id=user_id, direction="craftsman", body="Text", erp_body="Text", state="uncertain", last_error=f"Info {user_id}"))
        self.db.session.commit()
        response = self.client.get("/api/mobile/delivery-issues", headers=self.headers)
        messages = [item["message"] for item in response.json["items"]]
        self.assertEqual(len(messages), 2)
        self.assertTrue(any("ERP 1" in message for message in messages))
        self.assertTrue(any("Info 1" in message for message in messages))
        self.assertFalse(any("ERP 2" in message or "Info 2" in message for message in messages))
        self.assertEqual(self.client.post("/api/mobile/delivery-issues/retry", headers=self.headers).status_code, 200)
        self.db.session.expire_all()
        own = self.models.ErpOutboxOperation.query.filter_by(commission_id=10).one()
        other = self.models.ErpOutboxOperation.query.filter_by(commission_id=11).one()
        self.assertLess(own.available_at, other.available_at)

    def test_message_delivery_survives_service_commits(self):
        service = types.ModuleType(self.package_name + ".process_info_service")
        def write(info_id):
            self.assertEqual(info_id, "message")
            row = self.db.session.get(self.models.MobileSyncDelivery, "delivery")
            self.assertEqual(row.state, "processing")
            self.db.session.commit()
        service.write_outgoing_info = Mock(side_effect=write)
        self.db.session.add(self.models.MobileSyncDelivery(id="delivery", user_id=1, kind="process_info", payload={"info_id": "message"}))
        self.db.session.commit()
        with patch.dict(sys.modules, {service.__name__: service}):
            self.assertEqual(self.sync.process_mobile_deliveries(), 1)
            self.assertEqual(self.sync.process_mobile_deliveries(), 0)
        service.write_outgoing_info.assert_called_once_with("message")
        row = self.db.session.get(self.models.MobileSyncDelivery, "delivery")
        self.assertEqual(row.state, "sent")
        self.assertEqual(row.payload, {})

    def test_failed_message_delivery_is_retained_for_retry(self):
        service = types.ModuleType(self.package_name + ".process_info_service")
        service.write_outgoing_info = Mock(side_effect=RuntimeError("Wowiport nicht erreichbar"))
        self.db.session.add(self.models.MobileSyncDelivery(id="delivery", user_id=1, kind="process_info", payload={"info_id": "message"}))
        self.db.session.commit()
        with patch.dict(sys.modules, {service.__name__: service}), self.assertLogs(self.app.logger, level="ERROR") as logs:
            self.assertEqual(self.sync.process_mobile_deliveries(), 1)
        self.assertIn("mobile_delivery_failed delivery_id=delivery", "\n".join(logs.output))
        row = self.db.session.get(self.models.MobileSyncDelivery, "delivery")
        self.assertEqual(row.state, "failed")
        self.assertEqual(row.attempts, 1)
        self.assertEqual(row.payload, {"info_id": "message"})
        self.assertEqual(row.error, "Wowiport nicht erreichbar")

    def test_interrupted_delivery_is_reclaimed_after_lease_expires(self):
        service = types.ModuleType(self.package_name + ".process_info_service")
        service.write_outgoing_info = Mock()
        self.db.session.add(self.models.MobileSyncDelivery(id="delivery", user_id=1, kind="process_info", payload={"info_id": "message"}, state="processing", available_at=self.sync.utc_now() - timedelta(seconds=1)))
        self.db.session.commit()
        with patch.dict(sys.modules, {service.__name__: service}):
            self.assertEqual(self.sync.process_mobile_deliveries(), 1)
        service.write_outgoing_info.assert_called_once_with("message")
        self.assertEqual(self.db.session.get(self.models.MobileSyncDelivery, "delivery").state, "sent")

    def test_multipart_retries_preserve_binary_photo_and_metadata(self):
        content = bytes([0, 255, 128, 13, 10, 1, 2])
        raw = b'--sample\r\nContent-Disposition: form-data; name="caption"\r\n\r\nFoto\r\n--sample\r\nContent-Disposition: form-data; name="photo"; filename="foto.jpg"\r\nContent-Type: image/jpeg\r\n\r\n' + content + b'\r\n--sample--\r\n'
        operation = self.operation(path="/api/commissions/10/photos", body={}, content_type="multipart/form-data; boundary=sample")
        operation.pop("base")
        def submit():
            return self.client.post("/api/mobile/offline-operations", data={"operation": json.dumps(operation), "request_body": (io.BytesIO(raw), "request.bin")}, headers=self.headers)
        first = submit()
        self.assertEqual(first.status_code, 200, first.json)
        self.assertEqual(first.json["payload"]["item"]["content"], content.hex())
        self.assertEqual(first.json["payload"]["item"]["caption"], "Foto")
        self.assertEqual(submit().json, first.json)
        self.assertEqual(self.models.TimeEntry.query.count(), 1)


if __name__ == "__main__":
    unittest.main()
