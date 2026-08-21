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

Being admitted is not the same as having changed something, so the record also
carries the phase of its side effect. Nothing outside it can tell the two apart:
failed rows, disabled packages and JDownloader packages are also written by
earlier lives of the same release, by the automatic download path and by hand.
An operation still marked as never started therefore reconciles from nothing and
applies its transition, and one that already began its attempt may only
recognize artifacts carrying its own `operation_evidence`.

Records are secret-free by construction: the exact key set below carries no URL,
title, password, solver, notification, or link. The operation identity itself is
derived from the package ID rather than stored, so it survives a restart on
either side without a lookup table, and the evidence adds the epoch the record
opened at so one life of a release can never answer for another.
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
EFFECT_NOT_STARTED = "not_started"
EFFECT_ATTEMPTING = "attempting"
EFFECT_APPLIED = "applied"
EFFECT_STATES = (EFFECT_NOT_STARTED, EFFECT_ATTEMPTING, EFFECT_APPLIED)
# The phase of the one notification a terminal failure sends. `attempting` is
# written before it is dispatched, because no store can prove afterwards
# whether the operator already saw it and a second one would be visible.
NOTIFICATION_NOT_STARTED = "not_started"
NOTIFICATION_ATTEMPTING = "attempting"
NOTIFICATION_RECORDED = "recorded"
NOTIFICATION_STATES = (
    NOTIFICATION_NOT_STARTED,
    NOTIFICATION_ATTEMPTING,
    NOTIFICATION_RECORDED,
)
# The only phases an operation can be in, in the order it passes through them.
# `applied` belongs to a state past `prepared` by construction, so a row can
# never claim an outcome no phase of this operation could have produced.
OPERATION_PHASES = (
    ("prepared", EFFECT_NOT_STARTED),
    ("prepared", EFFECT_ATTEMPTING),
    ("submitted", EFFECT_APPLIED),
    ("complete", EFFECT_APPLIED),
)
LEGACY_OPERATION_RECORD_KEYS = frozenset(
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
EFFECT_OPERATION_RECORD_KEYS = LEGACY_OPERATION_RECORD_KEYS | {"effect_state"}
OPERATION_RECORD_KEYS = EFFECT_OPERATION_RECORD_KEYS | {
    "failure_persisted",
    "notification_state",
}
# Decoded, never stored: which row shape answered, and therefore whether the
# record can still account for the side effects it applied.
LEGACY_PROVENANCE_KEY = "legacy_unproven"
# The key an artifact of one operation carries, in the package or history JSON
# it was written with. Non-secret by construction: it is a digest.
TERMINAL_OPERATION_MARKER = "terminal_operation"

OPENED = "opened"
RESUMED = "resumed"
APPLIED = "applied"
CONFLICT = "conflict"
CAPACITY = "capacity"
# A stored row this build cannot decode: not an absent one, and not garbage.
UNREADABLE = "unreadable"

_OPERATION_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SUBMISSION_COMMENT_PATTERN = re.compile(
    r"(?P<package_id>\S+) op:(?P<evidence>[0-9a-f]{64})"
)
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


def operation_evidence(record):
    """The token that proves one artifact was made by this operation record.

    A package ID is derived from the release, so every life of one release
    reuses one operation ID, and so do the failed rows, disabled packages and
    JDownloader packages those lives leave behind. The epoch the record opened
    at is what separates them: a record is only ever replaced by one that
    opened strictly later, whether the package came back inside the retention
    window or the record was pruned a whole one after it.
    """
    material = (
        f"{TERMINAL_OPERATION_DOMAIN}\n"
        f"{record['package_id']}\n{record['created_epoch']}"
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def submission_comment(package_id, evidence=None):
    """The JDownloader package comment one submission travels with.

    Every legacy, automatic and manual submission keeps sending the bare
    package ID, which is the identity the package projection reads. A terminal
    operation appends its own evidence, so a retry after a lost answer can tell
    the package it submitted from one an earlier life of the release left.
    """
    if not evidence:
        return package_id
    return f"{package_id} op:{evidence}"


def decode_submission_comment(comment):
    """The `(package_id, evidence)` one comment names. Total for any input.

    Anything that is not exactly the marked shape is its own package ID and
    proves no operation, which is what an unmarked or hand-written comment is.
    """
    if not isinstance(comment, str):
        return None, None
    match = _SUBMISSION_COMMENT_PATTERN.fullmatch(comment)
    if match is None:
        return comment, None
    return match["package_id"], match["evidence"]


def _epoch(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def decode_operation_record(value):
    """Project one persisted row onto the exact record shape, or None.

    Total for any input: an unreadable row can never brick a terminal request,
    it simply authorizes nothing. Rows written before the effect phase and
    before the failure bookkeeping existed are migrated on read rather than
    discarded, and a migration never invents what it cannot show. A prepared
    row proves the operation was admitted and nothing more, so it may not adopt
    an artifact it cannot show it produced; a seven-key row past `prepared`
    marked none of its artifacts at all and is flagged as unprovable, because
    the absence of a marker is not evidence of anything it did.
    """
    try:
        record = json.loads(value) if isinstance(value, str) else None
    except (TypeError, ValueError, RecursionError):
        return None
    if not isinstance(record, dict):
        return None
    keys = set(record)
    legacy_unproven = False
    failure_persisted = False
    notification_state = NOTIFICATION_NOT_STARTED
    if keys == OPERATION_RECORD_KEYS:
        effect_state = record["effect_state"]
        failure_persisted = record["failure_persisted"]
        notification_state = record["notification_state"]
    elif keys == EFFECT_OPERATION_RECORD_KEYS:
        effect_state = record["effect_state"]
    elif keys == LEGACY_OPERATION_RECORD_KEYS:
        effect_state = (
            EFFECT_NOT_STARTED if record["state"] == "prepared" else EFFECT_APPLIED
        )
        legacy_unproven = record["state"] != "prepared"
    else:
        return None
    if (record["state"], effect_state) not in OPERATION_PHASES:
        return None
    if not isinstance(failure_persisted, bool):
        return None
    if notification_state not in NOTIFICATION_STATES:
        return None
    if effect_state == EFFECT_NOT_STARTED and (
        failure_persisted or notification_state != NOTIFICATION_NOT_STARTED
    ):
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
    return {
        **record,
        "effect_state": effect_state,
        "failure_persisted": failure_persisted,
        "notification_state": notification_state,
        LEGACY_PROVENANCE_KEY: legacy_unproven,
    }


def _encode(record):
    return json.dumps(
        {key: record[key] for key in OPERATION_RECORD_KEYS},
        sort_keys=True,
        separators=(",", ":"),
    )


def _incremented(value):
    """One more than a stored counter, treating an unusable row as zero."""
    try:
        current = int(value)
    except (TypeError, ValueError):
        current = 0
    return str(current + 1)


def _opened(package_id, terminal_state, epoch):
    """The record one life of a package opens with, at the epoch it opened."""
    return {
        "state": "prepared",
        "terminal_state": terminal_state,
        "package_id": package_id,
        "created_epoch": epoch,
        "updated_epoch": epoch,
        "package_removed": False,
        "package_terminal": False,
        "effect_state": EFFECT_NOT_STARTED,
        "failure_persisted": False,
        "notification_state": NOTIFICATION_NOT_STARTED,
        LEGACY_PROVENANCE_KEY: False,
    }


def _advanced(record, **updates):
    """The record a transition leaves behind, in the current shape.

    Whatever shape the row was read in, what is written back is the current
    one, so the transition itself is what ends a migrated record's inability to
    account for its own side effects.
    """
    return {**record, **updates, LEGACY_PROVENANCE_KEY: False}


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
        process replaced meanwhile survives. A row this build cannot read is
        never reclaimed: it may be the record of an effect that already
        happened, and deleting it would let the same identity open a fresh
        operation and apply that effect a second time. It still occupies its
        slot, so capacity stays bounded and an operator repairs it.
        """
        now = int(self._clock())
        expirable = []
        for key, raw, record in self._entries():
            if record is None:
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
        another package or terminal state is a conflict and writes nothing, and
        a stored row this build cannot read at all is neither: an absent row
        proves nothing happened, an unreadable one proves nothing either way,
        so it authorizes no side effect and is answered as unavailable rather
        than reopened.

        Retention runs first, on every request rather than only on a full
        table: an operation ID names a package, so a package that is added
        again can only ever reach a different terminal state once the expired
        record of its previous life is really gone.
        """
        self._validate(operation_id, package_id, terminal_state)
        with _operation_lock:
            self.prune_completed()
            table = self._table()
            stored = table.retrieve(operation_id)
            if stored is None:
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
                if record is None and current_value is not None:
                    decided.update(outcome=UNREADABLE, record=None)
                    return current_value
                if record is None:
                    opened = _opened(package_id, terminal_state, now)
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
        elif decided["outcome"] == UNREADABLE:
            warn(
                "Refusing a terminal operation whose stored record cannot be read; "
                "it is preserved for repair"
            )
        return self._result(decided["outcome"], decided["record"])

    def _advance(self, operation_id, package_id, terminal_state, target, flags=None):
        self._validate(operation_id, package_id, terminal_state)
        now = int(self._clock())
        decided = {}
        target_phase = OPERATION_PHASES.index(target)

        def decide(current_value):
            record = self._own(current_value, package_id, terminal_state)
            if record is None:
                decided.update(outcome=CONFLICT, record=record)
                return current_value
            if (
                OPERATION_PHASES.index((record["state"], record["effect_state"]))
                >= target_phase
            ):
                decided.update(outcome=RESUMED, record=record)
                return current_value
            advanced = _advanced(
                record,
                state=target[0],
                effect_state=target[1],
                updated_epoch=now,
                **(flags or {}),
            )
            decided.update(outcome=APPLIED, record=advanced)
            return _encode(advanced)

        self._table().mutate_value(operation_id, decide)
        return self._result(decided["outcome"], decided["record"])

    @staticmethod
    def _own(current_value, package_id, terminal_state):
        """The record this exact report may act on, or None for anything else."""
        record = decode_operation_record(current_value)
        if (
            record is None
            or record["package_id"] != package_id
            or record["terminal_state"] != terminal_state
        ):
            return None
        return record

    def _bookkeep(self, operation_id, package_id, terminal_state, decide):
        """Commit one bookkeeping phase of an operation that began its attempt.

        Nothing is ever recorded while the operation is still merely admitted:
        a record that provably touched nothing may not claim a counter or a
        notification, and the caller that skipped its durable attempt would
        otherwise hide exactly that.
        """
        self._validate(operation_id, package_id, terminal_state)
        now = int(self._clock())
        decided = {}

        def mutate(current_value):
            record = self._own(current_value, package_id, terminal_state)
            if record is None or record["effect_state"] == EFFECT_NOT_STARTED:
                decided.update(outcome=CONFLICT, record=record)
                return current_value
            updates = decide(record)
            if not updates:
                decided.update(outcome=RESUMED, record=record)
                return current_value
            advanced = _advanced(record, **updates, updated_epoch=now)
            decided.update(outcome=APPLIED, record=advanced)
            return _encode(advanced)

        self._table().mutate_value(operation_id, mutate)
        return self._result(decided["outcome"], decided["record"])

    def record_failure(
        self,
        operation_id,
        package_id,
        terminal_state,
        *,
        failed_target,
        failed_value,
        counter_targets,
    ):
        """Commit the failure row, its counters and this operation's marker at once.

        Every table of one Quasarr database lives in one file, so this is a
        single transaction: a counter can never answer for a row that was never
        written, a row can never be counted twice, and a retry that finds the
        marker knows both already happened. The caller owns the shape of the
        row and the names of the counters; this owns when they may be written.
        """
        self._validate(operation_id, package_id, terminal_state)
        if not isinstance(failed_value, str):
            raise TypeError("failed_value must be str")
        targets = (
            (TERMINAL_OPERATION_TABLE, operation_id),
            tuple(failed_target),
            *(tuple(target) for target in counter_targets),
        )
        now = int(self._clock())
        decided = {}

        def mutate(current_values):
            record = self._own(current_values[0], package_id, terminal_state)
            if record is None or record["effect_state"] == EFFECT_NOT_STARTED:
                decided.update(outcome=CONFLICT, record=record)
                return list(current_values)
            if record["failure_persisted"]:
                decided.update(outcome=RESUMED, record=record)
                return list(current_values)
            advanced = _advanced(record, failure_persisted=True, updated_epoch=now)
            decided.update(outcome=APPLIED, record=advanced)
            return [_encode(advanced), failed_value] + [
                _incremented(value) for value in current_values[2:]
            ]

        self._table().mutate_values(targets, mutate)
        return self._result(decided["outcome"], decided["record"])

    def mark_failure_persisted(self, operation_id, package_id, terminal_state):
        """Adopt a marked failure row an interrupted attempt already committed.

        Only the row is adopted, never a count: an older shape wrote its
        history without this bookkeeping, so whether it also counted the
        failure is exactly what cannot be shown, and inventing the counter
        would be the duplicate this whole ledger exists to prevent.
        """
        return self._bookkeep(
            operation_id,
            package_id,
            terminal_state,
            lambda record: (
                None if record["failure_persisted"] else {"failure_persisted": True}
            ),
        )

    def mark_notification(self, operation_id, package_id, terminal_state, target):
        """Move the notification phase forward, never backwards."""
        if target not in NOTIFICATION_STATES:
            raise ValueError("Unsupported notification phase")
        reached = NOTIFICATION_STATES.index(target)
        return self._bookkeep(
            operation_id,
            package_id,
            terminal_state,
            lambda record: (
                None
                if NOTIFICATION_STATES.index(record["notification_state"]) >= reached
                else {"notification_state": target}
            ),
        )

    def mark_effect_attempting(self, operation_id, package_id, terminal_state):
        """Record that this operation is about to touch the world.

        Persisted before the side effect, because nothing outside this record
        can tell an operation that crashed while merely admitted from one that
        crashed after its effect: every artifact a retry could read is also
        written by earlier lives of the same release, by the automatic download
        path and by hand.
        """
        return self._advance(
            operation_id,
            package_id,
            terminal_state,
            ("prepared", EFFECT_ATTEMPTING),
        )

    def mark_submitted(self, operation_id, package_id, terminal_state):
        """Record that the one irreversible step of this operation happened."""
        return self._advance(
            operation_id,
            package_id,
            terminal_state,
            ("submitted", EFFECT_APPLIED),
        )

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
            ("complete", EFFECT_APPLIED),
            flags={
                "package_removed": bool(package_removed),
                "package_terminal": bool(package_terminal),
            },
        )

    def reopen_completed(self, operation_id, package_id, terminal_state):
        """Retire a complete record whose package exists again, and open the next.

        The one transition that exists because a complete record is not
        evidence forever. A package ID is derived from the release, so one
        identity is reused by every life of that release, and a complete record
        describes only the life it closed:
        once that package is protected again, replaying the record would
        confirm a handoff the life in front of it never got, and it would do so
        for the whole retention window. Deciding that needs the protected
        package, which no mutation callback of this table may resolve, so the
        caller reads it and this owns the transition.

        The retirement is one transaction that replaces the row rather than a
        delete followed by an open, so a single identity can never hold two
        records or none, and the caller holds `operation_lock` around both the
        decision and the operation that follows it, so no second request can
        act on the record being replaced. The new record opens strictly after
        the retired one, so two lives of one package can never share the
        `operation_evidence` that separates the artifacts they leave behind,
        even when both open inside the same second of the clock.

        Only a complete record is retired. Any other phase may still own an
        unfinished side effect, and replacing it would authorize a second one.
        """
        self._validate(operation_id, package_id, terminal_state)
        now = int(self._clock())
        decided = {}

        def decide(current_value):
            record = self._own(current_value, package_id, terminal_state)
            if record is None:
                decided.update(outcome=CONFLICT, record=None)
                return current_value
            if record["state"] != "complete":
                decided.update(outcome=RESUMED, record=record)
                return current_value
            opened = _opened(
                package_id,
                terminal_state,
                max(now, record["created_epoch"] + 1),
            )
            decided.update(outcome=OPENED, record=opened)
            return _encode(opened)

        self._table().mutate_value(operation_id, decide)
        return self._result(decided["outcome"], decided["record"])

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
