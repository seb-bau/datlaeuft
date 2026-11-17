import importlib
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit

import test_ticket_views as fixtures


class CommissionFilterTests(unittest.TestCase):
    ticket = fixtures.TicketViewTests.ticket
    get = fixtures.TicketViewTests.get

    @classmethod
    def setUpClass(cls):
        fixtures.TicketViewTests.setUpClass.__func__(cls)
        cls.utils = importlib.import_module(cls.package_name + ".utils")
        for name in ("ExternalCraftsman", "CommissionPhoto", "CommissionAttachment", "CommissionAppointmentRequest", "AuditLog"):
            table = getattr(cls.models, name).__table__
            if table not in cls.tables:
                cls.tables.append(table)
        for endpoint in ("process_info_web.local_official", "process_info_web.office_chat_read", "process_info_web.office_chat_status", "dms_admin.work_report_regenerate"):
            cls.app.add_url_rule("/test/" + endpoint + "/<int:commission_id>", endpoint=endpoint, view_func=lambda commission_id: "")

    def setUp(self):
        fixtures.TicketViewTests.setUp(self)
        self.worker = self.models.User(email="worker@example.invalid", display_name="Erster Handwerker", craftsman_erp_id=501)
        self.other_worker = self.models.User(email="second-worker@example.invalid", display_name="Zweiter Handwerker", craftsman_erp_id=502)
        self.external = self.models.ExternalCraftsman(craftsman_erp_id=701, display_name="Externe Firma", wowi_active=True)
        self.db.session.add_all([self.worker, self.other_worker, self.external])
        self.db.session.flush()
        self.internal = self.models.Commission(title="Interner Auftrag", created_by=self.user, user=self.worker, craftsman_erp_id=501,
                                               assignment_type="internal", local_status="waiting_for_acceptance")
        self.other = self.models.Commission(title="Zweiter Auftrag", created_by=self.user, user=self.other_worker, craftsman_erp_id=502,
                                            assignment_type="internal", local_status="open")
        self.external_order = self.models.Commission(title="Externer Auftrag", created_by=self.user, craftsman_erp_id=701,
                                                     assignment_type="external", local_status="waiting_for_acceptance", is_high_priority=True)
        self.db.session.add_all([self.internal, self.other, self.external_order])
        self.db.session.commit()

    def test_craftsman_defaults_to_all_and_includes_internal_and_external_orders(self):
        view, _ = self.get("/admin/commissions")
        self.assertEqual(view["craftsman_filter"], "all")
        self.assertEqual({item.id for item in view["items"]}, {self.internal.id, self.other.id, self.external_order.id})
        self.assertEqual(view["craftsman_options"][f"user:{self.worker.id}"], "Erster Handwerker (Intern)")
        self.assertEqual(view["craftsman_options"]["erp:701"], "Externe Firma (Extern)")
        for selected, expected in ((f"user:{self.worker.id}", self.internal), ("erp:701", self.external_order)):
            view, _ = self.get("/admin/commissions?craftsman=" + selected)
            self.assertEqual([item.id for item in view["items"]], [expected.id])
            self.assertIn(f"user:{self.other_worker.id}", view["craftsman_options"])

    def test_opening_an_order_and_returning_restores_every_filter(self):
        path = "/admin/commissions?creator=all&view=active&assignment_type=external&official=unassigned&local_status=waiting_for_acceptance&priority=high&craftsman=erp%3A701"
        original, links = self.get(path)
        self.assertEqual([item.id for item in original["items"]], [self.external_order.id])
        detail = next(link["href"] for link in links if urlsplit(link["href"]).path == f"/admin/commissions/{self.external_order.id}")
        self.assertEqual(parse_qs(urlsplit(detail).query)["return_to"], [path])
        context, links = self.get(detail)
        back = next(link["href"] for link in links if link["text"] == "Zurück")
        self.assertEqual(back, path)
        self.assertEqual(context["list_return_url"], path)
        returned, _ = self.get(back)
        for name in ("creator_filter", "view_mode", "assignment_type", "official_filter", "local_status_filter", "priority_filter", "craftsman_filter"):
            self.assertEqual(returned[name], original[name])
        self.assertEqual([item.id for item in returned["items"]], [self.external_order.id])

    def test_return_context_survives_changes_and_sync_without_an_erp_id(self):
        path = "/admin/commissions?creator=all&craftsman=user%3A" + str(self.worker.id)
        for suffix, values in (("priority", {"priority": "high"}), ("attachments", {})):
            response = self.client.post(f"/admin/commissions/{self.internal.id}/{suffix}", data={**values, "return_to": path})
            self.assertEqual(response.status_code, 302)
            self.assertEqual(parse_qs(urlsplit(response.location).query)["return_to"], [path])
        detail = f"/admin/commissions/{self.internal.id}?" + urlencode({"return_to": path})
        response = self.client.post(f"/admin/commissions/{self.internal.id}/sync", data={"next": detail})
        self.assertEqual(response.location, detail)

    def test_direct_access_and_invalid_return_urls_use_the_order_list(self):
        for value in (None, "https://example.invalid/admin/commissions?view=all", "//example.invalid/admin/commissions", "/admin/users", "/\\example.invalid", "/admin/commissions\n?view=all"):
            with self.subTest(value=value), self.app.test_request_context(query_string={"return_to": value} if value is not None else {}):
                self.assertEqual(self.utils.commission_list_return_url(), "/admin/commissions")
                self.assertEqual(self.utils.commission_detail_url(self.internal.id), f"/admin/commissions/{self.internal.id}")

    def test_an_empty_selected_craftsman_remains_visible_and_reset_uses_all(self):
        view, links = self.get("/admin/commissions?craftsman=erp:999999")
        self.assertEqual(view["items"], [])
        self.assertEqual(view["craftsman_filter"], "erp:999999")
        reset = next(link["href"] for link in links if link["text"] == "Filter zurücksetzen")
        view, _ = self.get(reset)
        self.assertEqual(view["craftsman_filter"], "all")
