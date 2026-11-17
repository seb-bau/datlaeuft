import ast
from functools import wraps
import types
import unittest
from unittest.mock import patch

from flask import abort, g, redirect, request, url_for
from flask_login import current_user

import test_ticket_attachment_selection as fixtures


class UserEntraSynchronizationTests(unittest.TestCase):
    login = fixtures.TicketAttachmentSelectionTests.login
    start_patch = fixtures.TicketAttachmentSelectionTests.start_patch
    image = fixtures.TicketAttachmentSelectionTests.image

    @classmethod
    def setUpClass(cls):
        fixtures.TicketAttachmentSelectionTests.setUpClass.__func__(cls)
        cls.tables.append(cls.models.user_craft_activity)
        cls.app.before_request(lambda: g.pop("_login_user", None) and None)
        source = ast.parse((cls.root / "app/auth.py").read_text())
        guard = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == "admin_required")
        namespace = dict(F=object, Any=object, wraps=wraps, current_user=current_user, abort=abort, redirect=redirect, request=request, url_for=url_for)
        exec(compile(ast.Module(body=[guard], type_ignores=[]), "auth.py", "exec"), namespace)
        for endpoint in ("users", "import_users_from_entra"):
            view = cls.app.view_functions["admin." + endpoint]
            cls.app.view_functions["admin." + endpoint] = namespace["admin_required"](view.__wrapped__)

    def setUp(self):
        fixtures.TicketAttachmentSelectionTests.setUp(self)
        self.creator.is_admin = True
        self.db.session.commit()
        self.login(self.creator)
        self.import_users = self.start_patch(patch.object(self.admin, "import_entra_users", return_value=types.SimpleNamespace(created=1, updated=2, skipped=3)))

    def set_enabled(self, enabled):
        self.settings.settings.set(self.settings.SettingKeys.MICROSOFT_LOGIN_ENABLED, enabled)
        self.db.session.commit()

    def test_user_list_only_shows_the_post_button_when_entra_is_enabled(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                self.set_enabled(enabled)
                response = self.client.get("/admin/users")
                self.assertEqual(response.status_code, 200)
                html = response.get_data(as_text=True)
                self.assertEqual("Mit Entra synchronisieren" in html, enabled)
                if enabled:
                    self.assertIn('method="post" action="/admin/users/import-entra"', html)
                    self.assertIn('name="csrf_token"', html)
        self.import_users.assert_not_called()

    def test_sync_runs_the_shared_import_and_records_the_result(self):
        self.set_enabled(True)
        response = self.client.post("/admin/users/import-entra")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/admin/users"))
        self.import_users.assert_called_once_with()
        event = self.models.AuditLog.query.filter_by(action="users.import_entra").one()
        self.assertEqual(event.metadata_json, {"created": 1, "updated": 2, "skipped": 3})
        with self.client.session_transaction() as session:
            self.assertIn(("success", "Synchronisierung abgeschlossen: 1 neu, 2 aktualisiert, 3 übersprungen."), session["_flashes"])

    def test_sync_requires_an_administrator_enabled_entra_and_post(self):
        self.set_enabled(False)
        self.assertEqual(self.client.post("/admin/users/import-entra").status_code, 403)
        self.set_enabled(True)
        self.assertEqual(self.client.get("/admin/users/import-entra").status_code, 405)
        self.creator.is_admin = False
        self.db.session.commit()
        self.login(self.creator)
        self.assertEqual(self.client.get("/admin/users").status_code, 403)
        self.assertEqual(self.client.post("/admin/users/import-entra").status_code, 403)
        self.assertEqual(self.app.test_client().post("/admin/users/import-entra").status_code, 302)
        self.import_users.assert_not_called()

    def test_sync_errors_roll_back_changes_and_return_to_the_list_with_feedback(self):
        self.set_enabled(True)
        original_name = self.creator.display_name

        def fail_import():
            self.creator.display_name = "Unvollständiger Import"
            self.db.session.flush()
            raise RuntimeError("Graph request failed")

        self.import_users.side_effect = fail_import
        with self.assertLogs(self.app.logger, level="ERROR") as captured:
            response = self.client.post("/admin/users/import-entra")
        self.assertEqual(response.status_code, 302)
        self.assertIn("users_import_entra_failed", "\n".join(captured.output))
        self.assertEqual(self.creator.display_name, original_name)
        self.assertEqual(self.models.AuditLog.query.filter_by(action="users.import_entra").count(), 0)
        with self.client.session_transaction() as session:
            self.assertTrue(any(category == "danger" and "fehlgeschlagen" in message for category, message in session["_flashes"]))
