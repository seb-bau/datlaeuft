import importlib.util
import os
from pathlib import Path
import unittest

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa
from sqlalchemy.engine import make_url


class InvoiceAttachmentMigrationTests(unittest.TestCase):
    def test_migration_preserves_invoices_and_cascades_attachment_metadata(self):
        url = make_url(os.environ.get("DATLAEUFT_TEST_DATABASE_URL", "sqlite://"))
        if url.get_backend_name() != "sqlite" and not (url.database or "").startswith("datlaeuft_test_"):
            self.fail("Migrationstests benötigen eine separate datlaeuft_test-Datenbank.")
        engine = sa.create_engine(url)
        self.addCleanup(engine.dispose)
        names = ("invoice_attachment", "invoice", "user_account")
        if any(sa.inspect(engine).has_table(name) for name in names):
            self.skipTest("Die Migration benötigt eigene leere Testtabellen.")
        path = Path(__file__).resolve().parents[1] / "migrations/versions/f4a6c8e0b2d7_invoice_pdf_attachments.py"
        spec = importlib.util.spec_from_file_location("invoice_attachment_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        try:
            with engine.begin() as connection:
                if url.get_backend_name() == "sqlite":
                    connection.execute(sa.text("PRAGMA foreign_keys=ON"))
                connection.execute(sa.text("CREATE TABLE invoice (id INTEGER PRIMARY KEY, invoice_number VARCHAR(160))"))
                connection.execute(sa.text("CREATE TABLE user_account (id INTEGER PRIMARY KEY)"))
                connection.execute(sa.text("INSERT INTO invoice VALUES (1, 'ALT-1'), (2, 'ALT-2')"))
                connection.execute(sa.text("INSERT INTO user_account VALUES (1)"))
                with Operations.context(MigrationContext.configure(connection)):
                    migration.upgrade()
                    self.assertEqual(connection.execute(sa.text("SELECT COUNT(*) FROM invoice")).scalar(), 2)
                    connection.execute(sa.text("INSERT INTO invoice_attachment (id, invoice_id, uploaded_by_user_id, original_filename, storage_name, sha256, size_bytes, page_count, position_index, created_at) VALUES (1, 1, 1, 'anlage.pdf', '1/anlage.pdf', :digest, 100, 1, 0, '2026-11-17 00:00:00')"), {"digest": "a" * 64})
                    connection.execute(sa.text("DELETE FROM invoice WHERE id=1"))
                    self.assertEqual(connection.execute(sa.text("SELECT COUNT(*) FROM invoice_attachment")).scalar(), 0)
                    migration.downgrade()
                self.assertEqual(connection.execute(sa.text("SELECT invoice_number FROM invoice")).scalar(), "ALT-2")
                self.assertEqual(connection.execute(sa.text("SELECT COUNT(*) FROM user_account")).scalar(), 1)
        finally:
            with engine.begin() as connection:
                for name in names:
                    connection.execute(sa.text(f"DROP TABLE IF EXISTS {name}"))
