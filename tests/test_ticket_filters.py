import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask
from flask_sqlalchemy import SQLAlchemy


class OpenTicketFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = SQLAlchemy()
        cls.app = Flask(__name__)
        cls.app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
        cls.db.init_app(cls.app)
        cls.package_name = "_ticket_filter_test_app"
        package = types.ModuleType(cls.package_name)
        package.__path__ = [str(Path(__file__).resolve().parents[1] / "app")]
        extensions = types.ModuleType(cls.package_name + ".extensions")
        extensions.db = cls.db
        module_patch = patch.dict(sys.modules, {
            cls.package_name: package,
            extensions.__name__: extensions,
        })
        module_patch.start()
        cls.addClassCleanup(module_patch.stop)
        cls.models = importlib.import_module(cls.package_name + ".models")
        cls.filters = importlib.import_module(cls.package_name + ".ticket_filters")
        cls.tables = [model.__table__ for model in (
            cls.models.Commission, cls.models.TicketMirror,
            cls.models.CommissionTicketLink,
        )]

    def setUp(self):
        context = self.app.app_context()
        context.push()
        self.addCleanup(context.pop)
        self.addCleanup(self.db.session.remove)
        self.db.metadata.drop_all(self.db.engine, tables=self.tables)
        self.db.metadata.create_all(self.db.engine, tables=self.tables)
        self.next_erp_id = 100

    def ticket(self, **values):
        self.next_erp_id += 1
        defaults = {
            "erp_id": self.next_erp_id,
            "assigned_user_erp_id": 11,
            "status_code": "In Bearbeitung",
        }
        defaults.update(values)
        ticket = self.models.TicketMirror(**defaults)
        self.db.session.add(ticket)
        self.db.session.flush()
        return ticket

    def query(self, owner_ids=(11,)):
        return self.filters.open_tickets_query(self.models.TicketMirror.query).filter(
            self.models.TicketMirror.assigned_user_erp_id.in_(owner_ids)
        )

    def test_includes_open_statuses_and_tickets_without_a_status(self):
        expected = [self.ticket(status_code=code).id for code in (
            "Neu", "In Bearbeitung", "Warte auf Rückmeldung", None, "",
        )]
        self.assertCountEqual([ticket.id for ticket in self.query()], expected)

    def test_excludes_completed_and_commissioned_statuses(self):
        for code in ("Erledigt", "Beauftragt", "ERLEDIGT", "beauftragt", " Erledigt "):
            self.ticket(status_code=code)
        self.assertEqual(self.query().count(), 0)

    def test_uses_status_codes_instead_of_assuming_fixed_status_ids(self):
        self.ticket(status_erp_id=91, status_code="Erledigt")
        self.ticket(status_erp_id=92, status_code="Beauftragt")
        open_ticket = self.ticket(status_erp_id=91, status_code="In Bearbeitung")
        self.assertEqual([ticket.id for ticket in self.query()], [open_ticket.id])

    def test_preserves_the_current_users_owner_filter(self):
        mine = self.ticket()
        self.ticket(assigned_user_erp_id=12)
        self.ticket(assigned_user_erp_id=None)
        self.assertEqual([ticket.id for ticket in self.query()], [mine.id])

    def test_includes_all_mapped_owner_ids_without_including_other_users(self):
        first = self.ticket(assigned_user_erp_id=11)
        second = self.ticket(assigned_user_erp_id=12)
        self.ticket(assigned_user_erp_id=13)
        self.assertCountEqual(
            [ticket.id for ticket in self.query(owner_ids=(11, 12))],
            [first.id, second.id],
        )

    def test_missing_owner_mapping_returns_no_tickets(self):
        self.ticket()
        self.ticket(assigned_user_erp_id=None)
        self.assertEqual(self.query(owner_ids=()).count(), 0)

    def test_excludes_the_legacy_single_commission_link(self):
        commission = self.models.Commission()
        self.db.session.add(commission)
        self.db.session.flush()
        self.ticket(commission_id=commission.id)
        self.assertEqual(self.query().count(), 0)

    def test_excludes_a_commission_link_without_a_legacy_commission_id(self):
        ticket = self.ticket()
        commission = self.models.Commission()
        self.db.session.add(commission)
        self.db.session.flush()
        self.db.session.add(self.models.CommissionTicketLink(
            ticket_erp_id=ticket.erp_id, commission_id=commission.id,
        ))
        self.db.session.flush()
        self.assertIsNone(ticket.commission_id)
        self.assertEqual(self.query().count(), 0)

    def test_multiple_commission_links_do_not_change_the_open_ticket_count(self):
        linked_ticket = self.ticket()
        open_ticket = self.ticket()
        for _ in range(2):
            commission = self.models.Commission()
            self.db.session.add(commission)
            self.db.session.flush()
            self.db.session.add(self.models.CommissionTicketLink(
                ticket_erp_id=linked_ticket.erp_id, commission_id=commission.id,
            ))
        self.db.session.flush()
        self.assertEqual(self.query().count(), 1)
        self.assertEqual([ticket.id for ticket in self.query()], [open_ticket.id])

    def test_excludes_deleted_tickets(self):
        self.ticket(deleted_at=self.models.utc_now())
        self.assertEqual(self.query().count(), 0)

    def test_local_processing_state_excludes_ignored_tickets(self):
        self.ticket(state="ignored")
        expected = [self.ticket(state=state).id for state in (
            "new", "converted",
        )]
        self.assertCountEqual([ticket.id for ticket in self.query()], expected)

    def test_count_matches_the_paginated_ticket_list(self):
        expected = [self.ticket().id for _ in range(5)]
        self.ticket(status_code="Erledigt")
        self.ticket(assigned_user_erp_id=12)
        query = self.query().order_by(self.models.TicketMirror.id)
        pagination = query.paginate(page=1, per_page=2)
        self.assertEqual(query.count(), len(expected))
        self.assertEqual(pagination.total, len(expected))
        self.assertEqual([ticket.id for ticket in pagination.items], expected[:2])


if __name__ == "__main__":
    unittest.main()
