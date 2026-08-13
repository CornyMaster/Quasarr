# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

"""Durable, replayable identities for the helper's terminal package reports.

An HTTP 200 never proved that a package really reached its terminal state: the
answer can be lost, and the side effects behind it - a JDownloader submission, a
failed-history row, a notification, a counter - are not repeatable. Every
version-two terminal request therefore names one server-side operation whose
progress is persisted before and after the single irreversible step, so a retry
after any crash replays instead of duplicating.

Records are secret-free by construction: the exact key set below carries no URL,
title, password, solver, notification, or link. The operation identity itself is
derived from the package ID rather than stored, so it survives a restart on
either side without a lookup table.
"""

import hashlib
import json
import re
import time
from contextlib import contextmanager

from quasarr.providers.log import debug, warn
from quasarr.storage.lock import get_lock

TERMINAL_OPERATION_DOMAIN = "sponsors-helper-terminal-v2"
TERMINAL_OPERATION_TABLE = "sponsors_helper_terminal_operations"
MAXIMUM_TERMINAL_OPERATIONS = 4096
COMPLETED_OPERATION_RETENTION_SECONDS = 7 * 24 * 60 * 60

TERMINAL_STATES = ("downloaded", "failed", "disabled")
OPERATION_STATES = ("prepared", "submitted", "complete")
OPERATION_RECORD_KEYS = frozenset(
    {
        "state",
        "terminal_state",
        "package_id",
        "created_epoch",
        "updated_epoch",
        "package_removed",
        "package_terminal",
    }
)

OPENED = "opened"
RESUMED = "resumed"
APPLIED = "applied"
CONFLICT = "conflict"
CAPACITY = "capacity"

_OPERATION_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
# Admission only: it guards the shared capacity of the table, never one
# operation's side effects, which `operation_lock` owns per operation.
_operation_lock = get_lock("terminal_operations")


def terminal_operation_id(package_id):
    """The stable terminal operation identity of one package.

    Derived rather than stored so it survives a restart on either side and
    carries no URL or title.
    """
    material = f"{TERMINAL_OPERATION_DOMAIN}\n{package_id}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _epoch(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def decode_operation_record(value):
    """Project one persisted row onto the exact record shape, or None.

    Total for any input: an unreadable row can never brick a terminal request,
    it simply authorizes nothing.
    """
    try:
        record = json.loads(value) if isinstance(value, str) else None
    except (TypeError, ValueError, RecursionError):
        return None
    if not isinstance(record, dict) or set(record) != OPERATION_RECORD_KEYS:
        return None
    if record["state"] not in OPERATION_STATES:
        return None
    if record["terminal_state"] not in TERMINAL_STATES:
        return None
    if not isinstance(record["package_id"], str) or not record["package_id"]:
        return None
    if not _epoch(record["created_epoch"]) or not _epoch(record["updated_epoch"]):
        return None
    if not isinstance(record["package_removed"], bool):
        return None
    if not isinstance(record["package_terminal"], bool):
        return None
    return record


def _encode(record):
    return json.dumps(record, sort_keys=True, separators=(",", ":"))


class TerminalOperationService:
    """The durable state machine behind one helper terminal confirmation.

    Every decision runs inside a `mutate_value` transaction that re-reads the
    row it acts on, and no callback ever resolves storage, a device, or
    settings. Capacity and pruning are proven before the transaction opens,
    because enumerating rows inside one is neither allowed nor sound.

    A durable record alone still cannot stop two concurrent requests from
    reading the same phase and both acting on it, so each operation also owns a
    lock of its own that a caller holds across its whole transition. Lock order
    is operation lock -> admission lock -> database lock, never the reverse,
    and no external call is ever made while a storage lock is held.
    """

    def __init__(self, shared_state, clock=time.time):
        self._shared_state = shared_state
        self._clock = clock

    def _table(self):
        return self._shared_state.get_db(TERMINAL_OPERATION_TABLE)

    def _validate(self, operation_id, package_id, terminal_state):
        if terminal_state not in TERMINAL_STATES:
            raise ValueError("Unsupported terminal state")
        if not isinstance(package_id, str) or not package_id:
            raise ValueError("Invalid package_id")
        if not isinstance(operation_id, str) or not _OPERATION_ID_PATTERN.fullmatch(
            operation_id
        ):
            raise ValueError("Invalid terminal_operation_id")
        if operation_id != terminal_operation_id(package_id):
            raise ValueError("Invalid terminal_operation_id")

    @staticmethod
    def _result(outcome, record):
        return {"outcome": outcome, "record": record}

    @staticmethod
    def operation_lock(operation_id):
        """The cross-process lock that serializes one operation's side effects.

        The path is a digest of the identity rather than the identity itself,
        so nothing a caller sends can shape a file name, and it is stable for
        the same operation in every process and every service instance.
        """
        digest = hashlib.sha256(str(operation_id).encode("utf-8")).hexdigest()
        return get_lock(f"terminal_operation_{digest}")

    @contextmanager
    def exclusive(self, operation_id, package_id, terminal_state):
        """Admit one operation and hold it for the caller's whole transition.

        The identity is validated before anything is locked, and the record is
        re-read inside the lock, so what the caller decides from is what the
        store held after every earlier attempt finished. The caller keeps the
        lock until it leaves this block, which has to span the external side
        effect and the durable phase that records it.
        """
        self._validate(operation_id, package_id, terminal_state)
        with self.operation_lock(operation_id):
            yield self.begin(operation_id, package_id, terminal_state)

    def _rows(self):
        return self._table().retrieve_all_titles() or ()

    def _entries(self):
        """Every enumerated row as `(key, raw_value, record_or_None)`."""
        entries = []
        for row in self._rows():
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            key = row[0]
            if not isinstance(key, str):
                continue
            entries.append((key, row[1], decode_operation_record(row[1])))
        return entries

    def prune_completed(self):
        """Drop expired complete rows, oldest first, comparing what was read.

        Deletion compares the exact value this enumeration saw, so a row another
        process replaced meanwhile survives. An unreadable row authorizes
        nothing and is dropped the same way, because keeping it would deny
        capacity forever.
        """
        now = int(self._clock())
        expirable = []
        for key, raw, record in self._entries():
            if record is None:
                expirable.append((0, key, raw))
                continue
            if record["state"] != "complete":
                continue
            if now - record["updated_epoch"] >= COMPLETED_OPERATION_RETENTION_SECONDS:
                expirable.append((record["updated_epoch"], key, raw))

        pruned = 0
        table = self._table()
        for _updated, key, raw in sorted(expirable):
            if table.delete_exact(key, raw):
                pruned += 1
        return pruned

    def _capacity_available(self):
        """Whether one more operation may open, after expired rows are gone."""
        return len(self._entries()) < MAXIMUM_TERMINAL_OPERATIONS

    def begin(self, operation_id, package_id, terminal_state):
        """Open or resume the operation of one terminal report.

        A repeated identity resumes at whatever phase it reached, so the caller
        can replay instead of repeating a side effect. A stored row naming
        another package or terminal state is a conflict and writes nothing.

        Retention runs first, on every request rather than only on a full
        table: an operation ID names a package, so a package that is added
        again can only ever reach a different terminal state once the expired
        record of its previous life is really gone.
        """
        self._validate(operation_id, package_id, terminal_state)
        with _operation_lock:
            self.prune_completed()
            table = self._table()
            if decode_operation_record(table.retrieve(operation_id)) is None:
                # Capacity is proven before any terminal side effect, never inside
                # the transaction: a mutation callback may not enumerate storage.
                if not self._capacity_available():
                    warn(
                        "Refusing a new terminal operation; the operation table is full"
                    )
                    return self._result(CAPACITY, None)

            now = int(self._clock())
            decided = {}

            def decide(current_value):
                record = decode_operation_record(current_value)
                if record is None:
                    opened = {
                        "state": "prepared",
                        "terminal_state": terminal_state,
                        "package_id": package_id,
                        "created_epoch": now,
                        "updated_epoch": now,
                        "package_removed": False,
                        "package_terminal": False,
                    }
                    decided.update(outcome=OPENED, record=opened)
                    return _encode(opened)
                if (
                    record["package_id"] != package_id
                    or record["terminal_state"] != terminal_state
                ):
                    decided.update(outcome=CONFLICT, record=record)
                    return current_value
                decided.update(outcome=RESUMED, record=record)
                return current_value

            table.mutate_value(operation_id, decide)
        if decided["outcome"] == CONFLICT:
            debug("Refusing a terminal operation identity bound to another report")
        return self._result(decided["outcome"], decided["record"])

    def _advance(self, operation_id, package_id, terminal_state, target, flags=None):
        self._validate(operation_id, package_id, terminal_state)
        now = int(self._clock())
        decided = {}

        def decide(current_value):
            record = decode_operation_record(current_value)
            if (
                record is None
                or record["package_id"] != package_id
                or record["terminal_state"] != terminal_state
            ):
                decided.update(outcome=CONFLICT, record=record)
                return current_value
            if OPERATION_STATES.index(record["state"]) >= OPERATION_STATES.index(
                target
            ):
                decided.update(outcome=RESUMED, record=record)
                return current_value
            advanced = dict(record)
            advanced.update(state=target, updated_epoch=now, **(flags or {}))
            decided.update(outcome=APPLIED, record=advanced)
            return _encode(advanced)

        self._table().mutate_value(operation_id, decide)
        return self._result(decided["outcome"], decided["record"])

    def mark_submitted(self, operation_id, package_id, terminal_state):
        """Record that the one irreversible step of this operation happened."""
        return self._advance(operation_id, package_id, terminal_state, "submitted")

    def mark_complete(
        self,
        operation_id,
        package_id,
        terminal_state,
        *,
        package_removed,
        package_terminal,
    ):
        """Record the confirmed package outcome this operation may replay."""
        return self._advance(
            operation_id,
            package_id,
            terminal_state,
            "complete",
            flags={
                "package_removed": bool(package_removed),
                "package_terminal": bool(package_terminal),
            },
        )

    def snapshot(self, operation_id):
        """The current record of one operation, or None. Never writes."""
        if not isinstance(operation_id, str):
            return None
        return decode_operation_record(self._table().retrieve(operation_id))

    def count_by_state(self):
        """Active operations by state only - no identifier ever leaves here."""
        counts = {state: 0 for state in OPERATION_STATES}
        for _key, _raw, record in self._entries():
            if record is not None:
                counts[record["state"]] += 1
        return counts
