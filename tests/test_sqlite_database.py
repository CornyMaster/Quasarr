# -*- coding: utf-8 -*-

import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from quasarr.providers import shared_state
from quasarr.storage.config import Config
from quasarr.storage.sqlite_database import SQLITE_BUSY_TIMEOUT_MS, DataBase


class SQLiteDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.dbfile = os.path.join(self.tmpdir.name, "Quasarr.db")
        shared_state.values = {"dbfile": self.dbfile}
        shared_state.lock = None

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_crud_operations_survive_maintenance(self):
        db = DataBase("example_table")

        self.assertTrue(db.store("first", "one"))
        self.assertTrue(db.update_store("first", "two"))
        self.assertEqual("two", db.retrieve("first"))
        self.assertEqual([["first", "two"]], db.retrieve_all_titles())

        self.assertTrue(DataBase.maintain(self.dbfile))
        reopened = DataBase("example_table")

        self.assertEqual("two", reopened.retrieve("first"))
        self.assertTrue(reopened.delete("first"))
        self.assertIsNone(reopened.retrieve("first"))
        db._conn.close()
        reopened._conn.close()

    def test_mutate_value_preserves_both_concurrent_observations(self):
        first = DataBase("crypter_cooldowns")
        second = DataBase("crypter_cooldowns")
        first.update_store("filecrypt", json.dumps({"observations": []}))
        first_callback_started = threading.Event()
        second_mutation_started = threading.Event()
        errors = []

        def append_observation(current_value, package_id):
            record = json.loads(current_value)
            record["observations"].append(package_id)
            return json.dumps(record)

        def mutate_first():
            try:
                def mutator(current_value):
                    first_callback_started.set()
                    if not second_mutation_started.wait(2):
                        raise RuntimeError("second mutation did not start")
                    time.sleep(0.05)
                    return append_observation(current_value, "package-a")

                first.mutate_value("filecrypt", mutator)
            except Exception as error:
                errors.append(error)

        def mutate_second():
            try:
                if not first_callback_started.wait(2):
                    raise RuntimeError("first callback did not start")
                second_mutation_started.set()
                second.mutate_value(
                    "filecrypt",
                    lambda value: append_observation(value, "package-b"),
                )
            except Exception as error:
                errors.append(error)

        first_thread = threading.Thread(target=mutate_first)
        second_thread = threading.Thread(target=mutate_second)
        first_thread.start()
        second_thread.start()
        first_thread.join(5)
        second_thread.join(5)

        try:
            self.assertFalse(first_thread.is_alive())
            self.assertFalse(second_thread.is_alive())
            self.assertEqual([], errors)
            stored = json.loads(first.retrieve("filecrypt"))
            self.assertEqual(
                {"package-a", "package-b"}, set(stored["observations"])
            )
        finally:
            first._conn.close()
            second._conn.close()

    def test_mutate_value_rolls_back_when_mutator_raises(self):
        db = DataBase("crypter_cooldowns")
        db.update_store("filecrypt", "original")
        calls = {"count": 0}

        def fail_mutation(_current_value):
            calls["count"] += 1
            raise RuntimeError("mutation failed")

        try:
            with self.assertRaisesRegex(RuntimeError, "mutation failed"):
                db.mutate_value("filecrypt", fail_mutation)

            self.assertEqual(1, calls["count"])
            self.assertEqual("original", db.retrieve("filecrypt"))
        finally:
            db._conn.close()

    def test_mutate_value_rejects_nested_database_and_config_calls(self):
        db = DataBase("crypter_cooldowns")
        db.update_store("filecrypt", "original")

        try:
            with self.assertRaisesRegex(RuntimeError, "mutation callback"):
                db.mutate_value(
                    "filecrypt", lambda _current_value: db.retrieve("filecrypt")
                )

            with self.assertRaisesRegex(RuntimeError, "mutation callback"):
                db.mutate_value(
                    "filecrypt", lambda _current_value: Config("API").get("key")
                )

            self.assertEqual("original", db.retrieve("filecrypt"))
        finally:
            db._conn.close()

    def test_rejects_invalid_table_name(self):
        with self.assertRaises(ValueError):
            DataBase("bad-table")

    def test_maintenance_skips_when_connection_fails(self):
        with patch.object(
            DataBase,
            "_connect_with_retry",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            self.assertIsNone(DataBase.maintain(self.dbfile))

    def test_non_lock_connection_error_uses_generic_database_log(self):
        with (
            patch.object(
                DataBase,
                "_connect",
                side_effect=sqlite3.OperationalError("unable to open database file"),
            ),
            patch.object(DataBase, "_log_locked_database") as lock_log,
            patch("quasarr.storage.sqlite_database.error") as generic_log,
        ):
            with self.assertRaises(sqlite3.OperationalError):
                DataBase._connect_with_retry(self.dbfile)

        lock_log.assert_not_called()
        generic_log.assert_called_once()

    def test_integrity_error_uses_recovery_log(self):
        for message in (
            "database disk image is malformed",
            "file is not a database",
            "database is corrupt",
            "file is encrypted or is not a database",
        ):
            with self.subTest(message=message):
                with (
                    patch.object(
                        DataBase,
                        "_connect",
                        side_effect=sqlite3.OperationalError(message),
                    ),
                    patch("quasarr.storage.sqlite_database.error") as error_log,
                ):
                    with self.assertRaises(sqlite3.OperationalError):
                        DataBase._connect_with_retry(self.dbfile)

                error_log.assert_called_once()
                self.assertIn("Restore a healthy backup", error_log.call_args.args[0])

    def test_database_error_uses_recovery_log(self):
        with (
            patch.object(
                DataBase,
                "_connect",
                side_effect=sqlite3.DatabaseError("file is not a database"),
            ),
            patch("quasarr.storage.sqlite_database.error") as error_log,
        ):
            with self.assertRaises(sqlite3.DatabaseError):
                DataBase._connect_with_retry(self.dbfile)

        error_log.assert_called_once()
        self.assertIn("Restore a healthy backup", error_log.call_args.args[0])

    def test_busy_timeout_setup_error_closes_connection(self):
        class FakeConnection:
            def __init__(self):
                self.closed = False

            def execute(self, query):
                if query == f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}":
                    raise sqlite3.OperationalError("database is locked")
                return self

            def fetchone(self):
                return ("wal",)

            def close(self):
                self.closed = True

        fake_connection = FakeConnection()

        with patch("sqlite3.connect", return_value=fake_connection):
            with self.assertRaises(sqlite3.OperationalError):
                DataBase._connect(self.dbfile)

        self.assertTrue(fake_connection.closed)

    def test_connect_does_not_force_wal_mode(self):
        db = DataBase("example_table")
        try:
            journal_mode = db._conn.execute("PRAGMA journal_mode").fetchone()
            self.assertEqual("delete", str(journal_mode[0]).lower())
        finally:
            db._conn.close()

    def test_maintenance_converts_existing_wal_database_back_to_delete(self):
        conn = sqlite3.connect(self.dbfile)
        try:
            journal_mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()
            self.assertEqual("wal", str(journal_mode[0]).lower())
            conn.execute("CREATE TABLE example_table (key, value)")
            conn.commit()
        finally:
            conn.close()

        self.assertTrue(DataBase.maintain(self.dbfile))

        conn = sqlite3.connect(self.dbfile)
        try:
            journal_mode = conn.execute("PRAGMA journal_mode").fetchone()
            self.assertEqual("delete", str(journal_mode[0]).lower())
        finally:
            conn.close()

    def test_maintenance_skips_vacuum_when_existing_wal_checkpoint_is_busy(self):
        class FakeConnection:
            def __init__(self):
                self.query = None
                self.vacuum_ran = False

            def execute(self, query):
                self.query = query
                if query == "VACUUM":
                    self.vacuum_ran = True
                return self

            def fetchone(self):
                if self.query == "PRAGMA integrity_check":
                    return ("ok",)
                if self.query == "PRAGMA journal_mode":
                    return ("wal",)
                if self.query == "PRAGMA wal_checkpoint(TRUNCATE)":
                    return (1, 0, 0)
                return None

            def close(self):
                pass

        connection = FakeConnection()

        with (
            patch.object(DataBase, "_connect_with_retry", return_value=connection),
            patch("quasarr.storage.sqlite_database.warn") as warn_log,
        ):
            self.assertIsNone(DataBase.maintain(self.dbfile))

        self.assertFalse(connection.vacuum_ran)
        warn_log.assert_called_once()

    def test_rollback_failure_does_not_mask_original_write_error(self):
        class FakeConnection:
            def execute(self, *_args):
                raise sqlite3.OperationalError("database is locked")

            def rollback(self):
                raise sqlite3.OperationalError("rollback failed")

        db = object.__new__(DataBase)
        db._table = "example_table"
        db._conn = FakeConnection()

        with (
            patch("quasarr.storage.sqlite_database.warn") as warn_log,
            self.assertRaisesRegex(sqlite3.OperationalError, "database is locked"),
        ):
            db.delete("first")

        warn_log.assert_called()

    def test_sqlite_lock_variants_are_retried_as_lock_errors(self):
        for message in (
            "database is locked",
            "database table is locked",
            "database schema is locked",
            "database is busy",
        ):
            with self.subTest(message=message):
                attempts = {"count": 0}
                connection = object()

                def flaky_connect(
                    _dbfile,
                    error_message=message,
                    state=attempts,
                    conn=connection,
                ):
                    state["count"] += 1
                    if state["count"] == 1:
                        raise sqlite3.OperationalError(error_message)
                    return conn

                with patch.object(DataBase, "_connect", side_effect=flaky_connect):
                    self.assertIs(DataBase._connect_with_retry(self.dbfile), connection)
                self.assertEqual(2, attempts["count"])

    def test_maintenance_reports_integrity_failure(self):
        class FakeConnection:
            def execute(self, query):
                if query == "PRAGMA integrity_check":
                    return self
                return self

            def fetchone(self):
                return ("database disk image is malformed",)

            def close(self):
                pass

        with patch.object(
            DataBase, "_connect_with_retry", return_value=FakeConnection()
        ):
            self.assertFalse(DataBase.maintain(self.dbfile))

    def test_maintenance_reports_integrity_check_operational_error(self):
        class FakeConnection:
            def execute(self, _query):
                raise sqlite3.OperationalError("database disk image is malformed")

            def close(self):
                pass

        with patch.object(
            DataBase, "_connect_with_retry", return_value=FakeConnection()
        ):
            self.assertFalse(DataBase.maintain(self.dbfile))

    def test_maintenance_reports_integrity_check_database_error(self):
        class FakeConnection:
            def execute(self, _query):
                raise sqlite3.DatabaseError("database disk image is malformed")

            def close(self):
                pass

        with patch.object(
            DataBase, "_connect_with_retry", return_value=FakeConnection()
        ):
            self.assertFalse(DataBase.maintain(self.dbfile))

    def test_ensure_table_does_not_touch_schema_when_table_exists(self):
        class FakeResult:
            def fetchall(self):
                return [("CREATE TABLE example_table (key, value)",)]

        class FakeConnection:
            def __init__(self):
                self.commits = 0
                self.create_count = 0

            def execute(self, query, _params=None):
                if query.startswith("CREATE TABLE"):
                    self.create_count += 1
                return FakeResult()

            def commit(self):
                self.commits += 1

        db = object.__new__(DataBase)
        db._table = "example_table"
        db._conn = FakeConnection()

        db._ensure_table()

        self.assertEqual(0, db._conn.create_count)
        self.assertEqual(0, db._conn.commits)

    def test_ensure_table_rolls_back_failed_create_transaction_before_retry(self):
        class FakeResult:
            def fetchall(self):
                return []

        class FakeConnection:
            def __init__(self):
                self.commits = 0
                self.rollbacks = 0

            def execute(self, query, _params=None):
                return FakeResult()

            def commit(self):
                self.commits += 1
                if self.commits == 1:
                    raise sqlite3.OperationalError("database is locked")

            def rollback(self):
                self.rollbacks += 1

        db = object.__new__(DataBase)
        db._table = "example_table"
        db._conn = FakeConnection()

        db._ensure_table()

        self.assertEqual(2, db._conn.commits)
        self.assertEqual(1, db._conn.rollbacks)

    def test_init_closes_connection_when_table_setup_fails(self):
        class FakeConnection:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        fake_connection = FakeConnection()

        with (
            patch.object(DataBase, "_connect_with_retry", return_value=fake_connection),
            patch.object(
                DataBase,
                "_ensure_table",
                side_effect=sqlite3.OperationalError("database is locked"),
            ),
            self.assertRaises(sqlite3.OperationalError),
        ):
            DataBase("example_table")

        self.assertTrue(fake_connection.closed)


if __name__ == "__main__":
    unittest.main()
