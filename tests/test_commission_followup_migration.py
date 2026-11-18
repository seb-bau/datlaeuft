import importlib.util
import os
from pathlib import Path
import unittest

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa
from sqlalchemy.engine import make_url


class CommissionFollowupMigrationTests(unittest.TestCase):
    def test_migration_restores_only_proven_rejections_and_preserves_real_deletions(self):
        url = make_url(os.environ.get("DATLAEUFT_TEST_DATABASE_URL", "sqlite://"))
        if url.get_backend_name() != "sqlite" and not (url.database or "").startswith("datlaeuft_test_"):
            self.fail("Migrationstests benötigen eine separate datlaeuft_test-Datenbank.")
        engine = sa.create_engine(url)
        self.addCleanup(engine.dispose)
        names = ("erp_outbox_operation", "audit_log", "commission", "user_account")
        if any(sa.inspect(engine).has_table(name) for name in names):
            self.skipTest("Die Migration benötigt eigene leere Testtabellen.")
        path = Path(__file__).resolve().parents[1] / "migrations/versions/f7c9e1a3b5d8_commission_review_resolution.py"
        spec = importlib.util.spec_from_file_location("commission_followup_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        try:
            with engine.begin() as connection:
                connection.execute(sa.text("CREATE TABLE user_account (id INTEGER PRIMARY KEY)"))
                connection.execute(sa.text("CREATE TABLE commission (id INTEGER PRIMARY KEY, local_status VARCHAR(80), deleted_at DATETIME, completion_submitted_at DATETIME, status_changed_at DATETIME)"))
                connection.execute(sa.text("CREATE TABLE erp_outbox_operation (commission_id INTEGER, operation_type VARCHAR(80))"))
                connection.execute(sa.text("CREATE TABLE audit_log (entity_type VARCHAR(80), entity_id VARCHAR(120), action VARCHAR(120))"))
                connection.execute(sa.text("INSERT INTO user_account VALUES (1)"))
                connection.execute(sa.text("INSERT INTO commission VALUES (1, 'deleted', '2026-11-15 10:00:00', '2026-11-14 10:00:00', '2026-11-15 10:00:00'), (2, 'deleted', '2026-11-15 10:00:00', NULL, NULL), (3, 'deleted', '2026-11-15 10:00:00', NULL, NULL), (4, 'completion_review', NULL, '2026-11-14 11:00:00', NULL)"))
                connection.execute(sa.text("INSERT INTO erp_outbox_operation VALUES (1, 'refuse_commission')"))
                connection.execute(sa.text("INSERT INTO audit_log VALUES ('commission', '2', 'commission.reject'), ('commission', '3', 'commission.delete')"))
                with Operations.context(MigrationContext.configure(connection)):
                    migration.upgrade()
                    rows = connection.execute(sa.text("SELECT id, local_status, deleted_at, rejected_at, review_resolution FROM commission ORDER BY id")).all()
                    for row in rows[:2]:
                        self.assertEqual(row.local_status, "completion_review")
                        self.assertIsNone(row.deleted_at)
                        self.assertIsNotNone(row.rejected_at)
                        self.assertIsNone(row.review_resolution)
                    self.assertEqual(str(rows[0].rejected_at), "2026-11-14 10:00:00")
                    self.assertEqual(rows[2].local_status, "deleted")
                    self.assertIsNotNone(rows[2].deleted_at)
                    self.assertIsNone(rows[2].rejected_at)
                    self.assertIsNone(rows[3].rejected_at)
                    inspector = sa.inspect(connection)
                    self.assertTrue(any(item["column_names"] == ["reissued_from_commission_id"] for item in inspector.get_unique_constraints("commission")))
                    self.assertTrue(any(item["constrained_columns"] == ["review_resolved_by_user_id"] for item in inspector.get_foreign_keys("commission")))
                    migration.downgrade()
                self.assertEqual(connection.execute(sa.text("SELECT COUNT(*) FROM commission")).scalar(), 4)
        finally:
            with engine.begin() as connection:
                for name in names:
                    connection.execute(sa.text(f"DROP TABLE IF EXISTS {name}"))


if __name__ == "__main__":
    unittest.main()
