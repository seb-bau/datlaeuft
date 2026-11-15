import ast
from functools import wraps
import importlib
from io import BytesIO
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from flask import abort, redirect, request, url_for
from flask_login import current_user
from PIL import Image
from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader
from werkzeug.datastructures import MultiDict

import test_commission_approval as approval_fixtures


class TicketPhotoSelectionTests(unittest.TestCase):
    login = approval_fixtures.CommissionApprovalTests.login

    @classmethod
    def setUpClass(cls):
        approval_fixtures.CommissionApprovalTests.setUpClass.__func__(cls)
        for name in ("mobile_transaction", "photo_service", "attachment_service", "ticket_photos", "document_sync"):
            sys.modules.pop(cls.package_name + "." + name, None)
            setattr(cls, name, importlib.import_module(cls.package_name + "." + name))
        cls.admin.store_commission_photo = cls.photo_service.store_commission_photo
        cls.admin.commission_photo_path = cls.photo_service.commission_photo_path
        for name in ("prepare_ticket_photo_selection", "read_ticket_photo", "render_ticket_photo", "store_selected_ticket_photos", "ticket_photo_choices"):
            setattr(cls.admin, name, getattr(cls.ticket_photos, name))
        cls.admin.create_pending_commission = cls.service.create_pending_commission
        cls.admin.enqueue_erp_operation = cls.erp.enqueue_erp_operation
        cls.tables.append(cls.models.CommissionAttachment.__table__)
        cls.tables.append(cls.models.CommissionTicketLink.__table__)
        cls.tables.append(cls.models.WowiCommissionType.__table__)
        auth_source = ast.parse((cls.root / "app/auth.py").read_text())
        guard = next(node for node in auth_source.body if isinstance(node, ast.FunctionDef) and node.name == "commission_creation_required")
        namespace = dict(F=object, Any=object, wraps=wraps, current_user=current_user, abort=abort, redirect=redirect, request=request, url_for=url_for)
        exec(compile(ast.Module(body=[guard], type_ignores=[]), "auth.py", "exec"), namespace)
        cls.app.view_functions["admin.commission_create"] = namespace["commission_creation_required"](cls.app.view_functions["admin.commission_create"].__wrapped__)
        cls.app.jinja_loader = ChoiceLoader([
            DictLoader({"base.html": "<html><body>{% block content %}{% endblock %}{% block scripts %}{% endblock %}</body></html>"}),
            FileSystemLoader(str(cls.root / "app/templates")),
        ])

    def start_patch(self, item):
        result = item.start()
        self.addCleanup(item.stop)
        return result

    def setUp(self):
        approval_fixtures.CommissionApprovalTests.setUp(self)
        self.directory_path = TemporaryDirectory()
        self.addCleanup(self.directory_path.cleanup)
        self.app.config.update(
            COMMISSION_PHOTO_DIR=str(Path(self.directory_path.name) / "photos"),
            COMMISSION_ATTACHMENT_DIR=str(Path(self.directory_path.name) / "attachments"),
        )
        self.creator.external_commission_approver_user_id = None
        self.ticket = self.models.TicketMirror(erp_id=7001, id_num="T-7001", subject="Küche und Bad", state="new", ai_state="ready")
        self.db.session.add_all([self.ticket, self.models.WowiCommissionType(erp_id=100, code="Reparatur", active=True)])
        self.db.session.commit()
        self.login(self.creator)
        self.remote = Mock()
        self.rows = [
            {"FileGuid": "kitchen", "FileName": "kueche.png", "ExternalDocumentId": "ticket-kitchen"},
            {"FileGuid": "bathroom", "FileName": "bad.png", "ExternalDocumentId": "ticket-bathroom"},
            {"FileGuid": "obsolete", "FileName": "alt.jpg", "FileIsObsolete": True},
            {"FileGuid": "document", "FileName": "hinweise.pdf"},
        ]
        self.commission_rows = []
        self.remote.get_document_rows.side_effect = lambda entity_name, entity_id: self.rows if entity_name == "Ticket" else self.commission_rows
        self.remote.get_commission_ticket_rows.return_value = [{"Id": self.ticket.erp_id}]
        self.contents = {"kitchen": self.image("red"), "bathroom": self.image("blue"), "document": b"%PDF-1.4\n%%EOF\n"}
        self.remote.download_document_content.side_effect = lambda entity_name, file_guid: self.contents[file_guid]
        self.start_patch(patch.object(self.admin, "WowiportService", return_value=self.remote))
        self.start_patch(patch.object(self.admin, "configured_commission_type_ids", return_value={100}))
        self.start_patch(patch.object(self.admin, "available_external_craftsmen", return_value=[]))
        self.start_patch(patch.object(self.admin, "ticket_license_agreement_local_id", return_value=self.agreement.id))
        self.start_patch(patch.object(self.admin, "ticket_ai_is_current", return_value=True))

    def image(self, color):
        output = BytesIO()
        Image.new("RGB", (32, 24), color).save(output, format="PNG")
        return output.getvalue()

    def form(self, selected=("kitchen",), **values):
        data = MultiDict({
            "source_ticket_id": str(self.ticket.id), "object_type": "license_agreement", "object_id": str(self.agreement.id),
            "craft_activity_id": str(self.craft.id), "commission_type_id": "100", "assignment_type": "internal",
            "craftsman_user_id": str(self.craftsman.id), "title": "Küche reparieren", "priority": "normal",
            "position_service_catalog_id": "600", "position_units": "1", "position_text": "Bitte reparieren",
            "notify_tenants": "false", "notify_on_completion": "false", "ticket_photo_selection_ready": "true",
            **values,
        })
        for file_guid in selected:
            data.add("ticket_photo_guid", file_guid)
        return data

    def post_order(self, data=None):
        response = self.client.post("/admin/commissions/new", data=self.form() if data is None else data)
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True)[:5000])
        with self.client.session_transaction() as session:
            flashes = session.get("_flashes", [])
        self.assertEqual(self.models.Commission.query.count(), 1, (response.location, flashes))
        return self.models.Commission.query.one()

    def publish(self, commission):
        commission.erp_id = 9001
        commission.erp_idnum = "A-9001"
        commission.local_status = "open"
        self.db.session.commit()

    def test_form_loads_ticket_photo_selection_and_allows_additional_uploads(self):
        response = self.client.get(f"/admin/commissions/new?ticket_id={self.ticket.id}")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('id="ticketPhotoSelection"', html)
        self.assertIn(f'/admin/tickets/{self.ticket.id}/photos', html)
        self.assertIn('id="initialTicketPhotoSelection">null</script>', html)
        self.assertIn('name="photo"', html)
        self.assertIn("multiple", html)
        self.assertIn("ticket_photo_selection.js", html)

    def test_photo_endpoint_only_lists_current_images_and_returns_authenticated_previews(self):
        response = self.client.get(f"/admin/tickets/{self.ticket.id}/photos")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["file_guid"] for item in response.json["items"]], ["kitchen", "bathroom"])
        self.remote.download_document_content.assert_not_called()
        preview = self.client.get(response.json["items"][0]["thumbnail_url"])
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.mimetype, "image/jpeg")
        self.assertIn("private", preview.headers["Cache-Control"])
        with Image.open(BytesIO(preview.data)) as image:
            self.assertLessEqual(max(image.size), 480)
        self.assertEqual(self.models.Commission.query.count(), 0)

    def test_only_selected_ticket_photos_and_additional_photos_are_saved_before_the_worker(self):
        data = self.form()
        data.add("photo", (BytesIO(self.image("green")), "zusatz.png"))
        data.add("photo", (BytesIO(self.image("yellow")), "detail.png"))
        commission = self.post_order(data)
        photos = self.models.CommissionPhoto.query.filter_by(commission_id=commission.id).all()
        self.assertEqual(len(photos), 3)
        self.assertTrue(all(self.photo_service.commission_photo_path(photo.storage_name).is_file() for photo in photos))
        self.assertEqual(commission.raw_payload["ticket_photo_selection"]["selected_file_guids"], ["kitchen"])
        self.assertIn("commission_scope", commission.raw_payload)
        self.assertEqual(self.ticket.state, "converted")
        self.assertEqual([call.kwargs["file_guid"] for call in self.remote.download_document_content.call_args_list], ["kitchen"])
        self.assertEqual(self.models.CommissionAttachment.query.count(), 0)

    def test_removing_all_photos_is_explicit_and_the_worker_does_not_add_them_back(self):
        commission = self.post_order(self.form(selected=()))
        self.assertEqual(self.models.CommissionPhoto.query.count(), 0)
        self.assertEqual(commission.raw_payload["ticket_photo_selection"]["selected_file_guids"], [])
        self.remote.download_document_content.assert_not_called()
        self.publish(commission)
        self.rows.append({"FileGuid": "later", "FileName": "spaeter.jpg"})
        for _ in range(2):
            result = self.document_sync.sync_commission_documents(commission, self.remote, force=True)
            self.db.session.commit()
            self.assertEqual(result["skipped_ticket_photos"], 3)
        attachments = self.models.CommissionAttachment.query.all()
        self.assertEqual([item.original_filename for item in attachments], ["hinweise.pdf"])
        self.assertEqual(self.models.CommissionPhoto.query.count(), 0)
        self.assertEqual([call.kwargs["file_guid"] for call in self.remote.download_document_content.call_args_list], ["document"])

    def test_uploaded_selection_does_not_reappear_as_ticket_or_commission_attachments(self):
        commission = self.post_order()
        photo = self.models.CommissionPhoto.query.one()
        self.publish(commission)
        self.commission_rows.append({"FileGuid": "uploaded", "FileName": "kueche.jpg", "ExternalDocumentId": photo.external_document_id})
        self.remote.download_document_content.reset_mock()
        result = self.document_sync.sync_commission_documents(commission, self.remote, force=True)
        self.db.session.commit()
        self.assertEqual(result["linked_photos"], 1)
        self.assertEqual(result["skipped_ticket_photos"], 2)
        self.assertEqual(self.models.CommissionPhoto.query.count(), 1)
        self.assertEqual([item.original_filename for item in self.models.CommissionAttachment.query.all()], ["hinweise.pdf"])

    def test_worker_rejects_image_content_even_when_the_filename_looks_like_a_document(self):
        commission = self.post_order(self.form(selected=()))
        self.publish(commission)
        self.rows.append({"FileGuid": "disguised", "FileName": "falscher-name.pdf"})
        self.contents["disguised"] = self.image("purple")
        result = self.document_sync.sync_commission_documents(commission, self.remote, force=True)
        self.db.session.commit()
        self.assertEqual(result["skipped_ticket_photos"], 3)
        self.assertEqual([item.external_file_guid for item in self.models.CommissionAttachment.query.all()], ["document"])

    def test_legacy_commissions_keep_their_existing_ticket_documents(self):
        commission = self.post_order(self.form(selected=()))
        commission.raw_payload = {"commission_scope": commission.raw_payload["commission_scope"]}
        self.publish(commission)
        result = self.document_sync.sync_commission_documents(commission, self.remote, force=True)
        self.db.session.commit()
        self.assertEqual(result["skipped_ticket_photos"], 0)
        self.assertEqual(self.models.CommissionAttachment.query.count(), 3)

    def test_erp_updates_preserve_the_confirmed_photo_selection(self):
        commission = self.post_order()
        self.publish(commission)
        expected = dict(commission.raw_payload["ticket_photo_selection"])
        updated = self.gateway.upsert_commission({
            "Id": commission.erp_id, "IdNum": commission.erp_idnum,
            "CommissionStatus": {"Id": 7, "Code": "Offen"},
        }, sync_signature="updated-by-erp")
        self.db.session.commit()
        self.assertEqual(updated.raw_payload["ticket_photo_selection"], expected)
        self.assertEqual(updated.raw_payload["sync_signature"], "updated-by-erp")

    def test_a_failed_download_rolls_back_the_order_and_removes_already_stored_photos(self):
        def download(entity_name, file_guid):
            if file_guid == "bathroom":
                raise RuntimeError("Ticketfoto vorübergehend nicht erreichbar.")
            return self.contents[file_guid]
        self.remote.download_document_content.side_effect = download
        response = self.client.post("/admin/commissions/new", data=self.form(selected=("kitchen", "bathroom")))
        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as session:
            self.assertIn(("danger", "Ticketfoto vorübergehend nicht erreichbar."), session.get("_flashes", []))
        self.assertEqual(self.models.Commission.query.count(), 0)
        self.assertEqual(self.models.CommissionPhoto.query.count(), 0)
        self.assertEqual(self.models.ErpOutboxOperation.query.count(), 0)
        self.assertEqual(self.models.CommissionTicketLink.query.count(), 0)
        self.assertEqual(self.ticket.state, "new")
        self.assertEqual([path for path in Path(self.directory_path.name).rglob("*") if path.is_file()], [])

    def test_unconfirmed_or_unknown_photos_cannot_create_an_order(self):
        for data in (self.form(ticket_photo_selection_ready=""), self.form(selected=("other-ticket",))):
            response = self.client.post("/admin/commissions/new", data=data)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.models.Commission.query.count(), 0)
            self.assertEqual(self.models.ErpOutboxOperation.query.count(), 0)
        self.remote.download_document_content.assert_not_called()

    def test_invalid_additional_photo_rolls_back_the_ticket_selection_and_ticket_state(self):
        data = self.form()
        data.add("photo", (BytesIO(b"kein Bild"), "ungueltig.jpg"))
        response = self.client.post("/admin/commissions/new", data=data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.models.Commission.query.count(), 0)
        self.assertEqual(self.models.CommissionPhoto.query.count(), 0)
        self.assertEqual(self.models.ErpOutboxOperation.query.count(), 0)
        self.assertEqual(self.ticket.state, "new")
        self.assertEqual([path for path in Path(self.directory_path.name).rglob("*") if path.is_file()], [])

    def test_pending_approval_keeps_selected_photos_local_without_releasing_an_erp_operation(self):
        self.creator.internal_commission_approver_user_id = self.reviewer.id
        self.db.session.commit()
        commission = self.post_order()
        self.assertEqual(commission.approval_state, "pending")
        self.assertEqual(self.models.CommissionPhoto.query.count(), 1)
        self.assertEqual(self.models.ErpOutboxOperation.query.count(), 0)
        self.assertEqual(self.models.PushDelivery.query.count(), 0)
        self.assertEqual(commission.raw_payload["ticket_photo_selection"]["selected_file_guids"], ["kitchen"])

    def test_missing_and_foreign_ticket_photo_identifiers_are_not_downloaded(self):
        for suffix in ("other-ticket", "document", "obsolete"):
            response = self.client.get(f"/admin/tickets/{self.ticket.id}/photos/{suffix}/content")
            self.assertEqual(response.status_code, 404)
        self.remote.download_document_content.assert_not_called()

    def test_craftsmen_cannot_access_ticket_photo_previews(self):
        self.craftsman.can_manage_offers = True
        self.db.session.commit()
        self.login(self.craftsman)
        for path in (f"/admin/tickets/{self.ticket.id}/photos", f"/admin/tickets/{self.ticket.id}/photos/kitchen/content"):
            self.assertEqual(self.client.get(path).status_code, 403)
        self.remote.get_document_rows.assert_not_called()

    def test_loading_failure_is_reported_and_does_not_mean_an_empty_selection(self):
        self.remote.get_document_rows.side_effect = RuntimeError("Nicht erreichbar")
        with self.assertLogs(self.app.logger, level="ERROR"):
            response = self.client.get(f"/admin/tickets/{self.ticket.id}/photos")
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("items", response.json)
        response = self.client.post("/admin/commissions/new", data=self.form(selected=()))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.models.Commission.query.count(), 0)
