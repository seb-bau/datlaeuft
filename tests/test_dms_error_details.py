from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import quote, quote_plus

from flask import Flask
from requests import Response
from requests_cache import CachedSession

from app.dms_client import CloudDmsClient, DmsError, DmsTransport
from app.dms_service import process_claimed_document
from app.extensions import db
from app.models import AppSetting, DmsDocument, utc_now


HOSTNAME = "dms.example.invalid"
API_KEY = "test:key+prüfung"
REPOSITORY = "repository"


def error_response(status, body, content_type="application/hal+json"):
    response = Response()
    response.status_code = status
    response.headers["Content-Type"] = content_type
    response.encoding = "utf-8"
    response._content = body.encode("utf-8")
    return response


class DmsErrorDetailsTests(TestCase):
    def request_error(self, response):
        transport = DmsTransport(HOSTNAME, API_KEY, REPOSITORY)
        self.addCleanup(transport.session.close)
        with patch.object(transport.session, "request", return_value=response), self.assertRaises(DmsError) as caught:
            transport.post("o2m", data={})
        return caught.exception

    def test_rejection_keeps_the_status_and_limits_the_diagnostic_text(self):
        reason = "Pflichtfeld fehlt. " + "x" * 1100
        response = error_response(400, '{"reason":"' + reason + '","requestId":"abc-1"}')
        error = self.request_error(response)
        self.assertIn("Status 400", str(error))
        self.assertIn("Anfrage-ID abc-1", str(error))
        self.assertIn("Pflichtfeld fehlt", str(error))
        self.assertEqual(len(str(error)), 1000)
        self.assertTrue(str(error).endswith("…"))
        self.assertFalse(error.retryable)
        self.assertFalse(error.uncertain)

    def test_plain_text_is_available_and_the_api_key_is_removed(self):
        response = error_response(
            400, f"Ungültiger Wert\n{API_KEY}\n{quote(API_KEY, safe='')} {quote_plus(API_KEY, safe='')}", "text/plain; charset=utf-8",
        )
        message = str(self.request_error(response))
        self.assertIn("Ungültiger Wert", message)
        self.assertIn("[API-Key ausgeblendet]", message)
        for secret in {API_KEY, quote(API_KEY, safe=""), quote_plus(API_KEY, safe="")}:
            self.assertNotIn(secret, message)

    def test_invalid_json_empty_or_html_errors_keep_the_status(self):
        for body, content_type in (("{", "application/json"), ("", "text/plain"), ("<html>Gateway</html>", "text/html")):
            with self.subTest(content_type=content_type, body=body):
                error = self.request_error(error_response(500, body, content_type))
                self.assertEqual(str(error), "d.velop hat die Anfrage mit Status 500 abgelehnt.")
                self.assertTrue(error.retryable)
                self.assertTrue(error.uncertain)

    def test_archive_worker_persists_the_dvelop_error_details(self):
        with TemporaryDirectory() as directory:
            app = Flask("app", instance_path=directory)
            app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
            db.init_app(app)
            with app.app_context():
                db.metadata.create_all(db.engine, tables=[AppSetting.__table__, DmsDocument.__table__])
                path = Path(directory) / "dms_documents" / "transfer" / "rechnung.pdf"
                path.parent.mkdir(parents=True)
                pdf = b"%PDF-1.4\n"
                path.write_bytes(pdf)
                document = DmsDocument(
                    id="transfer", document_type="invoice", entity_type="invoice", entity_id=1,
                    filename=path.name, storage_name=str(path.relative_to(directory)), sha256=sha256(pdf).hexdigest(),
                    context={}, profile_snapshot={"category_id": "invoices", "schema": {"fields": []}, "mappings": {}},
                    hostname=HOSTNAME, repository_id=REPOSITORY, status="processing", lock_token="lock",
                    created_at=utc_now(), available_at=utc_now(),
                )
                db.session.add(document)
                db.session.commit()

                uploaded = error_response(201, "")
                uploaded.headers["Location"] = "/dms/r/repository/blob/chunk/uploaded"
                rejected = error_response(400, """{
                    "reason": "Pflichtfeld fehlt.", "hint": "Eigenschaftszuordnung prüfen.",
                    "details": ["Feld Rechnungsnummer ist leer."], "errorCode": 10019, "requestId": "dms-123"
                }""")
                with (
                    patch("app.dms_service.connection", return_value=(HOSTNAME, API_KEY, REPOSITORY)),
                    patch.object(CloudDmsClient, "get_mappings"),
                    patch.object(CachedSession, "request", side_effect=[uploaded, rejected]),
                ):
                    process_claimed_document(document.id, "lock")

                db.session.refresh(document)
                self.assertEqual(document.status, "blocked")
                self.assertFalse(document.upload_started)
                for detail in (
                    "Status 400", "Pflichtfeld fehlt.", "Feld Rechnungsnummer ist leer.",
                    "Eigenschaftszuordnung prüfen.", "Fehlercode 10019", "Anfrage-ID dms-123",
                ):
                    self.assertIn(detail, document.error)
