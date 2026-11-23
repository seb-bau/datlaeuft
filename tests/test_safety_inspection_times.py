from datetime import date, datetime, time, timedelta, timezone
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

from flask import Flask
from flask_jwt_extended import create_access_token

from app import create_app
from app.extensions import db
from app.models import (
    Commission,
    SafetyInspectionGroup,
    SafetyInspectionJob,
    SafetyInspectionResult,
    SafetyInspectionSchedule,
    SafetyInspectionTemplate,
    SafetyInspectionTemplateItem,
    TimeEntry,
    User,
)
from app.safety_inspection_document import SafetyInspectionDocumentError
from app.safety_inspection_service import create_safety_inspection_template


class SafetyInspectionTimeTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        flask_app = Flask("app", instance_path=directory.name, instance_relative_config=True)
        with patch("app.Flask", return_value=flask_app), patch("app.configure_logging"):
            self.app = create_app()
        self.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
            JWT_SECRET_KEY="inspection-time-test-secret-at-least-32-bytes",
        )

        with self.app.app_context():
            db.create_all()
            manager = User(email="manager@example.invalid", display_name="Administrator", is_admin=True)
            craftsman = User(email="worker@example.invalid", display_name="Max Mustermann", craftsman_erp_id=10)
            foreman = User(email="foreman@example.invalid", display_name="Vorarbeiter", is_foreman=True)
            db.session.add_all([manager, craftsman, foreman])
            template, version = create_safety_inspection_template("Spielplatzkontrolle", None, None, manager, 35)
            version.status = "published"
            item = SafetyInspectionTemplateItem(template_version=version, title="Standsicherheit", position_index=0)
            today = date.today()
            group = SafetyInspectionGroup(
                name="Spielplätze Nord",
                template=template,
                schedule=SafetyInspectionSchedule(name="Monatlich", created_by=manager),
                object_level="economic_unit",
                created_by=manager,
                first_period_start=today,
            )
            job = SafetyInspectionJob(
                group=group,
                template_version=version,
                assigned_user=craftsman,
                period_key="test-period",
                period_start=today,
                period_end=today,
                due_from=today,
                due_to=today,
                warning_on=today,
                missed_on=today,
                object_level="economic_unit",
                object_erp_id=7,
                economic_unit_erp_id=7,
                object_address="Beispielstraße 1",
            )
            job.results.append(SafetyInspectionResult(
                template_item=item,
                item_key=str(uuid4()),
                item_title=item.title,
                position_index=0,
                state="no_defect",
            ))
            db.session.add(job)
            db.session.flush()
            self.job_id = job.id
            self.template_id = template.id
            self.manager_id = manager.id
            self.craftsman_id = craftsman.id
            self.foreman_id = foreman.id
            db.session.commit()

        self.template_url = f"/admin/safety-inspections/templates/{self.template_id}"
        self.job_url = f"/admin/safety-inspections/jobs/{self.job_id}"
        self.api_url = f"/api/safety-inspections/{self.job_id}"
        self.client = self.app.test_client()

    def web_client(self, user_id):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(user_id)
            session["_fresh"] = True
        return client

    def auth(self, user_id):
        with self.app.app_context():
            return {"Authorization": "Bearer " + create_access_token(identity=str(user_id))}

    def test_time_allowance_is_editable_on_a_published_detail_plan(self):
        manager = self.web_client(self.manager_id)
        self.assertIn(b'name="time_allowance_minutes"', manager.get(self.template_url).data)
        response = manager.post(self.template_url + "/time-allowance", data={"time_allowance_minutes": "45"})
        self.assertEqual(response.status_code, 302)
        invalid = manager.post(
            self.template_url + "/time-allowance", data={"time_allowance_minutes": "-5"}, follow_redirects=True,
        )
        self.assertIn("in ganzen Minuten".encode(), invalid.data)
        with self.app.app_context():
            template = db.session.get(SafetyInspectionTemplate, self.template_id)
            self.assertEqual(template.time_allowance_minutes, 45)
            self.assertEqual([version.status for version in template.versions], ["published"])

        manager.post(self.template_url + "/time-allowance", data={"time_allowance_minutes": "0"})
        with self.app.app_context():
            self.assertIsNone(db.session.get(SafetyInspectionTemplate, self.template_id).time_allowance_minutes)

    def test_offline_completion_credits_the_local_day_once_and_links_the_original_protocol(self):
        service_date = date.today() - timedelta(days=1)
        occurred_at = datetime.combine(service_date, time(0, 30), ZoneInfo("Europe/Berlin"))
        operation = {
            "operation_id": str(uuid4()),
            "job_id": self.job_id,
            "kind": "complete",
            "occurred_at": occurred_at.astimezone(timezone.utc).isoformat(),
        }
        self.assertNotEqual(occurred_at.astimezone(timezone.utc).date(), service_date)
        with self.app.app_context():
            commission = Commission(code="A-42", title="Auftrag", user_id=self.craftsman_id)
            db.session.add_all([
                TimeEntry(user_id=self.craftsman_id, service_date=service_date, minutes=5),
                TimeEntry(user_id=self.craftsman_id, service_date=service_date, minutes=10, commission=commission),
            ])
            db.session.commit()

        headers = self.auth(self.craftsman_id)
        response = self.client.post(self.api_url + "/offline-operations", json=operation, headers=headers)
        self.assertEqual(response.status_code, 200, response.json)
        replay = self.client.post(self.api_url + "/offline-operations", json=operation, headers=headers)
        self.assertEqual(replay.json, response.json)
        with self.app.app_context():
            entry = TimeEntry.query.filter_by(safety_inspection_job_id=self.job_id).one()
            self.assertEqual((entry.user_id, entry.service_date), (self.craftsman_id, service_date))
            self.assertEqual((entry.minutes, entry.work_minutes), (35, 35))
            self.assertEqual(entry.safety_inspection_protocol.version_number, 1)
            protocol_id = entry.safety_inspection_protocol_id

        summary = self.client.get("/api/my-time", query_string={"date": service_date.isoformat()}, headers=headers).json
        self.assertEqual(summary["recorded_minutes"], 50)
        inspection, order, free_time = summary["items"]
        self.assertEqual(inspection["safety_inspection"], {
            "id": self.job_id, "title": "Spielplätze Nord", "object_address": "Beispielstraße 1", "protocol_id": protocol_id,
        })
        self.assertFalse(inspection["is_free_time"])
        self.assertFalse(inspection["can_edit"])
        self.assertEqual(order["commission"]["number"], "A-42")
        self.assertTrue(free_time["is_free_time"])

        manager = self.web_client(self.manager_id)
        manager.post(self.job_url + "/protocols", data={"correction_reason": "Schreibfehler"})
        with self.app.app_context():
            job = db.session.get(SafetyInspectionJob, self.job_id)
            self.assertEqual(len(job.protocols), 2)
            correction_id = job.protocols[-1].id
        protocol_url = f"{self.job_url}/protocols/{protocol_id}"

        foreman = self.web_client(self.foreman_id)
        for path, duration in (
            ("/admin/performance-reviews?q=Spielplätze", b"35 Min."),
            ("/admin/work-time-reports?anchor=" + service_date.isoformat(), b"0:35 Std."),
            (self.job_url, b"35 Min."),
        ):
            page = foreman.get(path)
            self.assertEqual(page.status_code, 200)
            self.assertIn(duration, page.data)
            self.assertIn(protocol_url.encode(), page.data)
        with foreman.get(protocol_url) as protocol:
            self.assertEqual(protocol.status_code, 200)
            self.assertTrue(protocol.data.startswith(b"%PDF"))
        self.assertEqual(foreman.get(f"{self.job_url}/protocols/{correction_id}").status_code, 403)
        self.assertEqual(foreman.post(self.job_url + "/protocols").status_code, 403)
        self.assertEqual(self.web_client(self.craftsman_id).get(self.job_url).status_code, 403)

        inspection_list = manager.get("/admin/safety-inspections/jobs?view=completed")
        self.assertIn(protocol_url.encode(), inspection_list.data)
        review = self.client.get("/api/review/time-entries?status=all", headers=self.auth(self.foreman_id))
        self.assertEqual(review.status_code, 200)
        self.assertEqual(review.json["items"][0]["safety_inspection"]["protocol_id"], protocol_id)

    def test_online_completion_credits_the_actual_performer(self):
        headers = self.auth(self.manager_id)
        response = self.client.post(self.api_url + "/complete", json={}, headers=headers)
        self.assertEqual(response.status_code, 200, response.json)
        replay = self.client.post(self.api_url + "/complete", json={}, headers=headers)
        self.assertEqual(replay.status_code, 409)
        with self.app.app_context():
            entry = TimeEntry.query.one()
            self.assertEqual(entry.user_id, self.manager_id)
            self.assertEqual(entry.minutes, 35)
            self.assertIsNotNone(entry.safety_inspection_protocol_id)

    def test_job_without_allowance_creates_only_the_protocol(self):
        with self.app.app_context():
            db.session.get(SafetyInspectionTemplate, self.template_id).time_allowance_minutes = None
            db.session.commit()
        response = self.client.post(self.api_url + "/complete", json={}, headers=self.auth(self.craftsman_id))
        self.assertEqual(response.status_code, 200, response.json)
        with self.app.app_context():
            self.assertEqual(TimeEntry.query.count(), 0)
            self.assertEqual(len(db.session.get(SafetyInspectionJob, self.job_id).protocols), 1)
        self.assertEqual(self.web_client(self.foreman_id).get(self.job_url).status_code, 403)

    def test_failed_protocol_rolls_back_completion_and_time_credit(self):
        with patch("app.safety_inspection_document.create_safety_inspection_protocol", side_effect=SafetyInspectionDocumentError("Test")):
            response = self.client.post(self.api_url + "/complete", json={}, headers=self.auth(self.craftsman_id))
        self.assertEqual(response.status_code, 500)
        with self.app.app_context():
            self.assertEqual(TimeEntry.query.count(), 0)
            self.assertIsNone(db.session.get(SafetyInspectionJob, self.job_id).completed_at)
