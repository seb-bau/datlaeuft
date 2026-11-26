import importlib.util
import os
from pathlib import Path
import unittest

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa
from sqlalchemy.engine import make_url


class InvoiceWorkflowMigrationTests(unittest.TestCase):
    def test_migration_backfills_responsibility_and_preserves_invoice_documents(self):
        url = make_url(os.environ.get("DATLAEUFT_TEST_DATABASE_URL", "sqlite://"))
        if url.get_backend_name() != "sqlite" and not (url.database or "").startswith("datlaeuft_test_"):
            self.fail("Migrationstests benötigen eine separate datlaeuft_test-Datenbank.")
        engine = sa.create_engine(url)
        self.addCleanup(engine.dispose)
        names = ("invoice", "wowi_company_code", "user_account")
        if any(sa.inspect(engine).has_table(name) for name in names):
            self.skipTest("Die Migration benötigt eigene leere Testtabellen.")
        path = Path(__file__).resolve().parents[1] / "migrations/versions/f5b7d9e1a3c8_invoice_responsibility_cover_letter.py"
        spec = importlib.util.spec_from_file_location("invoice_workflow_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        try:
            with engine.begin() as connection:
                connection.execute(sa.text("CREATE TABLE user_account (id INTEGER PRIMARY KEY)"))
                connection.execute(sa.text("CREATE TABLE wowi_company_code (id INTEGER PRIMARY KEY, stationery_filename VARCHAR(255))"))
                connection.execute(sa.text("CREATE TABLE invoice (id INTEGER PRIMARY KEY, created_by_user_id INTEGER NOT NULL, document_filename VARCHAR(255), document_sha256 VARCHAR(64))"))
                connection.execute(sa.text("INSERT INTO user_account VALUES (1), (2)"))
                connection.execute(sa.text("INSERT INTO wowi_company_code VALUES (1, 'bestehend.pdf')"))
                connection.execute(sa.text("INSERT INTO invoice VALUES (1, 1, 'original.pdf', 'digest'), (2, 2, NULL, NULL)"))
                with Operations.context(MigrationContext.configure(connection)):
                    migration.upgrade()
                    rows = connection.execute(sa.text("SELECT responsible_user_id, responsibility_revision, document_filename, document_sha256 FROM invoice ORDER BY id")).all()
                    self.assertEqual(rows, [(1, 0, "original.pdf", "digest"), (2, 0, None, None)])
                    inspector = sa.inspect(connection)
                    self.assertFalse(next(column for column in inspector.get_columns("invoice") if column["name"] == "responsible_user_id")["nullable"])
                    self.assertTrue(any(item["constrained_columns"] == ["responsible_user_id"] for item in inspector.get_foreign_keys("invoice")))
                    migration.downgrade()
                self.assertEqual(connection.execute(sa.text("SELECT document_filename, document_sha256 FROM invoice WHERE id=1")).one(), ("original.pdf", "digest"))
                self.assertEqual(connection.execute(sa.text("SELECT stationery_filename FROM wowi_company_code")).scalar(), "bestehend.pdf")
        finally:
            with engine.begin() as connection:
                for name in names:
                    connection.execute(sa.text(f"DROP TABLE IF EXISTS {name}"))

    def test_completion_migration_leaves_existing_invoices_open_and_opt_in_disabled(self):
        url = make_url(os.environ.get("DATLAEUFT_TEST_DATABASE_URL", "sqlite://"))
        if url.get_backend_name() != "sqlite" and not (url.database or "").startswith("datlaeuft_test_"):
            self.fail("Migrationstests benötigen eine separate datlaeuft_test-Datenbank.")
        engine = sa.create_engine(url)
        self.addCleanup(engine.dispose)
        names = ("invoice", "wowi_company_code")
        if any(sa.inspect(engine).has_table(name) for name in names):
            self.skipTest("Die Migration benötigt eigene leere Testtabellen.")
        path = Path(__file__).resolve().parents[1] / "migrations/versions/18c65af3e290_invoice_completion.py"
        spec = importlib.util.spec_from_file_location("invoice_completion_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        try:
            with engine.begin() as connection:
                connection.execute(sa.text("CREATE TABLE wowi_company_code (id INTEGER PRIMARY KEY)"))
                connection.execute(sa.text("CREATE TABLE invoice (id INTEGER PRIMARY KEY, status VARCHAR(40) NOT NULL)"))
                connection.execute(sa.text("INSERT INTO wowi_company_code VALUES (1)"))
                connection.execute(sa.text("INSERT INTO invoice VALUES (1, 'finalized')"))
                with Operations.context(MigrationContext.configure(connection)):
                    migration.upgrade()
                    self.assertEqual(connection.execute(sa.text("SELECT tenant_charge_letter_enabled FROM wowi_company_code")).scalar(), 0)
                    self.assertEqual(connection.execute(sa.text("SELECT status, completed_at, completion_mode FROM invoice")).one(), ("finalized", None, None))
                    migration.downgrade()
                self.assertEqual(connection.execute(sa.text("SELECT status FROM invoice")).scalar(), "finalized")
        finally:
            with engine.begin() as connection:
                for name in names:
                    connection.execute(sa.text(f"DROP TABLE IF EXISTS {name}"))
