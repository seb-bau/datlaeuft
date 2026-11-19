import importlib
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import BytesIO
import os
import sys
from threading import Barrier
import unittest
from unittest.mock import patch

from flask import Flask, template_rendered
from flask_login import login_user
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from werkzeug.datastructures import FileStorage, MultiDict

import test_ticket_attachment_selection as fixtures


class CommissionFollowupTests(unittest.TestCase):
    login = fixtures.TicketAttachmentSelectionTests.login
    start_patch = fixtures.TicketAttachmentSelectionTests.start_patch
    image = fixtures.TicketAttachmentSelectionTests.image

    @classmethod
    def setUpClass(cls):
        fixtures.TicketAttachmentSelectionTests.setUpClass.__func__(cls)
        sys.modules.pop(cls.package_name + ".commission_followup", None)
        cls.followup = importlib.import_module(cls.package_name + ".commission_followup")
        sys.modules.pop(cls.package_name + ".commission_workflow_service", None)
        commission_workflow = importlib.import_module(cls.package_name + ".commission_workflow_service")
        cls.admin.cancel_commission = commission_workflow.cancel_commission
        cls.admin.CommissionWorkflowError = commission_workflow.CommissionWorkflowError
        for endpoint in ("process_info_web.local_official", "process_info_web.office_chat_read", "process_info_web.office_chat_status"):
            cls.app.add_url_rule("/test/" + endpoint + "/<int:commission_id>", endpoint=endpoint, view_func=lambda commission_id: "")
        cls.tables.append(cls.models.TicketCraftFallback.__table__)
        for name in ("can_handle_commission_review", "review_commission", "resolve_commission_review", "commission_reissue_data", "copy_commission_files"):
            setattr(cls.admin, name, getattr(cls.followup, name))

    def setUp(self):
        fixtures.TicketAttachmentSelectionTests.setUp(self)
        self.source = self.models.Commission(
            erp_id=9001, erp_idnum="A-9001", title="Bad reparieren", created_by=self.creator,
            user=self.craftsman, craftsman_erp_id=self.craftsman.craftsman_erp_id,
            assignment_type="internal", local_status="completion_review", completion_successful=False,
            completion_reason="Nicht zuständig", rejected_at=self.models.utc_now(),
            completion_submitted_at=self.models.utc_now(), completion_submitted_by=self.craftsman,
            license_agreement_id=self.agreement.erp_id, company_code_erp_id=self.company.erp_id,
            commission_type_erp_id=100, damage_location="Badezimmer", contact_person="Erika",
            execution_from=self.models.utc_now(), notify_on_completion=True,
        )
        self.source.positions.append(self.models.CommissionPosition(
            position_index=0,
            article_number="7", service_catalog_erp_id=600, craft_activity_erp_id=500,
            commission_text="Armatur tauschen", description="Armatur tauschen", internal_description="Schlüssel abholen", units=2,
        ))
        self.db.session.add(self.source)
        self.reviewer.craftsman_erp_id = 3031
        self.db.session.commit()

    def form(self, **values):
        data = fixtures.TicketAttachmentSelectionTests.form(self, selected=(), **values)
        for key in ("source_ticket_id", "ticket_attachment_selection_ready"):
            data.pop(key, None)
        data["source_commission_id"] = str(self.source.id)
        return data

    def view(self, path):
        contexts = []
        def capture(sender, template, context, **extra):
            contexts.append(context)
        with template_rendered.connected_to(capture, self.app):
            response = self.client.get(path)
        return response, contexts[-1] if contexts else {}

    def test_draft_copies_planning_fields_without_resolving_the_review(self):
        response, context = self.view(f"/admin/commissions/new?reissue_id={self.source.id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Auftrag neu erteilen", response.get_data(as_text=True))
        values = context["form_values"]
        self.assertEqual(values["object_id"], self.agreement.id)
        self.assertEqual(values["craft_activity_id"], self.craft.id)
        self.assertEqual(values["craftsman_user_id"], self.craftsman.id)
        self.assertEqual(values["title"], self.source.title)
        self.assertEqual(values["contact_person"], "Erika")
        self.assertEqual(context["position_rows"][0]["internal_description"], "Schlüssel abholen")
        self.assertEqual(context["position_rows"][0]["article_number"], "7")
        self.assertIsNone(self.source.review_resolved_at)
        self.assertTrue(self.source.requires_office_review)
        self.assertEqual(self.models.Commission.query.count(), 1)

    def test_reissue_creates_a_new_order_with_another_worker_and_copies_files(self):
        self.source.creditor_erp_id = 4040
        with self.app.test_request_context():
            login_user(self.creator)
            photo, _ = self.admin.save_web_commission_photo(
                self.source, FileStorage(stream=BytesIO(self.image("red")), filename="bad.png"), "Vorher",
            )
            attachment, _ = self.admin.save_web_commission_upload(
                self.source, FileStorage(stream=BytesIO(b"%PDF-1.4\n%%EOF\n"), filename="hinweis.pdf"), "Hinweise", [],
            )
        attachment.file_type_erp_id = 12
        attachment.privacy_category_name = "Intern"
        self.db.session.commit()
        response = self.client.post("/admin/commissions/new", data=self.form(craftsman_user_id=str(self.reviewer.id)))
        self.assertEqual(response.status_code, 302)
        replacement = self.models.Commission.query.filter(self.models.Commission.id != self.source.id).one()
        self.assertEqual(replacement.user_id, self.reviewer.id)
        self.assertIsNone(replacement.creditor_erp_id)
        self.assertNotEqual(replacement.public_id, self.source.public_id)
        self.assertIsNone(replacement.erp_id)
        self.assertEqual(replacement.reissued_from, self.source)
        self.assertEqual(self.source.review_resolution, "reissued")
        self.assertEqual(self.source.review_resolved_by, self.creator)
        self.assertFalse(self.source.requires_office_review)
        self.assertEqual(self.source.display_local_status, "rejected")
        self.assertIsNone(self.source.completed_at)
        self.assertFalse(self.source.completion_successful)
        self.assertFalse(self.source.is_actionable)
        self.assertEqual(len(replacement.photos), 1)
        self.assertEqual(len(replacement.attachments), 1)
        self.assertNotEqual(replacement.photos[0].storage_name, photo.storage_name)
        self.assertNotEqual(replacement.attachments[0].external_document_id, attachment.external_document_id)
        self.assertEqual(replacement.attachments[0].file_type_erp_id, 12)
        self.assertEqual(replacement.attachments[0].privacy_category_name, "Intern")
        self.assertEqual(self.attachment_service.commission_attachment_path(replacement.attachments[0].storage_name).read_bytes(), b"%PDF-1.4\n%%EOF\n")
        self.assertEqual(self.models.ErpOutboxOperation.query.filter_by(operation_type="create_commission").count(), 1)
        self.assertEqual(self.models.ErpOutboxOperation.query.filter_by(operation_type="complete_commission").count(), 0)
        self.client.post("/admin/commissions/new", data=self.form())
        self.assertEqual(self.models.Commission.query.count(), 2)

    def test_failed_reissue_keeps_the_original_review_open(self):
        response = self.client.post("/admin/commissions/new", data=self.form(position_units="-1"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.source.requires_office_review)
        self.assertEqual(self.models.Commission.query.count(), 1)
        self.assertEqual(self.models.ErpOutboxOperation.query.count(), 0)

    def test_submitted_draft_keeps_the_original_planning_and_all_position_fields(self):
        self.source.creditor_erp_id = 4040
        self.source.crafts_process_erp_id = 5050
        self.db.session.commit()
        draft = self.followup.commission_reissue_data(self.source)
        data = MultiDict(draft["form_values"])
        data["source_commission_id"] = str(self.source.id)
        for position in draft["position_rows"]:
            for key, field in (("article_number", "position_article_number"), ("service_catalog_id", "position_service_catalog_id"),
                               ("commission_text", "position_text"), ("internal_description", "position_internal_description"), ("units", "position_units")):
                data.add(field, position[key])
        response = self.client.post("/admin/commissions/new", data=data)
        self.assertEqual(response.status_code, 302)
        replacement = self.models.Commission.query.filter(self.models.Commission.id != self.source.id).one()
        for name in ("title", "damage_location", "contact_person", "company_code_erp_id", "license_agreement_id", "creditor_erp_id", "crafts_process_erp_id", "user_id"):
            self.assertEqual(getattr(replacement, name), getattr(self.source, name), name)
        for name in ("article_number", "commission_text", "internal_description", "units", "service_catalog_erp_id"):
            self.assertEqual(getattr(replacement.positions[0], name), getattr(self.source.positions[0], name), name)
        self.assertEqual(replacement.execution_from.replace(tzinfo=None), self.source.execution_from.replace(tzinfo=None, second=0, microsecond=0))

    def test_missing_source_file_rolls_back_the_new_order_and_copied_files(self):
        with self.app.test_request_context():
            login_user(self.creator)
            photo, _ = self.admin.save_web_commission_photo(
                self.source, FileStorage(stream=BytesIO(self.image("red")), filename="bad.png"), "Vorher",
            )
            attachment, _ = self.admin.save_web_commission_upload(
                self.source, FileStorage(stream=BytesIO(b"%PDF-1.4\n%%EOF\n"), filename="hinweis.pdf"), "Hinweise", [],
            )
        self.db.session.commit()
        self.attachment_service.commission_attachment_path(attachment.storage_name).unlink()
        directory = self.photo_service.commission_photo_path(photo.storage_name).parent
        existing_files = set(directory.rglob("*"))
        response = self.client.post("/admin/commissions/new", data=self.form())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.models.Commission.query.count(), 1)
        self.assertTrue(self.source.requires_office_review)
        self.assertEqual(self.models.ErpOutboxOperation.query.filter_by(operation_type="create_commission").count(), 0)
        self.assertEqual({path for path in directory.rglob("*") if path.is_file()}, {path for path in existing_files if path.is_file()})

    def test_acknowledging_is_idempotent_and_removes_only_the_open_review(self):
        for _ in range(2):
            response = self.client.post(f"/admin/commissions/{self.source.id}/acknowledge")
            self.assertEqual(response.status_code, 302)
        self.assertEqual(self.source.review_resolution, "acknowledged")
        self.assertEqual(self.source.review_resolved_by, self.creator)
        self.assertEqual(self.source.display_local_status, "rejected")
        self.assertFalse(self.source.needs_completion_review)
        self.assertEqual(self.models.AuditLog.query.filter_by(action="commission.review.acknowledged").count(), 1)
        self.assertEqual(self.models.ErpOutboxOperation.query.count(), 0)
        _, context = self.view("/admin/commission-reviews")
        self.assertEqual(context["items"], [])
        _, context = self.view("/admin/commissions?view=active")
        self.assertEqual(context["items"], [])
        response, context = self.view("/admin/commissions")
        self.assertEqual([item.id for item in context["items"]], [self.source.id])
        self.assertIn("Zur Kenntnis genommen", response.get_data(as_text=True))

    def test_review_and_status_filters_distinguish_rejection_from_incomplete_work(self):
        response, context = self.view("/admin/commission-reviews?status=rejected")
        self.assertEqual([item.id for item in context["items"]], [self.source.id])
        self.assertIn("Neu erteilen", response.get_data(as_text=True))
        _, context = self.view("/admin/commission-reviews?status=completion_review")
        self.assertEqual(context["items"], [])
        response, context = self.view("/admin/commissions?local_status=rejected")
        self.assertEqual([item.id for item in context["items"]], [self.source.id])
        self.assertIn("Abgelehnt", response.get_data(as_text=True))

    def test_office_can_cancel_an_order_from_details_and_it_leaves_the_review_list(self):
        commission = self.models.Commission(
            erp_id=9002, erp_idnum="A-9002", title="Abfluss prüfen", created_by=self.creator,
            user=self.craftsman, local_status="in_progress", status_id=7, status_code="Accepted",
            execution_to=self.models.utc_now() - timedelta(days=1),
        )
        self.db.session.add(commission)
        self.db.session.commit()
        url = f"/admin/commissions/{commission.id}"
        response, context = self.view("/admin/commission-reviews")
        self.assertEqual(response.status_code, 200)
        self.assertIn(commission.id, {item.id for item in context["items"]})
        detail = self.client.get(url).get_data(as_text=True)
        self.assertIn(f'action="{url}/cancel"', detail)
        self.assertIn("window.confirm('Diesen Auftrag wirklich stornieren?", detail)

        response = self.client.post(url + "/cancel", data={"return_to": "/admin/commissions?view=active"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("return_to=", response.location)
        self.db.session.refresh(commission)
        self.assertEqual(commission.local_status, "canceled")
        self.assertEqual(commission.erp_sync_state, "pending")
        self.assertIsNone(commission.rejected_at)
        self.assertIsNone(commission.deleted_at)
        audit = self.models.AuditLog.query.filter_by(action="commission.cancel", entity_id=str(commission.id)).one()
        self.assertEqual(audit.actor_user_id, self.creator.id)

        _, context = self.view("/admin/commission-reviews")
        self.assertIn(self.source.id, {item.id for item in context["items"]})
        self.assertNotIn(commission.id, {item.id for item in context["items"]})
        _, context = self.view("/admin/commissions?view=active")
        self.assertNotIn(commission.id, {item.id for item in context["items"]})
        detail = self.client.get(url).get_data(as_text=True)
        self.assertNotIn(f'action="{url}/cancel"', detail)
        self.assertIn("Stornierung ausstehend", detail)
        self.assertEqual(self.client.post(url + "/cancel").status_code, 302)
        self.assertEqual(self.models.ErpOutboxOperation.query.filter_by(commission_id=commission.id).count(), 1)

    def test_office_cancellation_requires_staff_and_an_open_order_in_wowiport(self):
        commission = self.models.Commission(
            erp_id=9003, erp_idnum="A-9003", title="Lüftung prüfen", user=self.craftsman,
            local_status="waiting_for_acceptance", status_id=12, status_code="WaitForCraftsmanFeedback",
        )
        self.db.session.add(commission)
        self.db.session.commit()
        url = f"/admin/commissions/{commission.id}"
        self.login(self.craftsman)
        self.assertEqual(self.client.post(url + "/cancel").status_code, 403)
        self.login(self.creator)
        for status, erp_status in (("done", 9), ("awaiting_invoice", 11), ("recorded", None)):
            with self.subTest(status=status):
                commission.local_status = status
                commission.status_id = erp_status
                self.db.session.commit()
                self.assertNotIn(f'action="{url}/cancel"', self.client.get(url).get_data(as_text=True))
                self.assertEqual(self.client.post(url + "/cancel").status_code, 302)
                self.db.session.refresh(commission)
                self.assertEqual(commission.local_status, status)
        self.assertEqual(self.client.post(f"/admin/commissions/{self.source.id}/cancel").status_code, 302)
        self.assertTrue(self.source.requires_office_review)
        self.assertEqual(self.models.ErpOutboxOperation.query.count(), 0)

    def test_other_staff_cannot_resolve_or_copy_someone_elses_review(self):
        self.login(self.reviewer)
        for path in (f"/admin/commissions/new?reissue_id={self.source.id}",):
            self.assertEqual(self.client.get(path).status_code, 302)
        self.client.post(f"/admin/commissions/{self.source.id}/acknowledge")
        self.client.post("/admin/commissions/new", data=self.form())
        self.assertIsNone(self.source.review_resolved_at)
        self.assertEqual(self.models.Commission.query.count(), 1)
        self.source.local_official = self.reviewer
        self.db.session.commit()
        self.client.post(f"/admin/commissions/{self.source.id}/acknowledge")
        self.assertEqual(self.source.review_resolved_by, self.reviewer)

    def test_locking_retries_mariadb_snapshot_conflicts_before_any_changes(self):
        for error_code in (1020, 1205, 1213):
            with self.subTest(error_code=error_code):
                error = OperationalError("SELECT", {}, Exception(error_code, "Parallel geändert"))
                with patch.object(sa.orm.Query, "one_or_none", side_effect=[error, self.source]) as query:
                    item = self.followup.review_commission(self.source.id, self.creator, lock=True)
                self.assertEqual(query.call_count, 2)
                self.assertEqual(item, self.source)
                self.assertIsNone(item.review_resolved_at)

    @unittest.skipUnless(os.environ.get("DATLAEUFT_TEST_DATABASE_URL", "").startswith("mysql"), "Benötigt MariaDB für gleichzeitige Neuvergaben")
    def test_simultaneous_reissues_resolve_the_original_exactly_once(self):
        database_url = os.environ["DATLAEUFT_TEST_DATABASE_URL"]
        if not (make_url(database_url).database or "").startswith("datlaeuft_test_"):
            self.fail("Diese Prüfung benötigt eine separate datlaeuft_test-Datenbank.")
        app = Flask("commission-followup-concurrency")
        app.config.update(SQLALCHEMY_DATABASE_URI=database_url, TESTING=True)
        self.db.init_app(app)
        metadata = sa.MetaData()
        for model in (self.models.User, self.models.ExternalCraftsman, self.models.Commission, self.models.AuditLog):
            table = model.__table__
            sa.Table(table.name, metadata, *[
                sa.Column(column.name, column.type, primary_key=column.primary_key, nullable=column.nullable,
                          server_default=column.server_default.arg if column.server_default is not None else None)
                for column in table.columns
            ])
        metadata.tables["commission"].append_constraint(sa.UniqueConstraint("reissued_from_commission_id"))
        with app.app_context():
            metadata.create_all(self.db.engine)
            reviewers = [self.models.User(email=f"reviewer-{index}@example.invalid", display_name="Administration", is_admin=True) for index in range(2)]
            source = self.models.Commission(local_status="completion_review", rejected_at=self.models.utc_now())
            self.db.session.add_all([source, *reviewers])
            self.db.session.commit()
            source_id, reviewer_ids = source.id, [user.id for user in reviewers]
        barrier = Barrier(2)

        def reissue(reviewer_id):
            with app.app_context():
                reviewer = self.db.session.get(self.models.User, reviewer_id)
                barrier.wait(timeout=10)
                try:
                    original = self.followup.review_commission(source_id, reviewer, lock=True)
                except ValueError:
                    self.db.session.rollback()
                    return False
                replacement = self.models.Commission(created_by=reviewer)
                self.db.session.add(replacement)
                self.db.session.flush()
                self.followup.resolve_commission_review(original, reviewer, replacement=replacement)
                self.db.session.commit()
                return True

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                self.assertEqual(sorted(executor.map(reissue, reviewer_ids)), [False, True])
            with app.app_context():
                self.assertEqual(self.models.Commission.query.count(), 2)
                self.assertEqual(self.models.AuditLog.query.filter_by(action="commission.review.reissued").count(), 1)
        finally:
            with app.app_context():
                self.db.session.remove()
                metadata.drop_all(self.db.engine)
                self.db.engine.dispose()


if __name__ == "__main__":
    unittest.main()
