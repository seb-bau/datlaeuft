import importlib
import json
import sys
import tempfile
import types
import unittest
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
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
            return jsonify(item={"id": commission.id, "local_status": commission.local_status, "assigned_user": {"id": commission.user_id}, "can_edit": True, "is_actionable": True})

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
            commission.local_status = "in_progress"
            cls.transactions.commit_mobile_changes()
            return jsonify(item={"id": commission.id, "local_status": commission.local_status})

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

        @cls.app.get("/api/commissions/<int:commission_id>/time")
        def times(commission_id):
            viewable(commission_id, current_user())
            return jsonify(items=[{"id": entry.id, "minutes": entry.minutes} for entry in cls.models.TimeEntry.query.filter_by(commission_id=commission_id).all()])

        cls.tables = [model.__table__ for model in (cls.models.User, cls.models.Commission, cls.models.TimeEntry, cls.models.MobileSyncOperation, cls.models.MobileSyncDelivery)]

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
        self.base = {"path": "/api/commissions/10", "query": [], "value": {"local_status": "waiting_for_acceptance", "assigned_user": {"id": 1}, "can_edit": True, "is_actionable": True, "accepted_appointment_from": None, "accepted_appointment_to": None, "accepted_appointment_comment": None, "accepted_appointment_is_manual": None}}

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


if __name__ == "__main__":
    unittest.main()
