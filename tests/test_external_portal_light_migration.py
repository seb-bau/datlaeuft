import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
import sqlalchemy as sa
from sqlalchemy.engine import make_url


class ExternalPortalLightMigrationTests(unittest.TestCase):
    def test_upgrade_and_downgrade_preserve_existing_assignments_and_disabled_defaults(self):
        root = Path(__file__).resolve().parents[1]
        database_url = make_url(os.environ.get("DATLAEUFT_TEST_DATABASE_URL", "sqlite://"))
        if database_url.get_backend_name() != "sqlite" and not (database_url.database or "").startswith("datlaeuft_test_"):
            self.fail("Migrationstests benötigen eine separate datlaeuft_test-Datenbank.")
        engine = sa.create_engine(database_url)
        self.addCleanup(engine.dispose)
        spec = importlib.util.spec_from_file_location("_external_portal_light_migration",
                                                      root / "migrations/versions/96a4c8e2f0b6_external_portal_light.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        metadata = sa.MetaData()
        craftsman = sa.Table("external_craftsman", metadata, sa.Column("id", sa.Integer(), primary_key=True),
                             sa.Column("display_name", sa.String(255)), sa.Column("portal_enabled", sa.Boolean()))
        assignment = sa.Table("external_commission_assignment_mail", metadata,
                              sa.Column("id", sa.Integer(), primary_key=True), sa.Column("commission_id", sa.Integer()),
                              sa.Column("external_craftsman_id", sa.Integer()), sa.Column("assignment_version", sa.Integer()),
                              sa.Column("state", sa.String(30)),
                              sa.UniqueConstraint("commission_id", "assignment_version", name="uq_external_assignment_mail_version"))
        try:
            with engine.begin() as connection:
                metadata.create_all(connection)
                connection.execute(craftsman.insert(), [{"id": 1, "display_name": "Firma A", "portal_enabled": False},
                                                        {"id": 2, "display_name": "Firma B", "portal_enabled": True}])
                connection.execute(assignment.insert().values(id=1, commission_id=100, external_craftsman_id=1,
                                                              assignment_version=3, state="sent"))
                with patch.object(migration, "op", Operations(MigrationContext.configure(connection))):
                    migration.upgrade()
                    rows = connection.execute(sa.text("SELECT portal_enabled, portal_light_enabled, reminder_first_days, reminder_second_days, cancel_unanswered_days FROM external_craftsman ORDER BY id")).all()
                    self.assertEqual([tuple(row) for row in rows], [(0, None, 0, 0, 0), (1, None, 0, 0, 0)])
                    row = connection.execute(sa.text("SELECT assignment_version, state, response_at, response_decision, response_text, auto_canceled_at FROM external_commission_assignment_mail WHERE id=1")).one()
                    self.assertEqual(tuple(row), (3, "sent", None, None, None, None))
                    constraints = sa.inspect(connection).get_unique_constraints("external_commission_assignment_mail")
                    self.assertTrue(any(item["column_names"] == ["commission_id", "assignment_version"] for item in constraints))
                    migration.downgrade()
                self.assertEqual(connection.execute(sa.select(sa.func.count()).select_from(craftsman)).scalar(), 2)
                self.assertEqual(connection.execute(sa.select(assignment.c.assignment_version)).scalar(), 3)
                self.assertNotIn("response_at", {item["name"] for item in sa.inspect(connection).get_columns(assignment.name)})
                self.assertNotIn("portal_light_enabled", {item["name"] for item in sa.inspect(connection).get_columns(craftsman.name)})
        finally:
            metadata.drop_all(engine)
        self.assertEqual(ScriptDirectory(str(root / "migrations")).get_current_head(), migration.revision)


if __name__ == "__main__":
    unittest.main()
