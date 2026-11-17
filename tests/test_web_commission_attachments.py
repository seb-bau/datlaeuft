import ast
import base64
from datetime import timedelta
from io import BytesIO
from unittest.mock import Mock, patch
import unittest

from flask_jwt_extended import create_access_token
from sqlalchemy import or_

import test_ticket_attachment_selection as attachment_fixtures


class WebCommissionAttachmentTests(unittest.TestCase):
    login = attachment_fixtures.TicketAttachmentSelectionTests.login
    start_patch = attachment_fixtures.TicketAttachmentSelectionTests.start_patch
    image = attachment_fixtures.TicketAttachmentSelectionTests.image
    form = attachment_fixtures.TicketAttachmentSelectionTests.form
    post_order = attachment_fixtures.TicketAttachmentSelectionTests.post_order
    publish = attachment_fixtures.TicketAttachmentSelectionTests.publish

    @classmethod
    def setUpClass(cls):
        attachment_fixtures.TicketAttachmentSelectionTests.setUpClass.__func__(cls)
        cls.api.commission_attachment_path = cls.attachment_service.commission_attachment_path
        source = ast.parse((cls.root / "app/erp_sync.py").read_text())
        functions = [node for node in source.body if isinstance(node, ast.FunctionDef) and node.name in {
            "enqueue_pending_commission_attachment_uploads", "execute_outbox_operation", "fail_outbox_operation",
        }]
        namespace = {name: getattr(cls.models, name) for name in dir(cls.models)}
        namespace.update(db=cls.db, or_=or_, Any=object, WowiportService=object, timedelta=timedelta,
                         ERP_OPERATION_STATUS=cls.gateway.ERP_OPERATION_STATUS,
                         enqueue_erp_operation=cls.erp.enqueue_erp_operation, require_erp_id=lambda commission: None,
                         audit=lambda *args, **kwargs: None)
        exec(compile(ast.Module(body=functions, type_ignores=[]), "erp_sync.py", "exec"), namespace)
        cls.erp_functions = namespace

    def setUp(self):
        attachment_fixtures.TicketAttachmentSelectionTests.setUp(self)

    def order_with_uploads(self, uploads=None):
        data = self.form(selected=())
        for content, filename in uploads or [(self.contents["document"], "hinweise.pdf")]:
            data.add("photo", (BytesIO(content), filename))
        return self.post_order(data)

    def test_mixed_uploads_keep_photos_and_store_documents_for_the_app(self):
        pdf = self.contents["document"]
        commission = self.order_with_uploads([(pdf, "Hinweise.PDF"), (b"PK document", "auftrag.docx"), (self.image("red"), "foto.png")])
        attachments = self.models.CommissionAttachment.query.order_by(self.models.CommissionAttachment.id).all()
        self.assertEqual([item.mime_type for item in attachments], ["application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"])
        self.assertEqual(self.models.CommissionPhoto.query.count(), 1)
        self.assertEqual(self.attachment_service.commission_attachment_path(attachments[0].storage_name).read_bytes(), pdf)
        self.assertEqual(attachments[0].source, "web")
        self.assertEqual(attachments[0].uploaded_by_user_id, self.creator.id)
        headers = {"Authorization": "Bearer " + create_access_token(identity=str(self.craftsman.id))}
        response = self.client.get(f"/api/commissions/{commission.id}/attachments", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json["items"]), 2)
        item = next(item for item in response.json["items"] if item["mime_type"] == "application/pdf")
        self.assertFalse(item["is_image"])
        self.assertIsNone(item["thumbnail_path"])
        content = self.client.get(item["content_path"], headers=headers)
        self.assertEqual(content.status_code, 200)
        self.assertEqual(content.data, pdf)
        self.assertEqual(content.mimetype, "application/pdf")
        self.assertTrue(content.headers["Content-Disposition"].startswith("inline"))
        self.assertEqual(content.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("private", content.headers["Cache-Control"])
        content.close()

    def test_pdfs_are_recognized_without_a_filename_extension(self):
        self.order_with_uploads([(self.contents["document"], "Auftragsunterlagen")])
        attachment = self.models.CommissionAttachment.query.one()
        self.assertEqual(attachment.mime_type, "application/pdf")
        self.assertEqual(attachment.original_filename, "Auftragsunterlagen")

    def test_documents_can_be_added_later_and_downloaded_in_the_web(self):
        commission = self.post_order(self.form(selected=()))
        response = self.client.post(f"/admin/commissions/{commission.id}/attachments", data={
            "photo": [(BytesIO(self.contents["document"]), "hinweise.pdf"), (BytesIO(b"text"), "notiz.txt")],
            "photo_caption": "Wichtige Unterlagen",
        })
        self.assertEqual(response.status_code, 302)
        attachments = self.models.CommissionAttachment.query.all()
        self.assertEqual(len(attachments), 2)
        for item in attachments:
            self.assertEqual(item.caption, "Wichtige Unterlagen")
            response = self.client.get(f"/admin/commissions/{commission.id}/attachments/{item.id}/content")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, item.mime_type)
            self.assertIn("private", response.headers["Cache-Control"])
            response.close()
        self.assertTrue(response.headers["Content-Disposition"].startswith("attachment"))

    def test_duplicate_documents_do_not_leave_extra_files_or_rows(self):
        commission = self.order_with_uploads()
        for _ in range(2):
            self.client.post(f"/admin/commissions/{commission.id}/attachments", data={"photo": (BytesIO(self.contents["document"]), "kopie.pdf")})
        self.assertEqual(self.models.CommissionAttachment.query.count(), 1)
        self.assertEqual(len(list(self.attachment_service.commission_attachment_root().rglob("*.pdf"))), 1)

    def test_empty_or_oversized_files_roll_back_the_order_and_all_written_files(self):
        for content, filename in [(b"", "leer.pdf"), (b"123456789", "gross.pdf")]:
            with self.subTest(filename=filename), patch.object(self.admin, "MAX_ATTACHMENT_BYTES", 8):
                data = self.form(selected=())
                data.add("photo", (BytesIO(b"valid"), "erste.txt"))
                data.add("photo", (BytesIO(content), filename))
                response = self.client.post("/admin/commissions/new", data=data)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.models.Commission.query.count(), 0)
                self.assertEqual(self.models.CommissionAttachment.query.count(), 0)
                self.assertFalse(any(path.is_file() for path in self.attachment_service.commission_attachment_root().rglob("*")))

    def test_invalid_pdf_and_active_web_formats_are_served_as_downloads(self):
        commission = self.order_with_uploads([(b"<html>test</html>", "falsch.pdf"), (b"<svg></svg>", "bild.svg")])
        for item in self.models.CommissionAttachment.query.all():
            self.assertEqual(item.mime_type, "application/octet-stream")
            response = self.client.get(f"/admin/commissions/{commission.id}/attachments/{item.id}/content")
            self.assertTrue(response.headers["Content-Disposition"].startswith("attachment"))
            response.close()

    def test_attachment_access_requires_login_assignment_and_matching_order(self):
        commission = self.order_with_uploads()
        attachment = self.models.CommissionAttachment.query.one()
        content_path = f"/api/commissions/{commission.id}/attachments/{attachment.id}/content"
        self.assertEqual(self.client.get(content_path).status_code, 401)
        other = self.models.User(email="other@example.invalid", display_name="Andere Person", craftsman_erp_id=9999)
        self.db.session.add(other)
        self.db.session.commit()
        headers = {"Authorization": "Bearer " + create_access_token(identity=str(other.id))}
        self.assertEqual(self.client.get(content_path, headers=headers).status_code, 403)
        self.assertEqual(self.client.get(f"/admin/commissions/{commission.id + 100}/attachments/{attachment.id}/content").status_code, 404)
        self.login(self.craftsman)
        self.assertEqual(self.client.post(f"/admin/commissions/{commission.id}/attachments", data={"photo": (BytesIO(b"text"), "notiz.txt")}).status_code, 403)

    def test_pending_documents_are_queued_after_the_erp_number_is_known(self):
        commission = self.order_with_uploads()
        attachment = self.models.CommissionAttachment.query.one()
        enqueue = self.erp_functions["enqueue_pending_commission_attachment_uploads"]
        self.assertEqual(enqueue(), 0)
        self.publish(commission)
        self.assertEqual(enqueue(), 1)
        self.assertEqual(enqueue(), 1)
        operations = self.models.ErpOutboxOperation.query.filter_by(operation_type="upload_commission_attachment").all()
        self.assertEqual(len(operations), 1)
        self.assertEqual(operations[0].payload, {"attachment_id": attachment.id})

    def test_successful_erp_upload_is_reused_by_document_sync(self):
        commission = self.order_with_uploads()
        attachment = self.models.CommissionAttachment.query.one()
        self.publish(commission)
        self.erp_functions["enqueue_pending_commission_attachment_uploads"]()
        operation = self.models.ErpOutboxOperation.query.filter_by(operation_type="upload_commission_attachment").one()
        operation.state = "processing"
        self.db.session.commit()
        self.remote.upload_commission_attachment.return_value = {"Id": 99, "FileGuid": "uploaded-pdf"}
        self.erp_functions["execute_outbox_operation"](operation.id, self.remote)
        self.assertEqual(attachment.erp_sync_state, "synced")
        self.assertEqual(attachment.external_file_guid, "uploaded-pdf")
        self.commission_rows.append({"Id": 99, "FileGuid": "uploaded-pdf", "ExternalDocumentId": attachment.external_document_id, "FileName": "hinweise.pdf"})
        self.document_sync.sync_commission_documents(commission, self.remote, force=True)
        self.db.session.commit()
        self.assertEqual(self.models.CommissionAttachment.query.count(), 1)
        self.assertTrue(attachment.active)
        self.assertEqual(self.attachment_service.commission_attachment_path(attachment.storage_name).read_bytes(), self.contents["document"])

    def test_sync_matches_a_local_upload_even_when_the_receipt_had_no_file_guid(self):
        commission = self.order_with_uploads()
        attachment = self.models.CommissionAttachment.query.one()
        self.publish(commission)
        self.commission_rows.append({"Id": 99, "FileGuid": "uploaded-pdf", "ExternalDocumentId": attachment.external_document_id, "FileName": "hinweise.pdf"})
        self.document_sync.sync_commission_documents(commission, self.remote, force=True)
        self.db.session.commit()
        self.assertEqual(self.models.CommissionAttachment.query.count(), 1)
        self.assertEqual(attachment.external_file_guid, "uploaded-pdf")
        self.remote.download_document_content.assert_not_called()

    def test_erp_failure_keeps_the_document_available_and_records_the_error(self):
        commission = self.order_with_uploads()
        self.publish(commission)
        self.erp_functions["enqueue_pending_commission_attachment_uploads"]()
        operation = self.models.ErpOutboxOperation.query.filter_by(operation_type="upload_commission_attachment").one()
        self.erp_functions["fail_outbox_operation"](operation.id, RuntimeError("Wowiport nicht erreichbar"))
        attachment = self.models.CommissionAttachment.query.one()
        self.assertEqual(attachment.erp_sync_state, "failed")
        self.assertEqual(attachment.erp_sync_message, "Wowiport nicht erreichbar")
        self.assertTrue(attachment.active)

    def test_wowiport_document_upload_preserves_pdf_bytes_and_is_idempotent(self):
        commission = self.order_with_uploads()
        attachment = self.models.CommissionAttachment.query.one()
        self.publish(commission)
        service = object.__new__(self.gateway.WowiportService)
        service.client = Mock()
        service.get_document_rows = Mock(return_value=[])
        service.client._rest_adapter.post.return_value.data = {"Id": 99, "FileGuid": "uploaded-pdf"}
        self.gateway.commission_attachment_path = self.attachment_service.commission_attachment_path
        self.settings.settings.set(self.settings.SettingKeys.WOWI_DOCUMENT_FILE_TYPE_ID, 1)
        self.settings.settings.set(self.settings.SettingKeys.WOWI_DOCUMENT_PRIVACY_CATEGORY_ID, 1)
        result = service.upload_commission_attachment(commission, attachment)
        self.assertEqual(result["FileGuid"], "uploaded-pdf")
        payload = service.client._rest_adapter.post.call_args.kwargs["data"]
        self.assertEqual(base64.b64decode(payload["Contents"]), self.contents["document"])
        self.assertEqual(payload["ExternalDocumentId"], attachment.external_document_id)
        service.get_document_rows.return_value = [result | {"ExternalDocumentId": attachment.external_document_id}]
        self.assertEqual(service.upload_commission_attachment(commission, attachment), service.get_document_rows.return_value[0])
        service.client._rest_adapter.post.assert_called_once()
