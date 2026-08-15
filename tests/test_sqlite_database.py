# -*- coding: utf-8 -*-

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from quasarr.providers import shared_state
from quasarr.providers.crypter_candidates import enumerate_filecrypt_candidates
from quasarr.providers.crypter_cooldowns import CrypterCooldownService
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

    def test_existing_tables_gain_a_non_unique_key_index(self):
        with sqlite3.connect(self.dbfile) as connection:
            connection.execute("CREATE TABLE example_table (key, value)")
            connection.executemany(
                "INSERT INTO example_table VALUES (?, ?)",
                (("duplicate", "one"), ("duplicate", "two")),
            )

        database = DataBase("example_table")
        try:
            indexes = database._conn.execute(
                "PRAGMA index_list(example_table)"
            ).fetchall()
            key_indexes = [
                row
                for row in indexes
                if database._conn.execute(f"PRAGMA index_info({row[1]})").fetchall()
                == [(0, 0, "key")]
            ]

            self.assertEqual(1, len(key_indexes))
            self.assertEqual(0, key_indexes[0][2])
            self.assertEqual(["one", "two"], database.retrieve_all("duplicate"))
        finally:
            database._conn.close()

    def test_mutate_value_preserves_both_concurrent_observations(self):
        first = DataBase("crypter_cooldowns")
        second = DataBase("crypter_cooldowns")
        first.update_store("filecrypt", json.dumps({"observations": []}))
        first_callback_started = threading.Event()
        second_mutation_started = threading.Event()
        second_callback_started = threading.Event()
        finish_first_callback = threading.Event()
        callback_order = []
        errors = []

        def append_observation(current_value, package_id):
            record = json.loads(current_value)
            record["observations"].append(package_id)
            return json.dumps(record)

        def mutate_first():
            try:

                def mutator(current_value):
                    callback_order.append("first-started")
                    first_callback_started.set()
                    if not second_mutation_started.wait(2):
                        raise RuntimeError("second mutation did not start")
                    if not finish_first_callback.wait(2):
                        raise RuntimeError("first mutation was not released")
                    callback_order.append("first-finished")
                    return append_observation(current_value, "package-a")

                first.mutate_value("filecrypt", mutator)
            except Exception as error:
                errors.append(error)

        def mutate_second():
            try:
                if not first_callback_started.wait(2):
                    raise RuntimeError("first callback did not start")
                second_mutation_started.set()

                def mutator(current_value):
                    callback_order.append("second-started")
                    second_callback_started.set()
                    return append_observation(current_value, "package-b")

                second.mutate_value("filecrypt", mutator)
            except Exception as error:
                errors.append(error)

        first_thread = threading.Thread(target=mutate_first)
        second_thread = threading.Thread(target=mutate_second)
        first_thread.start()
        try:
            self.assertTrue(first_callback_started.wait(2))
            second_thread.start()
            self.assertTrue(second_mutation_started.wait(2))
            self.assertFalse(second_callback_started.wait(0.1))
        finally:
            finish_first_callback.set()
        first_thread.join(5)
        second_thread.join(5)

        try:
            self.assertFalse(first_thread.is_alive())
            self.assertFalse(second_thread.is_alive())
            self.assertEqual([], errors)
            self.assertEqual(
                ["first-started", "first-finished", "second-started"],
                callback_order,
            )
            stored = json.loads(first.retrieve("filecrypt"))
            self.assertEqual({"package-a", "package-b"}, set(stored["observations"]))
        finally:
            first._conn.close()
            second._conn.close()

    def test_mutate_value_upserts_returned_string_and_commits(self):
        writer = DataBase("crypter_cooldowns")
        reader = DataBase("crypter_cooldowns")
        writer.update_store("filecrypt", "original")

        try:
            result = writer.mutate_value(
                "filecrypt", lambda current_value: f"{current_value}-updated"
            )

            self.assertEqual("original-updated", result)
            self.assertEqual("original-updated", reader.retrieve("filecrypt"))
        finally:
            writer._conn.close()
            reader._conn.close()

    def test_mutate_value_none_deletes_value_and_commits(self):
        writer = DataBase("crypter_cooldowns")
        reader = DataBase("crypter_cooldowns")
        writer.update_store("filecrypt", "original")

        try:
            self.assertIsNone(
                writer.mutate_value("filecrypt", lambda _current_value: None)
            )
            self.assertIsNone(reader.retrieve("filecrypt"))
        finally:
            writer._conn.close()
            reader._conn.close()

    def test_mutate_values_commits_every_table_of_one_decision(self):
        writer = DataBase("crypter_cooldowns")
        reader = DataBase("crypter_events")
        writer.update_store("filecrypt", "record")

        try:
            result = writer.mutate_values(
                (("crypter_cooldowns", "filecrypt"), ("crypter_events", "pending")),
                lambda current_values: (f"{current_values[0]}-updated", "counted"),
            )

            self.assertEqual(("record-updated", "counted"), result)
            self.assertEqual("record-updated", writer.retrieve("filecrypt"))
            # The second table is created on demand and committed together.
            self.assertEqual("counted", reader.retrieve("pending"))
        finally:
            writer._conn.close()
            reader._conn.close()

    def test_mutate_values_rolls_back_every_table_on_failure(self):
        writer = DataBase("crypter_cooldowns")
        events = DataBase("crypter_events")
        writer.update_store("filecrypt", "record")
        events.update_store("pending", "counted")

        def explode(_current_values):
            raise RuntimeError("decision failed")

        try:
            with self.assertRaisesRegex(RuntimeError, "decision failed"):
                writer.mutate_values(
                    (("crypter_cooldowns", "filecrypt"), ("crypter_events", "pending")),
                    explode,
                )

            self.assertEqual("record", writer.retrieve("filecrypt"))
            self.assertEqual("counted", events.retrieve("pending"))
        finally:
            writer._conn.close()
            events._conn.close()

    def test_mutate_values_rejects_unusable_targets_and_returns(self):
        db = DataBase("crypter_cooldowns")

        try:
            with self.assertRaises(ValueError):
                db.mutate_values((), lambda _current_values: ())
            with self.assertRaises(ValueError):
                db.mutate_values(
                    (("crypter_events", "pending"), ("crypter_events", "pending")),
                    lambda current_values: current_values,
                )
            with self.assertRaises(ValueError):
                db.mutate_values(
                    (("crypter_events; DROP TABLE failed", "pending"),),
                    lambda current_values: current_values,
                )
            with self.assertRaises(TypeError):
                db.mutate_values(
                    (("crypter_cooldowns", "filecrypt"),),
                    lambda _current_values: ("first", "second"),
                )
            with self.assertRaises(TypeError):
                db.mutate_values(
                    (("crypter_cooldowns", "filecrypt"),),
                    lambda _current_values: (17,),
                )

            self.assertIsNone(db.retrieve("filecrypt"))
        finally:
            db._conn.close()

    def test_mutate_values_rolls_back_a_row_in_a_table_it_created_on_demand(self):
        writer = DataBase("crypter_cooldowns")
        writer.update_store("filecrypt", "record")

        def explode(_current_values):
            raise RuntimeError("decision failed")

        try:
            with self.assertRaisesRegex(RuntimeError, "decision failed"):
                writer.mutate_values(
                    (("crypter_cooldowns", "filecrypt"), ("crypter_events", "pending")),
                    explode,
                )

            self.assertEqual("record", writer.retrieve("filecrypt"))
            created = DataBase("crypter_events")
            try:
                # The table is created before the transaction and survives its
                # rollback, so the retry reuses it - but holds no row.
                self.assertEqual(
                    [],
                    created._conn.execute("SELECT * FROM crypter_events").fetchall(),
                )
            finally:
                created._conn.close()
        finally:
            writer._conn.close()

    def competing_write_error(self):
        """Write from a connection that refuses to wait for a read lock."""
        conn = sqlite3.connect(self.dbfile, timeout=0)
        try:
            conn.execute("PRAGMA busy_timeout = 0")
            conn.execute("UPDATE statistics SET value = '999' WHERE key = 'probes'")
            conn.commit()
            return None
        except sqlite3.OperationalError as error:
            return error
        finally:
            conn.close()

    def test_retrieve_values_reads_every_target_in_one_transaction(self):
        events = DataBase("crypter_events")
        statistics = DataBase("statistics")
        events.update_store("pending", "ledger")
        statistics.update_store("probes", "7")
        competing = {}
        original_select = DataBase._select_value

        def select_then_race(database, table, key):
            value = original_select(database, table, key)
            if "error" not in competing:
                competing["error"] = self.competing_write_error()
            return value

        try:
            with patch.object(DataBase, "_select_value", select_then_race):
                values = events.retrieve_values(
                    (("crypter_events", "pending"), ("statistics", "probes"))
                )

            self.assertEqual(("ledger", "7"), values)
            # A writer cannot commit between the two reads of one snapshot.
            self.assertIn("locked", str(competing["error"]).lower())
            self.assertEqual("7", statistics.retrieve("probes"))
        finally:
            events._conn.close()
            statistics._conn.close()

    def test_retrieve_values_reports_a_missing_table_without_creating_it(self):
        statistics = DataBase("statistics")
        statistics.update_store("probes", "7")

        try:
            self.assertEqual(
                (None, "7"),
                statistics.retrieve_values(
                    (("crypter_events", "pending"), ("statistics", "probes"))
                ),
            )

            tables = {
                row[0]
                for row in statistics._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            self.assertNotIn("crypter_events", tables)
        finally:
            statistics._conn.close()

    def test_retrieve_values_rejects_unusable_targets(self):
        db = DataBase("statistics")

        try:
            with self.assertRaises(ValueError):
                db.retrieve_values(())
            with self.assertRaises(ValueError):
                db.retrieve_values((("statistics", "probes"), ("statistics", "probes")))
            with self.assertRaises(ValueError):
                db.retrieve_values((("statistics; DROP TABLE failed", "probes"),))
        finally:
            db._conn.close()

    def test_delete_exact_removes_only_one_matching_row_and_commits(self):
        writer = DataBase("failed")
        reader = DataBase("failed")
        writer.store("package", "failure-before")
        writer.store("package", "failure-before")
        writer.store("package", "failure-after")

        try:
            self.assertTrue(writer.delete_exact("package", "failure-before"))
            rows = reader._conn.execute(
                "SELECT value FROM failed WHERE key=? ORDER BY rowid", ("package",)
            ).fetchall()

            self.assertEqual(
                [("failure-before",), ("failure-after",)],
                rows,
            )
            self.assertFalse(writer.delete_exact("package", "missing"))
        finally:
            writer._conn.close()
            reader._conn.close()

    def test_delete_exact_rolls_back_when_delete_fails(self):
        class DeleteFailureConnection:
            def __init__(self):
                self.rollbacks = 0

            def execute(self, query, _params=None):
                if query == "BEGIN IMMEDIATE":
                    return None
                if query.startswith("DELETE FROM failed"):
                    raise RuntimeError("delete failed")
                raise AssertionError(f"unexpected query: {query}")

            def rollback(self):
                self.rollbacks += 1

        db = object.__new__(DataBase)
        db._table = "failed"
        db._conn = DeleteFailureConnection()

        with self.assertRaisesRegex(RuntimeError, "delete failed"):
            db.delete_exact("package", "failure-before")

        self.assertEqual(1, db._conn.rollbacks)

    def test_mutate_value_invalid_return_rolls_back_and_calls_once(self):
        db = DataBase("crypter_cooldowns")
        db.update_store("filecrypt", "original")
        calls = {"count": 0}

        def return_invalid(_current_value):
            calls["count"] += 1
            return {"invalid": "mapping"}

        try:
            with self.assertRaisesRegex(TypeError, "str or None"):
                db.mutate_value("filecrypt", return_invalid)

            self.assertEqual(1, calls["count"])
            self.assertEqual("original", db.retrieve("filecrypt"))
        finally:
            db._conn.close()

    def test_mutate_value_rolls_back_when_begin_fails(self):
        class BeginFailureConnection:
            def __init__(self):
                self.rollbacks = 0

            def execute(self, query, _params=None):
                if query == "BEGIN IMMEDIATE":
                    raise RuntimeError("begin failed")
                raise AssertionError(f"unexpected query: {query}")

            def rollback(self):
                self.rollbacks += 1

        db = object.__new__(DataBase)
        db._table = "crypter_cooldowns"
        db._conn = BeginFailureConnection()
        callback_calls = {"count": 0}

        def callback(_current_value):
            callback_calls["count"] += 1
            return "updated"

        with self.assertRaisesRegex(RuntimeError, "begin failed"):
            db.mutate_value("filecrypt", callback)

        self.assertEqual(1, db._conn.rollbacks)
        self.assertEqual(0, callback_calls["count"])

    def test_cooldown_reads_do_not_start_real_sqlite_write_transactions(self):
        database = DataBase("crypter_cooldowns")

        class SharedState:
            values = {}

            @staticmethod
            def get_db(table):
                if table != "crypter_cooldowns":
                    raise AssertionError(f"unexpected table: {table}")
                return database

        now = 1_700_000_000
        service = CrypterCooldownService(SharedState(), clock=lambda: now)
        statements = []
        database._conn.set_trace_callback(statements.append)

        try:
            self.assertEqual("available", service.snapshot("filecrypt")["state"])
            self.assertEqual(
                [],
                [
                    statement
                    for statement in statements
                    if statement.upper().startswith(
                        ("BEGIN", "DELETE", "INSERT", "UPDATE", "REPLACE", "COMMIT")
                    )
                ],
            )

            packages = (
                "Quasarr_movies_00000000000000000000000000000000",
                "Quasarr_movies_11111111111111111111111111111111",
                "Quasarr_movies_22222222222222222222222222222222",
            )
            for package_id, fingerprint in zip(
                packages, ("a" * 64, "b" * 64, "c" * 64), strict=True
            ):
                decision = service.observe(
                    "filecrypt", package_id, fingerprint, "ip_block_suspected"
                )
            self.assertEqual(now + 24 * 60 * 60, decision["package_retry_after_epoch"])

            statements.clear()
            self.assertEqual("cooldown", service.snapshot("filecrypt")["state"])
            self.assertTrue(service.is_cooling("filecrypt"))
            self.assertEqual(now + 24 * 60 * 60, service.retry_after("filecrypt"))

            self.assertEqual(
                3,
                sum(
                    statement.upper().startswith("SELECT VALUE")
                    for statement in statements
                ),
            )
            self.assertEqual(
                [],
                [
                    statement
                    for statement in statements
                    if statement.upper().startswith(
                        ("BEGIN", "DELETE", "INSERT", "UPDATE", "REPLACE", "COMMIT")
                    )
                ],
            )
        finally:
            database._conn.set_trace_callback(None)
            database._conn.close()

    def test_expired_cooldown_cleanup_is_one_real_sqlite_transaction(self):
        database = DataBase("crypter_cooldowns")

        class SharedState:
            values = {}

            @staticmethod
            def get_db(table):
                if table != "crypter_cooldowns":
                    raise AssertionError(f"unexpected table: {table}")
                return database

        now = 1_700_000_000
        expired = {
            "state": "cooldown",
            "reason_code": "ip_block_suspected",
            "first_seen_epoch": now - 900,
            "last_seen_epoch": now - 900,
            "retry_after_epoch": now,
            "observations": [
                {
                    "package_id": "Quasarr_movies_00000000000000000000000000000000",
                    "link_fingerprint": "a" * 64,
                    "seen_at_epoch": now - 900,
                }
            ],
        }
        database.update_store("filecrypt", json.dumps(expired))
        service = CrypterCooldownService(SharedState(), clock=lambda: now)
        statements = []
        database._conn.set_trace_callback(statements.append)

        try:
            self.assertEqual("available", service.snapshot("filecrypt")["state"])

            self.assertEqual(
                1,
                sum(
                    statement.upper().startswith("BEGIN IMMEDIATE")
                    for statement in statements
                ),
            )
            self.assertEqual(
                1,
                sum(
                    statement.upper().startswith("DELETE FROM CRYPTER_COOLDOWNS")
                    for statement in statements
                ),
            )
            self.assertEqual(
                0,
                sum(
                    statement.upper().startswith("INSERT INTO CRYPTER_COOLDOWNS")
                    for statement in statements
                ),
            )
            self.assertIsNone(database.retrieve("filecrypt"))
        finally:
            database._conn.set_trace_callback(None)
            database._conn.close()

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


class CohortSweepTransactionTests(unittest.TestCase):
    """The version-two sweep writer against the real SQLite transaction boundary."""

    CRYPTER = "filecrypt"
    REASON = "ip_block_suspected"
    NOW = 1_700_000_000

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.dbfile = os.path.join(self.tmpdir.name, "Quasarr.db")
        shared_state.values = {"dbfile": self.dbfile}
        shared_state.lock = None
        self.databases = {}
        self.addCleanup(self.close_databases)
        self.minted = 0
        patcher = patch.object(
            CrypterCooldownService, "_new_identifier", lambda _self: self.mint()
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.service = CrypterCooldownService(self.state(), clock=lambda: self.NOW)
        self.store_cohort(5)

    def close_databases(self):
        for database in self.databases.values():
            database._conn.close()
        self.databases.clear()

    def mint(self):
        self.minted += 1
        return f"{self.minted:032d}"

    def state(self):
        databases = self.databases

        class SharedState:
            values = {
                "dbfile": self.dbfile,
                "crypter_cooldown_hours": 24,
                "crypter_block_mode": "defer",
            }

            @staticmethod
            def get_db(table):
                if table not in databases:
                    databases[table] = DataBase(table)
                return databases[table]

        return SharedState()

    def package(self, index):
        return f"Quasarr_movies_{index:032d}"

    def store_cohort(self, size):
        protected = self.databases.setdefault("protected", DataBase("protected"))
        for index in range(1, size + 1):
            protected.update_store(
                self.package(index),
                json.dumps(
                    {
                        "title": "Synthetic.Release",
                        "password": "",
                        "links": [
                            [
                                f"https://filecrypt.invalid/container/{index}",
                                self.CRYPTER,
                            ]
                        ],
                    }
                ),
            )

    def inventory(self):
        return enumerate_filecrypt_candidates(
            self.databases["protected"].retrieve_all_titles()
        )

    def report_blocked(self, offer):
        return self.service.record_cohort_blocked(
            self.CRYPTER,
            self.package_for(offer),
            offer["link_fingerprint"],
            offer["sweep_id"],
            offer["offer_id"],
            self.REASON,
            self.inventory(),
        )

    def package_for(self, offer):
        """Real fingerprints are hash-ordered, so never assume a member's owner."""
        return next(
            candidate.occurrences[0].package_id
            for candidate in self.inventory().candidates
            if candidate.fingerprint == offer["link_fingerprint"]
        )

    def test_decision_hold_and_ledger_commit_in_one_sqlite_transaction(self):
        offer = self.service.prepare_offer(self.CRYPTER, self.inventory())
        connection = self.databases["crypter_cooldowns"]._conn
        statements = []
        connection.set_trace_callback(statements.append)
        try:
            self.report_blocked(offer)
        finally:
            connection.set_trace_callback(None)

        transactional = [
            statement.split()[0].upper()
            for statement in statements
            if statement.upper().startswith(("BEGIN", "COMMIT", "ROLLBACK"))
        ]
        self.assertEqual(["BEGIN", "COMMIT"], transactional)
        self.assertEqual(
            ["crypter_cooldowns", "protected", "crypter_events"],
            [
                statement.split()[2]
                for statement in statements
                if statement.upper().startswith("INSERT")
            ],
        )

    def test_an_overflowing_ledger_rolls_back_every_row_of_the_transition(self):
        offer = self.service.prepare_offer(self.CRYPTER, self.inventory())
        held = self.package_for(offer)
        ledger_row = json.dumps(
            {"observations": 10**1000 - 1, "cooldowns": 0, "probes": 0}
        )
        self.databases.setdefault("crypter_events", DataBase("crypter_events"))
        self.databases["crypter_events"].update_store("pending", ledger_row)
        before = self.databases["crypter_cooldowns"].retrieve(self.CRYPTER)

        with self.assertRaises(OverflowError):
            self.report_blocked(offer)

        reopened = DataBase("crypter_cooldowns")
        self.addCleanup(reopened._conn.close)
        self.assertEqual(before, reopened.retrieve(self.CRYPTER))
        events = DataBase("crypter_events")
        self.addCleanup(events._conn.close)
        self.assertEqual(ledger_row, events.retrieve("pending"))
        protected = DataBase("protected")
        self.addCleanup(protected._conn.close)
        self.assertNotIn("deferred", json.loads(protected.retrieve(held)))


if __name__ == "__main__":
    unittest.main()
