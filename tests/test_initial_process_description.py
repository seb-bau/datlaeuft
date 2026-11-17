import ast
import importlib
from datetime import timedelta
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from flask import Flask, abort, current_app, jsonify
from flask_jwt_extended import JWTManager, create_access_token, get_jwt_identity
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import or_


class InitialProcessDescriptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.db = SQLAlchemy()
        cls.app = Flask(__name__)
        cls.app.config.update(SQLALCHEMY_DATABASE_URI="sqlite://", TESTING=True,
                              JWT_SECRET_KEY="process-description-test-secret-at-least-thirty-two-characters")
        cls.db.init_app(cls.app)
        cls.package_name = "_initial_process_description_test_app"
        package = types.ModuleType(cls.package_name)
        package.__path__ = [str(cls.root / "app")]
        extensions = types.ModuleType(cls.package_name + ".extensions")
        extensions.db = cls.db
        extensions.jwt = JWTManager(cls.app)
        gateway = types.ModuleType(cls.package_name + ".wowiport")
        gateway.WowiportService = Mock()
        cls.push = types.ModuleType(cls.package_name + ".push_service")
        cls.push.enqueue_process_info_push = Mock()
        cls.notifications = types.ModuleType(cls.package_name + ".process_info_notifications")
        for name in ("read_receipts", "record_visible_reads", "visible_items", "notify_craftsmen"):
            setattr(cls.notifications, name, Mock(return_value=[]))
        api = types.ModuleType(cls.package_name + ".api")

        def current_user():
            return cls.db.session.get(cls.models.User, int(get_jwt_identity()))

        def viewable(commission_id, user):
            commission = cls.db.session.get(cls.models.Commission, commission_id)
            if commission is None:
                abort(404)
            if commission.user_id != user.id:
                abort(403)
            return commission

        api.current_api_user = current_user
        api.viewable_commission = viewable
        api.editable_commission = viewable
        modules = patch.dict(sys.modules, {
            cls.package_name: package, extensions.__name__: extensions, gateway.__name__: gateway,
            cls.push.__name__: cls.push, cls.notifications.__name__: cls.notifications, api.__name__: api,
        })
        modules.start()
        cls.addClassCleanup(modules.stop)
        cls.models = importlib.import_module(cls.package_name + ".models")
        cls.service = importlib.import_module(cls.package_name + ".process_info_service")
        cls.api = importlib.import_module(cls.package_name + ".process_info_api")
        cls.app.register_blueprint(cls.api.process_info_api_bp)
        source = ast.parse((cls.root / "app/process_info_web.py").read_text())
        function = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == "chat_status")
        namespace = {"ProcessInfo": cls.models.ProcessInfo, "jsonify": jsonify, "read_receipts": cls.notifications.read_receipts}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "process_info_web.py", "exec"), namespace)
        cls.app.add_url_rule("/test/web-chat/<int:process_id>", view_func=namespace["chat_status"])
        cls.tables = [model.__table__ for model in (
            cls.models.User, cls.models.Commission, cls.models.CommissionPosition, cls.models.AppSetting,
            cls.models.BlacklistEntry, cls.models.ExternalCraftsman, cls.models.ProcessInfo,
            cls.models.ProcessInfoThread, cls.models.ProcessInfoRead, cls.models.ProcessInfoDelivery,
            cls.models.BrowserPushSubscription,
            cls.models.CommissionPhoto, cls.models.CommissionAttachment, cls.models.CommissionAppointmentRequest,
            cls.models.AuditLog, cls.models.TimeEntry,
        )]

    def setUp(self):
        context = self.app.app_context()
        context.push()
        self.addCleanup(context.pop)
        self.addCleanup(self.db.session.remove)
        self.db.metadata.drop_all(self.db.engine, tables=self.tables)
        self.db.metadata.create_all(self.db.engine, tables=self.tables)
        self.office = self.models.User(email="office@example.invalid", display_name="Sachbearbeitung", is_foreman=True)
        self.craftsman = self.models.User(email="craftsman@example.invalid", display_name="Handwerker", craftsman_erp_id=123)
        self.commission = self.models.Commission(
            crafts_process_erp_id=900, description="Wasserhahn reparieren.", user=self.craftsman,
            created_by=self.office, local_official=self.office, local_status="open", source="web",
        )
        self.db.session.add_all([self.office, self.craftsman, self.commission])
        self.db.session.commit()
        self.client = self.app.test_client()
        self.headers = {"Authorization": "Bearer " + create_access_token(identity=str(self.craftsman.id))}
        self.remote = Mock()
        self.remote.get_process_notes.return_value = []
        gateway = patch.object(self.service, "WowiportService", return_value=self.remote)
        gateway.start()
        self.addCleanup(gateway.stop)
        self.push.enqueue_process_info_push.reset_mock()
        self.notifications.notify_craftsmen.reset_mock()

    def note(self, note_id, body):
        return {"Id": note_id, "Description": body, "Date": "2026-11-16T10:00:00", "User": {"UserName": "Sachbearbeitung"}}

    def known_thread(self):
        self.db.session.add(self.models.ProcessInfoThread(
            process_erp_id=900, last_synced_at=self.models.utc_now() - timedelta(days=1),
        ))
        self.db.session.commit()

    def sync(self, rows):
        self.remote.get_process_notes.return_value = rows
        changed = self.service.sync_process_infos(900, notify=True, service=self.remote)
        self.db.session.commit()
        return changed

    def messages(self):
        return self.models.ProcessInfo.query.filter(self.models.ProcessInfo.visible_filter()).order_by(self.models.ProcessInfo.erp_note_id).all()

    def detail_context(self, path, **parameters):
        source = ast.parse((self.root / path).read_text())
        function = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == "commission_detail")
        function.decorator_list = []
        namespace = {
            "Any": object, "db": self.db, "current_app": current_app, "current_user": self.office,
            "render_template": lambda template, **values: values, "sync_process_infos": self.service.sync_process_infos,
            "format_manual_appointment": lambda item: "", "format_local_datetime": str,
            "commission_wowiport_url": lambda item: None, "can_approve_commission": lambda *args: False,
            "commission_list_return_url": lambda: "/admin/commissions",
            "external_commission_or_404": lambda public_id: self.commission,
            "external_commission_photos": lambda item: [], "WowiportService": lambda: self.remote,
            "or_": or_, "uuid4": uuid4,
        }
        namespace.update({name: getattr(self.models, name) for name in (
            "Commission", "ProcessInfo", "CommissionPhoto", "CommissionAppointmentRequest", "AuditLog", "User", "CommissionAttachment",
        )})
        exec(compile(ast.Module(body=[function], type_ignores=[]), path, "exec"), namespace)
        return namespace["commission_detail"](**parameters)

    def test_initial_description_is_retained_but_hidden_and_never_notified(self):
        self.known_thread()
        changed = self.sync([
            self.note(102, "Ein Ersatzteil ist nötig."),
            self.note(100, " Wasserhahn\r\n reparieren. "),
        ])
        self.assertEqual(changed, 1)
        self.assertEqual([item.erp_note_id for item in self.messages()], [102])
        original = self.models.ProcessInfo.query.filter_by(erp_note_id=100).one()
        self.assertEqual(original.state, "initial_description")
        self.assertEqual(original.mail_state, "not_required")
        self.assertIn("Wasserhahn", original.erp_body)
        self.assertFalse(self.service.is_info_unread(original, self.craftsman.id))
        self.assertEqual(self.service.unread_summary_for_user(self.craftsman.id), {
            "total": 1, "by_commission": {str(self.commission.id): 1},
        })
        self.assertEqual(self.service.unread_count_for_user(self.craftsman.id), 1)
        self.push.enqueue_process_info_push.assert_called_once()
        self.assertEqual(self.push.enqueue_process_info_push.call_args.kwargs["info"].erp_note_id, 102)

    def test_a_real_first_message_and_later_matching_texts_are_preserved(self):
        self.known_thread()
        self.assertEqual(self.sync([self.note(100, "Bitte vorher anrufen."), self.note(101, self.commission.description)]), 2)
        self.assertEqual([item.erp_note_id for item in self.messages()], [100, 101])

    def test_only_the_original_entry_is_hidden_when_later_messages_repeat_the_order_text(self):
        self.assertEqual(self.sync([self.note(100, self.commission.description), self.note(101, self.commission.description)]), 1)
        self.assertEqual([item.erp_note_id for item in self.messages()], [101])

    def test_existing_description_messages_are_reclassified_and_pending_delivery_is_cancelled(self):
        self.known_thread()
        original = self.models.ProcessInfo(
            process_erp_id=900, erp_note_id=100, direction="office", body=self.commission.description,
            erp_body=self.commission.description, state="synced", mail_state="pending",
            mail_available_at=self.models.utc_now(),
        )
        self.db.session.add(original)
        self.db.session.flush()
        delivery = self.models.ProcessInfoDelivery(
            process_info_id=original.id, version=1, commission_id=self.commission.id,
            channel="email", recipient_key="test", destination_key="test", recipient_name="Test",
            recipient_email="office@example.invalid", available_at=self.models.utc_now(),
        )
        self.db.session.add(delivery)
        self.db.session.commit()
        self.assertEqual(self.service.unread_count_for_user(self.craftsman.id), 1)
        self.assertEqual(self.sync([self.note(100, self.commission.description)]), 0)
        self.db.session.refresh(delivery)
        self.assertEqual(delivery.state, "cancelled")
        self.assertEqual(original.state, "initial_description")
        self.assertEqual(original.version, 1)
        self.assertEqual(self.messages(), [])
        self.assertEqual(self.service.unread_count_for_user(self.craftsman.id), 0)
        self.assertEqual(self.service.process_one_info_email(), 0)
        self.push.enqueue_process_info_push.assert_not_called()

    def test_saved_original_text_takes_precedence_over_a_changed_order_description(self):
        self.commission.raw_payload = {"initial_process_description": {"process_erp_id": 900, "body": self.commission.description}}
        self.commission.description = "Neue Auftragsbeschreibung."
        self.db.session.commit()
        self.sync([self.note(100, "Wasserhahn reparieren."), self.note(101, self.commission.description)])
        self.assertEqual([item.erp_note_id for item in self.messages()], [101])

    def test_a_description_snapshot_for_another_process_cannot_hide_a_message(self):
        self.commission.raw_payload = {"initial_process_description": {"process_erp_id": 999, "body": "Andere Nachricht."}}
        self.db.session.commit()
        self.sync([self.note(100, "Andere Nachricht.")])
        self.assertEqual([item.erp_note_id for item in self.messages()], [100])

    def test_locally_authored_messages_are_never_mistaken_for_the_description(self):
        message = self.models.ProcessInfo(
            process_erp_id=900, erp_note_id=100, direction="office", body=self.commission.description,
            erp_body=self.commission.description, sender_user=self.office, state="synced",
        )
        self.db.session.add(message)
        self.db.session.commit()
        self.sync([self.note(100, self.commission.description)])
        self.assertEqual(self.messages(), [message])
        self.assertEqual(message.state, "synced")

    def test_outgoing_messages_are_correlated_before_initial_description_detection(self):
        message = self.models.ProcessInfo(
            id=str(uuid4()), process_erp_id=900, direction="craftsman", body=self.commission.description,
            erp_body=self.commission.description, sender_user=self.craftsman, state="pending", erp_note_ids_before=[],
        )
        self.db.session.add(message)
        self.db.session.commit()
        self.sync([self.note(100, self.commission.description)])
        self.assertEqual(self.messages(), [message])
        self.assertEqual(message.erp_note_id, 100)
        self.assertEqual(message.state, "synced")

    def test_classification_survives_later_edits_and_does_not_hide_a_replacement_note(self):
        self.sync([self.note(100, self.commission.description)])
        original = self.models.ProcessInfo.query.one()
        self.commission.description = "Geänderter Auftragstext."
        self.sync([self.note(100, "Geänderter Vorgangstext."), self.note(101, "Echte Zusatzinfo.")])
        self.assertEqual(original.state, "initial_description")
        self.assertEqual([item.erp_note_id for item in self.messages()], [101])
        self.sync([self.note(101, "Echte Zusatzinfo."), self.note(102, self.commission.description)])
        self.assertEqual([item.erp_note_id for item in self.messages()], [101, 102])
        self.assertEqual(original.state, "initial_description")

    def test_mobile_api_web_status_and_unread_counts_exclude_the_description(self):
        self.known_thread()
        self.remote.get_process_notes.return_value = [self.note(100, self.commission.description), self.note(101, "Echte Zusatzinfo.")]
        response = self.client.get(f"/api/commissions/{self.commission.id}/infos", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["body"] for item in response.json["items"]], ["Echte Zusatzinfo."])
        self.assertTrue(response.json["items"][0]["is_unread"])
        original = self.models.ProcessInfo.query.filter_by(erp_note_id=100).one()
        status = self.client.get("/test/web-chat/900")
        self.assertEqual(status.status_code, 200)
        self.assertEqual([item["id"] for item in status.json["items"]], [response.json["items"][0]["id"]])
        read = self.client.post(f"/api/commissions/{self.commission.id}/infos/read", headers=self.headers, json={"info_ids": [original.id]})
        self.assertEqual(read.status_code, 200)
        self.assertEqual(read.json["total"], 1)
        self.assertIsNone(self.db.session.get(self.models.ProcessInfoRead, (original.id, self.craftsman.id)))

    def test_web_order_passes_only_additional_messages_to_its_template(self):
        self.remote.get_process_notes.return_value = [self.note(100, self.commission.description), self.note(101, "Echte Zusatzinfo.")]
        view = self.detail_context("app/admin.py", commission_id=self.commission.id)
        self.assertIsNone(view["process_info_sync_error"])
        self.assertEqual([item.body for item in view["process_infos"]], ["Echte Zusatzinfo."])
        self.assertEqual(view["item"].description, "Wasserhahn reparieren.")

    def test_external_order_passes_only_additional_messages_to_its_template(self):
        self.remote.get_process_notes.return_value = [self.note(100, self.commission.description), self.note(101, "Echte Zusatzinfo.")]
        view = self.detail_context("app/external_portal.py", public_id=self.commission.public_id)
        self.assertIsNone(view["info_sync_error"])
        self.assertEqual([item.body for item in view["infos"]], ["Echte Zusatzinfo."])
        self.assertEqual(view["item"].description, "Wasserhahn reparieren.")


if __name__ == "__main__":
    unittest.main()

