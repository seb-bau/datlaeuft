from contextlib import contextmanager
from datetime import datetime
from io import StringIO
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
import sqlalchemy as sa
from sqlalchemy.engine import make_url


class ApprovalMigrationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]
        self.database_url = os.environ.get("DATLAEUFT_TEST_DATABASE_URL", "sqlite://")
        url = make_url(self.database_url)
        if url.get_backend_name() != "sqlite" and not (url.database or "").startswith("datlaeuft_test"):
            self.fail("Migrationstests benötigen eine separate datlaeuft_test-Datenbank.")
        self.engine = sa.create_engine(self.database_url)
        self.addCleanup(self.engine.dispose)
        self.managed_tables = ("commission", "user_account", "alembic_version", "_alembic_tmp_commission", "_alembic_tmp_user_account")
        with self.engine.connect() as connection:
            existing = set(sa.inspect(connection).get_table_names()) & set(self.managed_tables)
            self.assertFalse(existing, f"Die Testtabellen sind bereits belegt: {sorted(existing)}")
        self.addCleanup(self.cleanup_schema)
        spec = importlib.util.spec_from_file_location("_approval_recovery_migration", self.root / "migrations/versions/a3c5e7f9b1d2_commission_approval.py")
        self.migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.migration)

    def cleanup_schema(self):
        with self.engine.begin() as connection:
            names = set(sa.inspect(connection).get_table_names()) & set(self.managed_tables)
            metadata = sa.MetaData()
            if names:
                metadata.reflect(connection, only=list(names))
                metadata.drop_all(connection)

    @contextmanager
    def schema(self, state="empty"):
        user_columns = [sa.Column("id", sa.Integer(), primary_key=True), sa.Column("name", sa.String(80), nullable=False)]
        commission_columns = [sa.Column("id", sa.Integer(), primary_key=True), sa.Column("title", sa.String(80), nullable=False)]
        metadata = sa.MetaData()
        if state != "empty":
            user_columns.append(sa.Column("internal_commission_approver_user_id", sa.Integer()))
        if state in {"users_complete", "commission_partial", "complete"}:
            user_columns.append(sa.Column("external_commission_approver_user_id", sa.Integer()))
        if state in {"commission_partial", "complete"}:
            commission_columns.extend((
                sa.Column("approval_state", sa.String(20), nullable=False, server_default="not_required"),
                sa.Column("approval_approver_user_id", sa.Integer()),
            ))
        if state == "complete":
            commission_columns.extend((
                sa.Column("approval_requested_at", sa.DateTime()),
                sa.Column("approved_by_user_id", sa.Integer()),
                sa.Column("approved_at", sa.DateTime()),
            ))
        users = sa.Table("user_account", metadata, *user_columns)
        commissions = sa.Table("commission", metadata, *commission_columns)
        if state in {"internal_foreign_key", "users_complete", "commission_partial", "complete"}:
            users.append_constraint(sa.ForeignKeyConstraint(
                ["internal_commission_approver_user_id"], ["user_account.id"], name="fk_user_internal_commission_approver", ondelete="RESTRICT",
            ))
        if state in {"users_complete", "commission_partial", "complete"}:
            users.append_constraint(sa.ForeignKeyConstraint(
                ["external_commission_approver_user_id"], ["user_account.id"], name="fk_user_external_commission_approver", ondelete="RESTRICT",
            ))
            for assignment in ("internal", "external"):
                column = f"{assignment}_commission_approver_user_id"
                sa.Index(f"ix_user_account_{column}", users.c[column])
        if state == "complete":
            for name, column in (("fk_commission_approval_approver", "approval_approver_user_id"), ("fk_commission_approved_by", "approved_by_user_id")):
                commissions.append_constraint(sa.ForeignKeyConstraint([column], ["user_account.id"], name=name, ondelete="RESTRICT"))
            for column in ("approval_state", "approval_approver_user_id"):
                sa.Index(f"ix_commission_{column}", commissions.c[column])
        try:
            with self.engine.begin() as connection:
                metadata.create_all(connection)
                connection.execute(users.insert(), [{"id": 1, "name": "Sachbearbeitung"}, {"id": 2, "name": "Freigabe"}])
                connection.execute(commissions.insert().values(id=1, title="Bestehender Auftrag"))
                if state != "empty":
                    connection.execute(users.update().where(users.c.id == 1).values(internal_commission_approver_user_id=2))
                if state in {"commission_partial", "complete"}:
                    connection.execute(commissions.update().values(approval_state="pending", approval_approver_user_id=2))
                if state == "complete":
                    connection.execute(commissions.update().values(approval_state="approved", approved_by_user_id=2, approved_at=datetime(2026, 11, 15, 10, 0)))
            yield
        finally:
            self.cleanup_schema()

    def upgrade(self):
        with self.engine.begin() as connection:
            with patch.object(self.migration, "op", Operations(MigrationContext.configure(connection))):
                self.migration.upgrade()

    def assert_schema_complete(self):
        with self.engine.connect() as connection:
            inspector = sa.inspect(connection)
            self.assertTrue({"internal_commission_approver_user_id", "external_commission_approver_user_id"} <= {column["name"] for column in inspector.get_columns("user_account")})
            self.assertTrue({"approval_state", "approval_approver_user_id", "approval_requested_at", "approved_by_user_id", "approved_at"} <= {column["name"] for column in inspector.get_columns("commission")})
            self.assertTrue({"fk_user_internal_commission_approver", "fk_user_external_commission_approver"} <= {item["name"] for item in inspector.get_foreign_keys("user_account")})
            self.assertTrue({"fk_commission_approval_approver", "fk_commission_approved_by"} <= {item["name"] for item in inspector.get_foreign_keys("commission")})
            self.assertTrue({"ix_user_account_internal_commission_approver_user_id", "ix_user_account_external_commission_approver_user_id"} <= {item["name"] for item in inspector.get_indexes("user_account")})
            self.assertTrue({"ix_commission_approval_state", "ix_commission_approval_approver_user_id"} <= {item["name"] for item in inspector.get_indexes("commission")})

    def test_upgrade_resumes_partial_ddl_without_resetting_users_or_approval_decisions(self):
        for state in ("empty", "internal_column", "internal_foreign_key", "users_complete", "commission_partial", "complete"):
            with self.subTest(state=state), self.schema(state):
                self.upgrade()
                self.assert_schema_complete()
                with self.engine.connect() as connection:
                    self.assertEqual(connection.scalar(sa.text("SELECT name FROM user_account WHERE id=1")), "Sachbearbeitung")
                    self.assertEqual(connection.scalar(sa.text("SELECT title FROM commission WHERE id=1")), "Bestehender Auftrag")
                    self.assertEqual(connection.scalar(sa.text("SELECT internal_commission_approver_user_id FROM user_account WHERE id=1")), None if state == "empty" else 2)
                    self.assertEqual(connection.scalar(sa.text("SELECT approval_state FROM commission WHERE id=1")), "approved" if state == "complete" else "pending" if state == "commission_partial" else "not_required")
                    if state == "complete":
                        self.assertEqual(connection.scalar(sa.text("SELECT approved_by_user_id FROM commission WHERE id=1")), 2)
                        self.assertIsNotNone(connection.scalar(sa.text("SELECT approved_at FROM commission WHERE id=1")))
                statements = []

                def record_statement(connection, cursor, statement, parameters, context, executemany):
                    if statement.lstrip().upper().startswith(("ALTER ", "CREATE ", "DROP ")):
                        statements.append(statement)

                sa.event.listen(self.engine, "before_cursor_execute", record_statement)
                try:
                    self.upgrade()
                finally:
                    sa.event.remove(self.engine, "before_cursor_execute", record_statement)
                self.assertEqual(statements, [])

    def test_existing_index_with_an_unexpected_definition_is_not_silently_accepted(self):
        with self.schema("internal_column"):
            with self.engine.begin() as connection:
                connection.execute(sa.text("CREATE INDEX ix_user_account_internal_commission_approver_user_id ON user_account (id)"))
            with self.assertRaisesRegex(RuntimeError, "Index"):
                self.upgrade()

    def test_offline_sql_still_contains_the_complete_upgrade(self):
        output = StringIO()
        context = MigrationContext.configure(dialect_name="mysql", opts={"as_sql": True, "output_buffer": output})
        with patch.object(self.migration, "op", Operations(context)):
            self.migration.upgrade()
        sql = output.getvalue()
        self.assertIn("ADD COLUMN internal_commission_approver_user_id", sql)
        self.assertIn("CREATE INDEX ix_user_account_internal_commission_approver_user_id", sql)
        self.assertIn("ADD CONSTRAINT fk_commission_approved_by", sql)

    def test_alembic_upgrade_releases_the_startup_session_and_finishes_a_partial_migration(self):
        with self.schema("internal_foreign_key"):
            with self.engine.begin() as connection:
                connection.execute(sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
                connection.execute(sa.text("INSERT INTO alembic_version (version_num) VALUES ('f1b3d5e7a9c2')"))
            app = Flask("migration-session-recovery")
            db = SQLAlchemy()
            app.config["SQLALCHEMY_DATABASE_URI"] = self.engine.url
            db.init_app(app)
            app.extensions["migrate"] = SimpleNamespace(db=db, configure_args={})
            config = Config()
            config.set_main_option("script_location", str(self.root / "migrations"))
            with app.app_context():
                unused_engine = db.engines[None]
                db.engines[None] = self.engine
                unused_engine.dispose()
                if db.engine.dialect.name in {"mysql", "mariadb"}:
                    def set_timeout(connection, record):
                        with connection.cursor() as cursor:
                            cursor.execute("SET SESSION lock_wait_timeout=1")

                    sa.event.listen(db.engine, "connect", set_timeout)
                db.session.execute(sa.text("SELECT id FROM user_account")).all()
                startup_connection = db.session.connection()
                self.assertTrue(startup_connection.in_transaction())
                with patch("logging.config.fileConfig"):
                    command.upgrade(config, "a3c5e7f9b1d2")
                self.assertTrue(startup_connection.closed)
                with db.engine.connect() as connection:
                    self.assertEqual(connection.scalar(sa.text("SELECT version_num FROM alembic_version")), "a3c5e7f9b1d2")
                db.session.remove()
            self.assert_schema_complete()


if __name__ == "__main__":
    unittest.main()
