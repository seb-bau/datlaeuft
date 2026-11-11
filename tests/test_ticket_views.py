import ast
import importlib
import sys
import types
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from flask import Flask, render_template
from flask_login import LoginManager, login_required
from flask_sqlalchemy import SQLAlchemy


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.current_link = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.current_link = {**dict(attrs), "text": ""}

    def handle_data(self, data):
        if self.current_link is not None:
            self.current_link["text"] += data

    def handle_endtag(self, tag):
        if tag == "a" and self.current_link is not None:
            self.current_link["text"] = " ".join(self.current_link["text"].split())
            self.links.append(self.current_link)
            self.current_link = None


class TicketViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.db = SQLAlchemy()
        cls.app = Flask(__name__, template_folder=str(cls.root / "app" / "templates"))
        cls.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite://", SECRET_KEY="ticket-view-test", TESTING=True,
        )
        cls.db.init_app(cls.app)
        cls.package_name = "_ticket_view_test_app"
        package = types.ModuleType(cls.package_name)
        package.__path__ = [str(cls.root / "app")]
        extensions = types.ModuleType(cls.package_name + ".extensions")
        extensions.db = cls.db
        extensions.jwt = Mock()
        modules = {cls.package_name: package, extensions.__name__: extensions}
        admin_source = ast.parse((cls.root / "app" / "admin.py").read_text())
        for node in admin_source.body:
            if not isinstance(node, ast.ImportFrom) or node.level != 1:
                continue
            if node.module in {"models", "extensions", "blacklist", "ticket_filters"}:
                continue
            module_name = cls.package_name + "." + node.module
            module = modules.setdefault(module_name, types.ModuleType(module_name))
            for item in node.names:
                setattr(module, item.name, login_required if node.module == "auth" else Mock())
        modules[cls.package_name + ".settings"].settings.get.return_value = False
        module_patch = patch.dict(sys.modules, modules)
        module_patch.start()
        cls.addClassCleanup(module_patch.stop)
        cls.models = importlib.import_module(cls.package_name + ".models")
        cls.admin = importlib.import_module(cls.package_name + ".admin")
        login_manager = LoginManager(cls.app)
        login_manager.user_loader(lambda user_id: cls.db.session.get(cls.models.User, int(user_id)))
        cls.app.register_blueprint(cls.admin.admin_bp)
        cls.app.add_url_rule("/logout", endpoint="auth.logout", view_func=lambda: "")
        cls.app.jinja_env.globals["csrf_token"] = lambda: "test-token"
        cls.tables = [model.__table__ for model in (
            cls.models.User, cls.models.UserJurisdictionPersonMapping,
            cls.models.WowiResponsibleOfficial, cls.models.WowiEconomicUnit,
            cls.models.WowiEconomicUnitJurisdiction, cls.models.BlacklistEntry,
            cls.models.Commission, cls.models.TicketMirror, cls.models.CommissionTicketLink,
            cls.models.TimeEntry, cls.models.InventoryMovement, cls.models.InventoryItem,
        )]

    def setUp(self):
        context = self.app.app_context()
        context.push()
        self.addCleanup(context.pop)
        self.addCleanup(self.db.session.remove)
        self.db.metadata.drop_all(self.db.engine, tables=self.tables)
        self.db.metadata.create_all(self.db.engine, tables=self.tables)
        self.user = self.models.User(
            id=1, email="foreman@example.invalid", display_name="Testbenutzer",
            person_erp_id=101, is_foreman=True,
        )
        self.db.session.add_all([
            self.user,
            self.models.WowiResponsibleOfficial(
                erp_id=21, person_erp_id=101, user_erp_id=11, name="Testbenutzer",
            ),
            self.models.WowiResponsibleOfficial(
                erp_id=22, person_erp_id=102, user_erp_id=12, name="Weiterer Benutzer",
            ),
        ])
        self.next_erp_id = 100
        self.first_open_ticket = self.ticket(subject="Eigenes offenes Ticket")
        self.second_open_ticket = self.ticket(subject="Eigene offene Rückfrage")
        self.ignored_ticket = self.ticket(subject="Ticket ohne gewünschten Auftrag", state="ignored")
        self.ticket(subject="Erledigtes Ticket", status_code="Erledigt")
        self.ticket(subject="Beauftragtes Ticket", status_code="Beauftragt")
        self.ticket(subject="Fremdes Ticket", assigned_user_erp_id=12)
        self.ticket(subject="Nicht zugewiesenes Ticket", assigned_user_erp_id=None)
        self.ticket(subject="Gelöschtes Ticket", deleted_at=self.models.utc_now())
        legacy_commission = self.models.Commission()
        linked_commission = self.models.Commission()
        self.db.session.add_all([legacy_commission, linked_commission])
        self.db.session.flush()
        self.ticket(subject="Ticket mit Einzelverknüpfung", commission_id=legacy_commission.id)
        linked_ticket = self.ticket(subject="Ticket mit Mehrfachverknüpfung")
        self.db.session.add(self.models.CommissionTicketLink(
            ticket_erp_id=linked_ticket.erp_id, commission_id=linked_commission.id,
        ))
        self.db.session.commit()
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["_user_id"] = "1"
            session["_fresh"] = True

    def ticket(self, **values):
        self.next_erp_id += 1
        defaults = {
            "erp_id": self.next_erp_id, "assigned_user_erp_id": 11,
            "status_code": "In Bearbeitung",
        }
        defaults.update(values)
        ticket = self.models.TicketMirror(**defaults)
        self.db.session.add(ticket)
        self.db.session.flush()
        return ticket

    def get(self, path):
        with patch.object(self.admin, "render_template", wraps=render_template) as render:
            response = self.client.get(path)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        parser = LinkParser()
        parser.feed(response.get_data(as_text=True))
        return render.call_args.kwargs, parser.links

    def test_dashboard_tile_and_menu_show_the_same_open_tickets(self):
        dashboard, links = self.get("/admin")
        self.assertEqual(dashboard["my_open_ticket_count"], 2)
        tile = next(link for link in links if link["text"].startswith("Meine offenen Tickets"))
        menu = next(link for link in links if link["text"] == "Tickets")
        self.assertEqual(tile["text"], "Meine offenen Tickets 2")
        tile_view, _ = self.get(tile["href"])
        menu_view, _ = self.get(menu["href"])
        expected = {self.first_open_ticket.id, self.second_open_ticket.id}
        for view in (tile_view, menu_view):
            self.assertEqual(view["selected_owner"], "mine")
            self.assertEqual(view["selected_state"], "open")
            self.assertEqual({ticket.id for ticket in view["pagination"].items}, expected)
            self.assertEqual(view["pagination"].total, dashboard["my_open_ticket_count"])

    def test_filter_reset_returns_to_my_open_tickets(self):
        _, links = self.get("/admin/tickets?owner=all&state=all")
        reset = next(link for link in links if link["text"] == "Filter zurücksetzen")
        view, _ = self.get(reset["href"])
        self.assertEqual(view["selected_owner"], "mine")
        self.assertEqual(view["selected_state"], "open")
        self.assertEqual(view["pagination"].total, 2)

    def test_explicit_filters_still_allow_closed_and_assigned_tickets(self):
        view, _ = self.get("/admin/tickets?owner=all&state=all")
        subjects = {ticket.subject for ticket in view["pagination"].items}
        self.assertIn("Erledigtes Ticket", subjects)
        self.assertIn("Beauftragtes Ticket", subjects)
        self.assertIn("Ticket mit Einzelverknüpfung", subjects)
        self.assertIn("Ticket mit Mehrfachverknüpfung", subjects)
        self.assertIn("Fremdes Ticket", subjects)
        self.assertNotIn("Gelöschtes Ticket", subjects)

    def test_search_keeps_the_default_open_ticket_filters(self):
        view, _ = self.get("/admin/tickets?q=Erledigtes")
        self.assertEqual(view["pagination"].total, 0)
        view, _ = self.get("/admin/tickets?q=Rückfrage")
        self.assertEqual([ticket.id for ticket in view["pagination"].items], [self.second_open_ticket.id])

    def test_ignore_and_restore_update_the_ticket_list_and_dashboard_count(self):
        response = self.client.post(f"/admin/tickets/{self.first_open_ticket.id}/ignore")
        self.assertEqual(response.status_code, 302)
        dashboard, _ = self.get("/admin")
        view, _ = self.get("/admin/tickets")
        self.assertEqual(dashboard["my_open_ticket_count"], 1)
        self.assertEqual([ticket.id for ticket in view["pagination"].items], [self.second_open_ticket.id])

        response = self.client.post(f"/admin/tickets/{self.first_open_ticket.id}/restore")
        self.assertEqual(response.status_code, 302)
        dashboard, _ = self.get("/admin")
        view, _ = self.get("/admin/tickets")
        self.assertEqual(dashboard["my_open_ticket_count"], 2)
        self.assertEqual(
            {ticket.id for ticket in view["pagination"].items},
            {self.first_open_ticket.id, self.second_open_ticket.id},
        )

    def test_ignored_tickets_remain_available_through_the_explicit_filter(self):
        view, _ = self.get("/admin/tickets?state=ignored")
        self.assertEqual([ticket.id for ticket in view["pagination"].items], [self.ignored_ticket.id])

    def test_pagination_links_keep_the_open_ticket_filters(self):
        for index in range(51):
            self.ticket(subject=f"Weiteres Ticket {index}")
        self.db.session.commit()
        view, links = self.get("/admin/tickets")
        self.assertEqual(view["pagination"].total, 53)
        second_page = next(link for link in links if link["text"] == "2")
        parameters = parse_qs(urlsplit(second_page["href"]).query)
        self.assertEqual(parameters["owner"], ["mine"])
        self.assertEqual(parameters["state"], ["open"])
        view, _ = self.get(second_page["href"])
        self.assertEqual(view["pagination"].page, 2)
        self.assertEqual(view["pagination"].total, 53)
        self.assertEqual(len(view["pagination"].items), 3)

    def test_dashboard_hides_ticket_tile_for_users_without_ticket_access(self):
        self.user.is_foreman = False
        self.db.session.commit()
        _, links = self.get("/admin")
        self.assertFalse(any(link["text"].startswith("Meine offenen Tickets") for link in links))

    def test_missing_owner_mapping_keeps_the_dashboard_and_ticket_list_empty(self):
        self.user.person_erp_id = None
        self.db.session.commit()
        dashboard, _ = self.get("/admin")
        view, _ = self.get("/admin/tickets")
        self.assertEqual(dashboard["my_open_ticket_count"], 0)
        self.assertEqual(view["pagination"].total, 0)
        self.assertFalse(view["owner_mapping_available"])


if __name__ == "__main__":
    unittest.main()
