from unittest import TestCase
from unittest.mock import DEFAULT, patch

from flask import Flask
from jinja2 import ChoiceLoader, DictLoader
from lxml import etree

from app import dms_admin
from app.extensions import csrf, db, login_manager
from app.models import AppSetting, BlacklistEntry, DmsDocument, User, utc_now
from app.settings import SettingKeys, settings


class DmsTransferFilterTests(TestCase):
    def setUp(self):
        app = Flask("app")
        app.config.update(TESTING=True, SECRET_KEY="dms-filter-test", SQLALCHEMY_DATABASE_URI="sqlite://")
        db.init_app(app)
        csrf.init_app(app)
        login_manager.init_app(app)
        app.register_blueprint(dms_admin.dms_admin_bp)
        app.jinja_loader = ChoiceLoader([
            DictLoader({"base.html": "{% block content %}{% endblock %}"}), app.jinja_loader,
        ])
        with app.app_context():
            db.metadata.create_all(db.engine, tables=[
                AppSetting.__table__, BlacklistEntry.__table__, DmsDocument.__table__, User.__table__,
            ])
            admin = User(email="admin@example.invalid", display_name="Administrator", is_admin=True)
            document = DmsDocument(
                id="transfer", document_type="invoice", entity_type="invoice", entity_id=1,
                filename="rechnung.pdf", storage_name="rechnung.pdf", sha256="0" * 64,
                context={}, profile_snapshot={"key_property_id": "key"}, status="uncertain",
                created_at=utc_now(), available_at=utc_now(), upload_started=True,
            )
            settings.set(SettingKeys.DMS_ENABLED, True)
            settings.set(SettingKeys.DMS_PROCESSING_PAUSED, True)
            db.session.add_all([admin, document])
            db.session.commit()
            admin_id = admin.id
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session["_user_id"] = str(admin_id)

    def test_actions_return_to_the_active_status_filter(self):
        listing = "/admin/dms/documents?status=uncertain"
        page = etree.HTML(self.client.get(listing).data)
        actions = {
            "/admin/dms/documents/transfer/retry": {},
            "/admin/dms/documents/transfer/remove": {"confirmed_absent": "on"},
            "/admin/dms/documents/transfer/link": {"confirmed_document": "on", "dms_document_id": "D001"},
            "/admin/dms/processing/pause": {"paused": "false", "return_to": "documents"},
        }
        with patch.multiple(
            dms_admin, audit=DEFAULT, retry_document=DEFAULT, remove_from_queue=DEFAULT,
            link_existing_document=DEFAULT,
        ):
            for path, fields in actions.items():
                with self.subTest(path=path):
                    action = path + "?status=uncertain"
                    forms = page.xpath("//form[@method='post' and @action=$action]", action=action)
                    self.assertEqual(len(forms), 1)
                    token = forms[0].xpath("string(.//input[@name='csrf_token']/@value)")
                    response = self.client.post(action, data={"csrf_token": token, **fields})
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(response.location, listing)

    def test_review_is_the_default_and_all_statuses_survives_an_action(self):
        listing = "/admin/dms/documents"
        default_page = etree.HTML(self.client.get(listing).data)
        self.assertEqual(default_page.xpath("string(//select[@name='status']/option[@selected]/@value)"), "blocked")
        self.assertFalse(default_page.xpath("//a[text()='rechnung.pdf']"))

        all_page = etree.HTML(self.client.get(listing + "?status=").data)
        self.assertTrue(all_page.xpath("//select[@name='status']/option[@selected and @value='']"))
        self.assertTrue(all_page.xpath("//a[text()='rechnung.pdf']"))
        action = listing + "/transfer/retry?status="
        token = all_page.xpath("string(//form[@action=$action]/input[@name='csrf_token']/@value)", action=action)
        self.assertTrue(token)
        with patch.object(dms_admin, "retry_document"):
            response = self.client.post(action, data={"csrf_token": token})
        self.assertEqual(response.location, listing + "?status=")

    def test_link_error_keeps_the_filter_and_invalid_status_is_ignored(self):
        listing = "/admin/dms/documents?status=uncertain"
        page = etree.HTML(self.client.get(listing).data)
        token = page.xpath("string(//input[@name='csrf_token']/@value)")
        path = "/admin/dms/documents/transfer/link"
        for query, target in (("?status=uncertain", listing), ("?status=invalid", "/admin/dms/documents")):
            with self.subTest(query=query):
                response = self.client.post(path + query, data={"csrf_token": token})
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.location, target)
                with self.client.session_transaction() as session:
                    self.assertEqual(session["_flashes"][-1][0], "danger")
