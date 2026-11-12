import ast
import importlib
import sys
import types
import unittest
from datetime import datetime, timezone
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
            cls.models.CommissionPosition,
            cls.models.TimeEntry, cls.models.InventoryMovement, cls.models.InventoryItem,
            cls.models.CraftActivity, cls.models.TimeTaskTemplate, cls.models.TicketCraftFallback,
            cls.models.WowiCommissionType, cls.models.WowiCompanyCode,
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

    def prepare_commission_defaults(self):
        crafts = {
            craft_id: self.models.CraftActivity(
                id=craft_id, erp_id=craft_id + 100, name=f"Gewerk {craft_id}",
                active=True, app_enabled=True,
            )
            for craft_id in range(1, 7)
        }
        crafts[6].app_enabled = False
        positions = [
            self.models.TimeTaskTemplate(
                id=position_id, code=f"P-{position_id}", title=f"Position {position_id}",
                craft_activity=crafts[craft_id], erp_service_catalogue_id=catalog_id,
                active=active, commission_type_erp_ids=[100],
            )
            for position_id, craft_id, catalog_id, active in (
                (11, 1, 101, True), (12, 1, 102, True), (21, 2, 201, True),
                (31, 3, 301, True), (41, 4, 401, False), (42, 4, 402, True),
                (51, 5, None, True), (52, 5, 502, True), (61, 6, 601, True),
            )
        ]
        self.db.session.add_all([
            *crafts.values(), *positions,
            self.models.WowiCommissionType(erp_id=100, code="Reparatur"),
            *[
                self.models.TicketCraftFallback(craft_activity_id=craft_id, task_template_id=position_id)
                for craft_id, position_id in ((1, 11), (2, 21), (3, 12), (4, 41), (5, 51), (6, 61))
            ],
        ])
        self.db.session.commit()

    def commission_form_response(self, path="/admin/commissions/new", **values):
        with (
            patch.object(self.admin, "commission_service_positions", side_effect=lambda: (
                self.models.TimeTaskTemplate.query.filter_by(active=True)
            )),
            patch.object(self.admin, "configured_commission_type_ids", return_value={100}),
            patch.object(self.admin, "available_external_craftsmen", return_value=[]),
            patch.object(self.admin, "ticket_license_agreement_local_id", return_value=None),
            patch.object(self.admin, "render_template", wraps=render_template) as render,
        ):
            response = self.client.open(path, **values)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response, render.call_args.kwargs

    def test_manual_commission_form_exposes_only_available_defaults_for_their_craft(self):
        self.prepare_commission_defaults()
        response, view = self.commission_form_response()
        self.assertEqual(view["default_service_catalog_ids"], {1: 101, 2: 201})
        html = response.get_data(as_text=True)
        self.assertIn('data-default-service-catalog-id="101"', html)
        self.assertIn('data-default-service-catalog-id="201"', html)
        self.assertNotIn('data-default-service-catalog-id="401"', html)
        self.assertNotIn('data-default-service-catalog-id="601"', html)

    def test_commission_search_forwards_all_supported_object_levels(self):
        for search_context in ("tenant", "agreement", "use_unit", "address", "economic_unit", "all"):
            with self.subTest(search_context=search_context), patch.object(
                self.admin, "search_commission_objects", return_value=[{"object_type": search_context}],
            ) as search:
                response = self.client.get(
                    "/admin/commission-objects/search", query_string={"q": "Objekt", "context": search_context},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json["items"], [{"object_type": search_context}])
                search.assert_called_once_with("Objekt", search_context=search_context, limit=30)

    def test_commission_search_rejects_unknown_levels(self):
        with patch.object(self.admin, "search_commission_objects") as search:
            response = self.client.get("/admin/commission-objects/search?q=Objekt&context=unknown")
        self.assertEqual(response.status_code, 400)
        search.assert_not_called()

    def test_ticket_commission_form_keeps_the_ai_position_without_manual_defaults(self):
        self.prepare_commission_defaults()
        self.first_open_ticket.ai_state = "ready"
        self.first_open_ticket.ai_craft_activity_id = 1
        self.first_open_ticket.ai_task_template_id = 12
        self.db.session.commit()
        response, view = self.commission_form_response(
            f"/admin/commissions/new?ticket_id={self.first_open_ticket.id}",
        )
        self.assertEqual(view["default_service_catalog_ids"], {})
        self.assertEqual(view["position_rows"][0]["service_catalog_id"], 102)
        self.assertNotIn('data-default-service-catalog-id="101"', response.get_data(as_text=True))

    def test_commission_form_retains_a_manual_position_after_validation_failure(self):
        self.prepare_commission_defaults()
        text = "Manuell erfasster Text\nWeitere Details in der zweiten Zeile"
        with patch.object(self.admin, "create_pending_commission", side_effect=ValueError("Bitte Angaben prüfen.")):
            response, view = self.commission_form_response(method="POST", data={
                "craft_activity_id": "1", "position_service_catalog_id": "102",
                "position_text": text, "position_units": "2",
                "commission_type_id": "100",
            })
        self.assertEqual(str(view["position_rows"][0]["service_catalog_id"]), "102")
        self.assertEqual(view["position_rows"][0]["commission_text"], text)
        self.assertIn(text, response.get_data(as_text=True))

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

    def test_incomplete_order_is_visible_to_its_creator_in_the_review_list_and_dashboard(self):
        incomplete = self.models.Commission(
            erp_id=9001, erp_idnum="A-9001", title="Nicht erledigter Auftrag",
            created_by=self.user, completion_successful=False, completion_reason="Ersatzteil fehlt",
            completion_submitted_at=self.models.utc_now(), local_status="completion_review", status_id=7,
        )
        self.db.session.add(incomplete)
        self.db.session.commit()
        view, _ = self.get("/admin/commission-reviews?status=completion_review")
        self.assertEqual([item.id for item in view["items"]], [incomplete.id])
        dashboard, _ = self.get("/admin")
        self.assertEqual(dashboard["completion_review_count"], 1)

    def test_filter_reset_returns_to_my_open_tickets(self):
        _, links = self.get("/admin/tickets?owner=all&state=all")
        reset = next(link for link in links if link["text"] == "Filter zurücksetzen")
        view, _ = self.get(reset["href"])
        self.assertEqual(view["selected_owner"], "mine")
        self.assertEqual(view["selected_state"], "open")
        self.assertEqual(view["pagination"].total, 2)

    def test_commission_list_shows_sortable_dates_instead_of_the_erp_id(self):
        commission = self.models.Commission(
            erp_id=987654321, erp_idnum="A-123", title="Auftrag mit Datum",
            created_by=self.user, status_code="ERP-Status nicht anzeigen",
            recording_date=datetime(2026, 10, 31, tzinfo=timezone.utc),
            status_changed_at=datetime(2026, 11, 5, 14, 30, tzinfo=timezone.utc),
        )
        self.db.session.add(commission)
        self.db.session.commit()
        response = self.client.get("/admin/commissions")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertNotIn("<th>ERP-ID</th>", html)
        self.assertNotIn("987654321", html)
        self.assertIn("A-123", html)
        self.assertIn("<th>Erstellungsdatum</th>", html)
        self.assertIn("<th>Letzter Statuswechsel</th>", html)
        self.assertIn('data-order="2026-10-31T00:00:00"', html)
        self.assertIn('data-order="2026-11-05T14:30:00"', html)
        self.assertIn("31.10.2026", html)
        self.assertIn("05.11.2026", html)
        self.assertNotIn("<th>Status</th>", html)
        self.assertNotIn("ERP-Status nicht anzeigen", html)
        self.assertIn("<th>Bearbeitungsstand</th>", html)

    def test_commission_creator_filter_defaults_to_orders_created_by_the_current_user(self):
        other = self.models.User(email="other@example.invalid", display_name="Andere Sachbearbeitung")
        own = self.models.Commission(title="Von mir angelegt", created_by=self.user, local_official=other)
        assigned = self.models.Commission(title="Mir zugewiesen", created_by=other, local_official=self.user)
        imported = self.models.Commission(title="Import ohne Ersteller")
        self.db.session.add_all([other, own, assigned, imported])
        self.db.session.commit()
        for path in ("/admin/commissions", "/admin/commissions?creator=invalid"):
            view, _ = self.get(path)
            self.assertEqual(view["creator_filter"], "mine")
            self.assertEqual([item.id for item in view["items"]], [own.id])
        view, links = self.get("/admin/commissions?creator=all")
        self.assertTrue({own.id, assigned.id, imported.id}.issubset({item.id for item in view["items"]}))
        reset = next(link for link in links if link["text"] == "Filter zurücksetzen")
        reset_view, _ = self.get(reset["href"])
        self.assertEqual(reset_view["creator_filter"], "mine")
        self.assertEqual([item.id for item in reset_view["items"]], [own.id])

    def test_ticket_filter_combines_multiple_employees_without_including_others(self):
        self.ticket(subject="Weiterer Mitarbeiter", assigned_user_erp_id=13)
        self.db.session.commit()
        view, _ = self.get("/admin/tickets?owner=erp:11&owner=erp:12")
        self.assertEqual(view["selected_owners"], ["erp:11", "erp:12"])
        self.assertEqual({item.subject for item in view["pagination"].items}, {
            "Eigenes offenes Ticket", "Eigene offene Rückfrage", "Fremdes Ticket",
        })
        single, _ = self.get("/admin/tickets?owner=erp:12")
        self.assertEqual([item.subject for item in single["pagination"].items], ["Fremdes Ticket"])

    def test_ticket_filter_can_combine_own_and_unassigned_tickets(self):
        view, _ = self.get("/admin/tickets?owner=mine&owner=unassigned")
        self.assertEqual({item.subject for item in view["pagination"].items}, {
            "Eigenes offenes Ticket", "Eigene offene Rückfrage", "Nicht zugewiesenes Ticket",
        })

    def test_ticket_filter_normalizes_duplicates_invalid_values_and_all(self):
        view, _ = self.get("/admin/tickets?owner=erp:12&owner=erp:12&owner=invalid")
        self.assertEqual(view["selected_owners"], ["erp:12"])
        self.assertEqual(view["pagination"].total, 1)
        view, _ = self.get("/admin/tickets?owner=invalid")
        self.assertEqual(view["selected_owners"], ["mine"])
        view, _ = self.get("/admin/tickets?owner=all&owner=erp:12")
        self.assertEqual(view["selected_owners"], ["all"])
        self.assertIn(self.first_open_ticket.id, {item.id for item in view["pagination"].items})

    def test_ticket_pagination_retains_multiple_employees_and_search(self):
        for index in range(51):
            self.ticket(subject=f"Gemeinsame Suche {index}", assigned_user_erp_id=11 if index % 2 else 12)
        self.db.session.commit()
        view, links = self.get("/admin/tickets?owner=erp:11&owner=erp:12&q=Gemeinsame&state=open")
        self.assertEqual(view["pagination"].total, 51)
        second_page = next(link for link in links if link["text"] == "2")
        parameters = parse_qs(urlsplit(second_page["href"]).query)
        self.assertEqual(parameters["owner"], ["erp:11", "erp:12"])
        self.assertEqual(parameters["q"], ["Gemeinsame"])
        self.assertEqual(parameters["state"], ["open"])
        next_view, _ = self.get(second_page["href"])
        self.assertEqual(len(next_view["pagination"].items), 1)
        self.assertEqual(next_view["selected_owners"], ["erp:11", "erp:12"])

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
