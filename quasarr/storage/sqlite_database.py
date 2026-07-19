# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337
"""SQLite key/value store for Quasarr.

Locking contract:
- Every public method takes the module-level `lock`, including
  `__init__` (which can create the table on first use) and
  `maintain` (which runs PRAGMA / VACUUM).
- The lock is reentrant on the per-process `FileLock` instance behind
  the handle returned by `get_lock("database")`, so nested calls in the
  same process (e.g. `__init__` then `retrieve`) do not self-deadlock.
  The handle rebuilds that instance after a fork, so forked workers do
  not inherit the parent's lock.
- Lock order: the database lock is always the *inner* lock when
  both config and database locks are involved. Never call into
  `quasarr.storage.config` from inside a DataBase method — that
  would invert the order and risks AB-BA deadlock across processes.
"""

import sqlite3
import time

from quasarr.providers.log import error, warn
from quasarr.storage.lock import get_lock, with_lock

lock = get_lock("database")

SQLITE_TIMEOUT_SECONDS = 30
SQLITE_BUSY_TIMEOUT_MS = 30000
SQLITE_LOCK_ATTEMPTS = 5


class DataBase(object):
    @with_lock(lock)
    def __init__(self, table):
        # Import shared_state inside the method to avoid circular import
        from quasarr.providers import shared_state

        self._table = self._validate_table_name(table)
        self._conn = self._connect_with_retry(shared_state.values["dbfile"])
        try:
            self._ensure_table()
        except Exception:
            self._conn.close()
            raise

    @staticmethod
    def _validate_table_name(table):
        if not table.replace("_", "").isalnum():
            raise ValueError(f'Invalid sqlite table name "{table}"')
        return table

    @staticmethod
    def _is_locked_error(error):
        message = str(error).lower()
        return "locked" in message or "busy" in message

    @staticmethod
    def _is_integrity_error(error):
        message = str(error).lower()
        return (
            "malformed" in message
            or "corrupt" in message
            or "not a database" in message
            or "file is encrypted" in message
        )

    @classmethod
    def _connect(cls, dbfile):
        conn = sqlite3.connect(
            dbfile, check_same_thread=False, timeout=SQLITE_TIMEOUT_SECONDS
        )
        try:
            conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        except sqlite3.OperationalError as e:
            if cls._is_locked_error(e) or cls._is_integrity_error(e):
                conn.close()
                raise
            warn(
                "Continuing after sqlite startup PRAGMA warning. Concurrent "
                f"Quasarr database access may hit sqlite locks more often: {e}"
            )
        except sqlite3.DatabaseError:
            conn.close()
            raise
        return conn

    @classmethod
    def _connect_with_retry(cls, dbfile):
        last_error = None
        for attempt in range(SQLITE_LOCK_ATTEMPTS):
            try:
                return cls._connect(dbfile)
            except sqlite3.OperationalError as e:
                last_error = e
                if not cls._is_locked_error(e) or attempt + 1 == SQLITE_LOCK_ATTEMPTS:
                    break
                time.sleep(min(5, 0.5 * (attempt + 1)))
            except sqlite3.DatabaseError as e:
                last_error = e
                break
        cls._log_database_error(last_error)
        raise last_error

    @classmethod
    @with_lock(lock)
    def maintain(cls, dbfile):
        conn = None
        try:
            conn = cls._connect_with_retry(dbfile)
            result = conn.execute("PRAGMA integrity_check").fetchone()
            if result and result[0] != "ok":
                error(
                    "Quasarr.db integrity check failed. Restore a healthy backup "
                    f"or delete Quasarr.db to recreate it: {result[0]}"
                )
                return False
            journal_mode = conn.execute("PRAGMA journal_mode").fetchone()
            wal_active = journal_mode and str(journal_mode[0]).lower() == "wal"
            if wal_active:
                checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint and checkpoint[0]:
                    warn(
                        "Quasarr.db startup maintenance skipped because the WAL "
                        "checkpoint is busy. Check for another running Quasarr instance "
                        "if this repeats."
                    )
                    return None
                conn.execute("PRAGMA journal_mode = DELETE").fetchone()

            conn.execute("VACUUM")
            conn.commit()
            return True
        except sqlite3.OperationalError as e:
            if cls._is_integrity_error(e):
                error(
                    "Quasarr.db integrity check failed. Restore a healthy backup "
                    f"or delete Quasarr.db to recreate it: {e}"
                )
                return False
            warn(
                "Quasarr.db startup maintenance skipped because sqlite could not "
                "open or maintain the database. Check for another running Quasarr "
                f"instance, path/permission problems, or very slow storage: {e}"
            )
            return None
        except sqlite3.DatabaseError as e:
            error(
                "Quasarr.db integrity check failed. Restore a healthy backup "
                f"or delete Quasarr.db to recreate it: {e}"
            )
            return False
        finally:
            if conn:
                conn.close()

    @staticmethod
    def _log_locked_database(error_message):
        warn(
            "Quasarr.db stayed locked after a short retry. Concurrent Quasarr "
            "database access is holding the sqlite lock too long; check for "
            f"another Quasarr instance or very slow storage if this repeats: {error_message}"
        )

    @classmethod
    def _log_database_error(cls, error_message):
        if cls._is_locked_error(error_message):
            cls._log_locked_database(error_message)
        elif cls._is_integrity_error(error_message):
            error(
                "Quasarr.db integrity check failed. Restore a healthy backup "
                f"or delete Quasarr.db to recreate it: {error_message}"
            )
        else:
            error(f"Error accessing Quasarr.db: {error_message}")

    def _with_retry(self, operation):
        last_error = None
        for attempt in range(SQLITE_LOCK_ATTEMPTS):
            try:
                return operation()
            except sqlite3.OperationalError as e:
                last_error = e
                if not self._is_locked_error(e) or attempt + 1 == SQLITE_LOCK_ATTEMPTS:
                    break
                time.sleep(min(5, 0.5 * (attempt + 1)))
        self._log_database_error(last_error)
        raise last_error

    def _rollback(self):
        try:
            self._conn.rollback()
        except sqlite3.Error as e:
            warn(f"Quasarr.db rollback after failed operation also failed: {e}")

    def _ensure_table(self):
        def operation():
            try:
                if not self._conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?;",
                    (self._table,),
                ).fetchall():
                    self._conn.execute(
                        f"CREATE TABLE IF NOT EXISTS {self._table} (key, value)"
                    )
                    self._conn.commit()
            except sqlite3.OperationalError:
                self._rollback()
                raise

        self._with_retry(operation)

    @with_lock(lock)
    def retrieve(self, key):
        def operation():
            query = f"SELECT value FROM {self._table} WHERE key=?"
            res = self._conn.execute(query, (key,)).fetchone()
            return res[0] if res else None

        return self._with_retry(operation)

    @with_lock(lock)
    def retrieve_all(self, key):
        def operation():
            query = (
                f"SELECT distinct value FROM {self._table} WHERE key=? ORDER BY value"
            )
            res = self._conn.execute(query, (key,))
            return [str(r[0]) for r in res]

        return self._with_retry(operation)

    @with_lock(lock)
    def retrieve_all_titles(self):
        def operation():
            query = f"SELECT distinct key, value FROM {self._table} ORDER BY key"
            res = self._conn.execute(query)
            items = [[str(r[0]), str(r[1])] for r in res]
            return items if items else None

        return self._with_retry(operation)

    @with_lock(lock)
    def store(self, key, value):
        def operation():
            try:
                query = f"INSERT INTO {self._table} VALUES (?, ?)"
                self._conn.execute(query, (key, value))
                self._conn.commit()
                return True
            except sqlite3.OperationalError:
                self._rollback()
                raise

        return self._with_retry(operation)

    @with_lock(lock)
    def update_store(self, key, value):
        def operation():
            try:
                delete_query = f"DELETE FROM {self._table} WHERE key=?"
                self._conn.execute(delete_query, (key,))
                insert_query = f"INSERT INTO {self._table} VALUES (?, ?)"
                self._conn.execute(insert_query, (key, value))
                self._conn.commit()
                return True
            except sqlite3.OperationalError:
                self._rollback()
                raise

        return self._with_retry(operation)

    @with_lock(lock)
    def delete(self, key):
        def operation():
            try:
                query = f"DELETE FROM {self._table} WHERE key=?"
                self._conn.execute(query, (key,))
                self._conn.commit()
                return True
            except sqlite3.OperationalError:
                self._rollback()
                raise

        return self._with_retry(operation)

    @with_lock(lock)
    def reset(self):
        def operation():
            try:
                self._conn.execute(f"DROP TABLE IF EXISTS {self._table}")
                self._conn.commit()
                return True
            except sqlite3.OperationalError:
                self._rollback()
                raise

        return self._with_retry(operation)
