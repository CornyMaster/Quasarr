# -*- coding: utf-8 -*-

import hashlib
import json
import os
import tempfile
import threading
import unittest
from unittest import mock

from quasarr.providers import shared_state as provider_shared_state
from quasarr.providers.terminal_operations import (
    COMPLETED_OPERATION_RETENTION_SECONDS,
    MAXIMUM_TERMINAL_OPERATIONS,
    OPERATION_RECORD_KEYS,
    OPERATION_STATES,
    TERMINAL_OPERATION_DOMAIN,
    TERMINAL_OPERATION_TABLE,
    TERMINAL_STATES,
    TerminalOperationService,
    decode_operation_record,
    terminal_operation_id,
)
from quasarr.storage.sqlite_database import DataBase

NOW = 1_700_000_000
DAY = 24 * 60 * 60


def package(index):
    return f"Quasarr_movies_{index:032x}"


class FakeClock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


class GuardedTable:
    """One in-memory table that fails the test on storage access from a callback."""

    def __init__(self, rows=None):
        self.rows = dict(rows or {})
        self.reads = 0
        self.enumerations = 0
        self.writes = 0
        self.deletes = 0
        self.in_callback = False
        self.after_enumeration = None
        self.enumeration_hook = None

    def _guard(self, operation):
        if self.in_callback:
            raise AssertionError(f"{operation} was resolved inside a mutation callback")

    def retrieve(self, key):
        self._guard("retrieve")
        self.reads += 1
        return self.rows.get(key)

    def retrieve_all_titles(self):
        self._guard("retrieve_all_titles")
        self.enumerations += 1
        items = [[key, value] for key, value in sorted(self.rows.items())]
        hook, self.after_enumeration = self.after_enumeration, None
        if hook is not None:
            hook()
        if self.enumeration_hook is not None:
            self.enumeration_hook(self.enumerations)
        return items or None

    def store(self, key, value):
        self._guard("store")
        self.writes += 1
        self.rows[key] = value
        return True

    def update_store(self, key, value):
        return self.store(key, value)

    def mutate_value(self, key, mutator):
        current = self.rows.get(key)
        self.in_callback = True
        try:
            value = mutator(current)
        finally:
            self.in_callback = False
        if value != current:
            self.writes += 1
        if value is None:
            self.rows.pop(key, None)
        else:
            self.rows[key] = value
        return value

    def delete_exact(self, key, value):
        self._guard("delete_exact")
        if self.rows.get(key) != value:
            return False
        self.rows.pop(key, None)
        self.deletes += 1
        return True

    def delete(self, key):
        self._guard("delete")
        self.rows.pop(key, None)
        self.deletes += 1
        return True


class GuardedSharedState:
    def __init__(self, rows=None):
        self.tables = {TERMINAL_OPERATION_TABLE: GuardedTable(rows)}
        self.values = {}

    def get_db(self, table):
        if table not in self.tables:
            self.tables[table] = GuardedTable()
        return self.tables[table]

    @property
    def operations(self):
        return self.tables[TERMINAL_OPERATION_TABLE]

    def update(self, key, value):
        self.values[key] = value


def record(
    package_id,
    terminal_state="downloaded",
    state="prepared",
    created=NOW,
    updated=NOW,
    package_removed=False,
    package_terminal=False,
):
    return json.dumps(
        {
            "state": state,
            "terminal_state": terminal_state,
            "package_id": package_id,
            "created_epoch": created,
            "updated_epoch": updated,
            "package_removed": package_removed,
            "package_terminal": package_terminal,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


class TerminalOperationIdentityTests(unittest.TestCase):
    def test_the_operation_id_is_the_documented_stable_digest(self):
        package_id = package(3)

        operation_id = terminal_operation_id(package_id)

        self.assertEqual(
            hashlib.sha256(
                f"{TERMINAL_OPERATION_DOMAIN}\n{package_id}".encode("utf-8")
            ).hexdigest(),
            operation_id,
        )
        self.assertRegex(operation_id, r"^[0-9a-f]{64}$")
        self.assertNotEqual(operation_id, terminal_operation_id(package(4)))

    def test_the_cohort_protocol_re_exports_exactly_this_digest(self):
        from quasarr.api.sponsors_helper import cohort_protocol

        self.assertIs(cohort_protocol.terminal_operation_id, terminal_operation_id)

    def test_the_documented_constants_are_the_approved_ones(self):
        self.assertEqual(4096, MAXIMUM_TERMINAL_OPERATIONS)
        self.assertEqual(7 * DAY, COMPLETED_OPERATION_RETENTION_SECONDS)
        self.assertEqual(
            "sponsors_helper_terminal_operations", TERMINAL_OPERATION_TABLE
        )
        self.assertEqual({"downloaded", "failed", "disabled"}, set(TERMINAL_STATES))
        self.assertEqual({"prepared", "submitted", "complete"}, set(OPERATION_STATES))
        self.assertEqual(
            {
                "state",
                "terminal_state",
                "package_id",
                "created_epoch",
                "updated_epoch",
                "package_removed",
                "package_terminal",
            },
            set(OPERATION_RECORD_KEYS),
        )


class OperationRecordCodecTests(unittest.TestCase):
    def test_a_valid_record_round_trips(self):
        decoded = decode_operation_record(record(package(1)))

        self.assertEqual(set(OPERATION_RECORD_KEYS), set(decoded))
        self.assertEqual("prepared", decoded["state"])
        self.assertEqual(package(1), decoded["package_id"])

    def test_every_unusable_row_decodes_to_none_instead_of_raising(self):
        deep = "[" * 100_000 + "]" * 100_000
        with self.assertRaises(RecursionError):
            json.loads(deep)
        digits = '{"created_epoch": ' + "9" * 5000 + "}"
        with self.assertRaises(ValueError):
            json.loads(digits)

        base = json.loads(record(package(1)))
        cases = [
            None,
            "",
            "{not json",
            "null",
            "[1, 2, 3]",
            '"prepared"',
            deep,
            digits,
        ]
        for key in sorted(OPERATION_RECORD_KEYS):
            missing = dict(base)
            missing.pop(key)
            cases.append(json.dumps(missing))
        cases.append(json.dumps({**base, "links": ["https://filecrypt.invalid/1"]}))
        cases.append(json.dumps({**base, "state": "started"}))
        cases.append(json.dumps({**base, "terminal_state": "downloading"}))
        cases.append(json.dumps({**base, "package_id": ""}))
        cases.append(json.dumps({**base, "package_id": 7}))
        cases.append(json.dumps({**base, "created_epoch": -1}))
        cases.append(json.dumps({**base, "created_epoch": True}))
        cases.append(json.dumps({**base, "updated_epoch": 1.5}))
        cases.append(json.dumps({**base, "package_removed": "yes"}))
        cases.append(json.dumps({**base, "package_terminal": 1}))

        for value in cases:
            with self.subTest(value=str(value)[:60]):
                self.assertIsNone(decode_operation_record(value))


class TerminalOperationServiceTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.state = GuardedSharedState()
        self.service = TerminalOperationService(self.state, clock=self.clock)

    def rows(self):
        return dict(self.state.operations.rows)

    def begin(self, index=1, terminal_state="downloaded"):
        package_id = package(index)
        return self.service.begin(
            terminal_operation_id(package_id), package_id, terminal_state
        )

    # --- validation -----------------------------------------------------

    def test_an_operation_id_that_is_not_the_package_digest_is_refused(self):
        package_id = package(1)
        cases = (
            terminal_operation_id(package(2)),
            terminal_operation_id(package_id).upper(),
            terminal_operation_id(package_id)[:63],
            terminal_operation_id(package_id) + "0",
            "z" * 64,
            "",
            None,
            7,
        )

        for operation_id in cases:
            with self.subTest(operation_id=str(operation_id)[:20]):
                with self.assertRaises(ValueError):
                    self.service.begin(operation_id, package_id, "downloaded")

        self.assertEqual({}, self.rows())

    def test_an_unknown_terminal_state_is_refused_before_any_write(self):
        package_id = package(1)

        for terminal_state in ("solved", "", None, "DOWNLOADED"):
            with self.subTest(terminal_state=terminal_state):
                with self.assertRaises(ValueError):
                    self.service.begin(
                        terminal_operation_id(package_id), package_id, terminal_state
                    )

        self.assertEqual({}, self.rows())

    # --- state machine --------------------------------------------------

    def test_begin_opens_a_secret_free_prepared_record(self):
        result = self.begin()

        self.assertEqual("opened", result["outcome"])
        self.assertEqual("prepared", result["record"]["state"])
        stored = json.loads(self.rows()[terminal_operation_id(package(1))])
        self.assertEqual(
            {
                "state": "prepared",
                "terminal_state": "downloaded",
                "package_id": package(1),
                "created_epoch": NOW,
                "updated_epoch": NOW,
                "package_removed": False,
                "package_terminal": False,
            },
            stored,
        )

    def test_repeating_begin_resumes_without_writing(self):
        self.begin()
        before = self.rows()
        writes = self.state.operations.writes

        result = self.begin()

        self.assertEqual("resumed", result["outcome"])
        self.assertEqual("prepared", result["record"]["state"])
        self.assertEqual(before, self.rows())
        self.assertEqual(writes, self.state.operations.writes)

    def test_a_conflicting_terminal_state_is_refused_without_writes(self):
        self.begin()
        before = self.rows()

        conflict = self.service.begin(
            terminal_operation_id(package(1)), package(1), "failed"
        )

        self.assertEqual("conflict", conflict["outcome"])
        self.assertEqual("prepared", conflict["record"]["state"])
        self.assertEqual(before, self.rows())

    def test_a_row_naming_another_package_is_refused_without_writes(self):
        operation_id = terminal_operation_id(package(1))
        self.state.operations.rows[operation_id] = record(package(2))
        before = self.rows()

        conflict = self.service.begin(operation_id, package(1), "downloaded")

        self.assertEqual("conflict", conflict["outcome"])
        self.assertEqual(before, self.rows())

    def test_the_prepared_submitted_complete_progression_is_exactly_once(self):
        package_id = package(1)
        operation_id = terminal_operation_id(package_id)
        self.begin()

        submitted = self.service.mark_submitted(operation_id, package_id, "downloaded")
        self.assertEqual("applied", submitted["outcome"])
        self.assertEqual("submitted", submitted["record"]["state"])

        repeated = self.service.mark_submitted(operation_id, package_id, "downloaded")
        self.assertEqual("resumed", repeated["outcome"])
        self.assertEqual("submitted", repeated["record"]["state"])

        completed = self.service.mark_complete(
            operation_id,
            package_id,
            "downloaded",
            package_removed=True,
            package_terminal=True,
        )
        self.assertEqual("applied", completed["outcome"])
        self.assertEqual("complete", completed["record"]["state"])
        self.assertTrue(completed["record"]["package_removed"])

        replay = self.service.mark_complete(
            operation_id,
            package_id,
            "downloaded",
            package_removed=False,
            package_terminal=False,
        )
        self.assertEqual("resumed", replay["outcome"])
        self.assertTrue(replay["record"]["package_removed"])
        self.assertTrue(replay["record"]["package_terminal"])

    def test_a_terminal_transition_updates_only_the_update_timestamp(self):
        package_id = package(1)
        operation_id = terminal_operation_id(package_id)
        self.begin()
        self.clock.now = NOW + 30

        self.service.mark_submitted(operation_id, package_id, "downloaded")

        stored = json.loads(self.rows()[operation_id])
        self.assertEqual(NOW, stored["created_epoch"])
        self.assertEqual(NOW + 30, stored["updated_epoch"])

    def test_advancing_a_missing_or_foreign_operation_conflicts(self):
        package_id = package(1)
        operation_id = terminal_operation_id(package_id)

        missing = self.service.mark_submitted(operation_id, package_id, "downloaded")
        self.assertEqual("conflict", missing["outcome"])
        self.assertIsNone(missing["record"])
        self.assertEqual({}, self.rows())

        self.begin()
        before = self.rows()
        foreign = self.service.mark_complete(
            operation_id,
            package_id,
            "failed",
            package_removed=True,
            package_terminal=True,
        )
        self.assertEqual("conflict", foreign["outcome"])
        self.assertEqual(before, self.rows())

    def test_an_unreadable_row_is_replaced_rather_than_resumed(self):
        package_id = package(1)
        operation_id = terminal_operation_id(package_id)
        self.state.operations.rows[operation_id] = "{not json"

        result = self.service.begin(operation_id, package_id, "downloaded")

        self.assertEqual("opened", result["outcome"])
        self.assertEqual("prepared", result["record"]["state"])

    def test_snapshot_is_a_pure_read(self):
        package_id = package(1)
        operation_id = terminal_operation_id(package_id)
        self.assertIsNone(self.service.snapshot(operation_id))
        self.begin()
        writes = self.state.operations.writes

        snapshot = self.service.snapshot(operation_id)

        self.assertEqual("prepared", snapshot["state"])
        self.assertEqual(writes, self.state.operations.writes)

    # --- capacity and pruning -------------------------------------------

    def test_capacity_fails_closed_before_any_terminal_side_effect(self):
        for index in range(MAXIMUM_TERMINAL_OPERATIONS):
            self.state.operations.rows[terminal_operation_id(package(index))] = record(
                package(index)
            )
        before = self.rows()

        result = self.begin(index=MAXIMUM_TERMINAL_OPERATIONS + 1)

        self.assertEqual("capacity", result["outcome"])
        self.assertIsNone(result["record"])
        self.assertEqual(before, self.rows())

    def test_an_existing_operation_still_resumes_at_capacity(self):
        for index in range(MAXIMUM_TERMINAL_OPERATIONS):
            self.state.operations.rows[terminal_operation_id(package(index))] = record(
                package(index)
            )
        enumerations = self.state.operations.enumerations

        result = self.begin(index=0)

        self.assertEqual("resumed", result["outcome"])
        self.assertEqual("prepared", result["record"]["state"])
        self.assertEqual(enumerations, self.state.operations.enumerations)

    def test_two_new_operations_cannot_both_consume_the_last_slot(self):
        first_enumerated = threading.Event()
        second_enumerated = threading.Event()
        release_first = threading.Event()

        def interleave(enumeration):
            if enumeration == 1:
                first_enumerated.set()
                release_first.wait(timeout=1)
            elif enumeration == 2:
                second_enumerated.set()
                release_first.set()

        self.state.operations.enumeration_hook = interleave
        results = []

        def open_operation(index):
            results.append(self.begin(index=index))

        with mock.patch(
            "quasarr.providers.terminal_operations.MAXIMUM_TERMINAL_OPERATIONS", 1
        ):
            first = threading.Thread(target=open_operation, args=(1,))
            second = threading.Thread(target=open_operation, args=(2,))
            first.start()
            self.assertTrue(first_enumerated.wait(timeout=1))
            second.start()
            second_enumerated.wait(timeout=0.1)
            release_first.set()
            first.join(timeout=1)
            second.join(timeout=1)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(["capacity", "opened"], sorted(r["outcome"] for r in results))
        self.assertEqual(1, len(self.rows()))

    def test_active_operations_are_never_evicted_to_make_room(self):
        for index in range(MAXIMUM_TERMINAL_OPERATIONS):
            state = "prepared" if index % 2 else "submitted"
            self.state.operations.rows[terminal_operation_id(package(index))] = record(
                package(index), state=state, updated=NOW - 400 * DAY
            )
        before = self.rows()

        result = self.begin(index=MAXIMUM_TERMINAL_OPERATIONS + 1)

        self.assertEqual("capacity", result["outcome"])
        self.assertEqual(before, self.rows())

    def test_unexpired_complete_rows_are_never_evicted_to_make_room(self):
        for index in range(MAXIMUM_TERMINAL_OPERATIONS):
            self.state.operations.rows[terminal_operation_id(package(index))] = record(
                package(index),
                state="complete",
                updated=NOW - COMPLETED_OPERATION_RETENTION_SECONDS + 1,
                package_removed=True,
                package_terminal=True,
            )
        before = self.rows()

        result = self.begin(index=MAXIMUM_TERMINAL_OPERATIONS + 1)

        self.assertEqual("capacity", result["outcome"])
        self.assertEqual(before, self.rows())

    def test_expired_complete_rows_are_pruned_oldest_first_to_free_capacity(self):
        expired = 3
        for index in range(MAXIMUM_TERMINAL_OPERATIONS):
            if index < expired:
                self.state.operations.rows[terminal_operation_id(package(index))] = (
                    record(
                        package(index),
                        state="complete",
                        updated=NOW - COMPLETED_OPERATION_RETENTION_SECONDS - index,
                        package_removed=True,
                        package_terminal=True,
                    )
                )
            else:
                self.state.operations.rows[terminal_operation_id(package(index))] = (
                    record(package(index))
                )

        result = self.begin(index=MAXIMUM_TERMINAL_OPERATIONS + 1)

        self.assertEqual("opened", result["outcome"])
        for index in range(expired):
            self.assertNotIn(terminal_operation_id(package(index)), self.rows())
        self.assertIn(
            terminal_operation_id(package(MAXIMUM_TERMINAL_OPERATIONS + 1)), self.rows()
        )

    def test_prune_completed_uses_the_exact_retention_boundary(self):
        cases = {
            package(1): NOW - COMPLETED_OPERATION_RETENTION_SECONDS - 1,
            package(2): NOW - COMPLETED_OPERATION_RETENTION_SECONDS,
            package(3): NOW - COMPLETED_OPERATION_RETENTION_SECONDS + 1,
        }
        for package_id, updated in cases.items():
            self.state.operations.rows[terminal_operation_id(package_id)] = record(
                package_id,
                state="complete",
                updated=updated,
                package_removed=True,
                package_terminal=True,
            )

        pruned = self.service.prune_completed()

        self.assertEqual(2, pruned)
        self.assertEqual(
            {terminal_operation_id(package(3))},
            set(self.rows()),
        )

    def test_pruning_compares_the_exact_value_it_enumerated(self):
        package_id = package(1)
        operation_id = terminal_operation_id(package_id)
        self.state.operations.rows[operation_id] = record(
            package_id,
            state="complete",
            updated=NOW - COMPLETED_OPERATION_RETENTION_SECONDS - 1,
            package_removed=True,
            package_terminal=True,
        )
        replacement = record(package_id, state="prepared", created=NOW, updated=NOW)

        def replace():
            self.state.operations.rows[operation_id] = replacement

        self.state.operations.after_enumeration = replace

        pruned = self.service.prune_completed()

        self.assertEqual(0, pruned)
        self.assertEqual(replacement, self.rows()[operation_id])

    def test_counts_by_state_are_fixed_cardinality_and_identifier_free(self):
        states = ("prepared", "prepared", "submitted", "complete")
        for index, state in enumerate(states):
            self.state.operations.rows[terminal_operation_id(package(index))] = record(
                package(index), state=state
            )
        self.state.operations.rows[terminal_operation_id(package(99))] = "{not json"

        counts = self.service.count_by_state()

        self.assertEqual(
            {"prepared": 2, "submitted": 1, "complete": 1},
            counts,
        )


class RealDatabaseTerminalOperationTests(unittest.TestCase):
    """Durability, capacity and races against a real SQLite file."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.dbfile = os.path.join(self.tmpdir.name, "Quasarr.db")
        self.original_values = provider_shared_state.values
        self.original_lock = provider_shared_state.lock
        provider_shared_state.values = {"dbfile": self.dbfile}
        provider_shared_state.lock = None
        self.addCleanup(self.restore)
        self.clock = FakeClock()
        self.databases = []

    def restore(self):
        for database in self.databases:
            database._conn.close()
        provider_shared_state.values = self.original_values
        provider_shared_state.lock = self.original_lock

    def shared_state(self):
        databases = {}
        collected = self.databases

        class RealSharedState:
            values = {"dbfile": self.dbfile}

            @staticmethod
            def get_db(table):
                if table not in databases:
                    database = DataBase(table)
                    databases[table] = database
                    collected.append(database)
                return databases[table]

        return RealSharedState()

    def test_a_submitted_operation_survives_a_restart(self):
        package_id = package(1)
        operation_id = terminal_operation_id(package_id)
        first = TerminalOperationService(self.shared_state(), clock=self.clock)
        first.begin(operation_id, package_id, "downloaded")
        first.mark_submitted(operation_id, package_id, "downloaded")

        restarted = TerminalOperationService(self.shared_state(), clock=self.clock)
        resumed = restarted.begin(operation_id, package_id, "downloaded")

        self.assertEqual("resumed", resumed["outcome"])
        self.assertEqual("submitted", resumed["record"]["state"])

    def test_a_concurrent_replacement_survives_pruning_on_disk(self):
        package_id = package(1)
        operation_id = terminal_operation_id(package_id)
        state = self.shared_state()
        service = TerminalOperationService(state, clock=self.clock)
        table = state.get_db(TERMINAL_OPERATION_TABLE)
        table.update_store(
            operation_id,
            record(
                package_id,
                state="complete",
                updated=NOW - COMPLETED_OPERATION_RETENTION_SECONDS - 1,
                package_removed=True,
                package_terminal=True,
            ),
        )
        replacement = record(package_id, state="prepared")
        original_enumerate = DataBase.retrieve_all_titles

        def enumerate_then_replace(database):
            rows = original_enumerate(database)
            competitor = DataBase(TERMINAL_OPERATION_TABLE)
            self.databases.append(competitor)
            competitor.update_store(operation_id, replacement)
            return rows

        with mock.patch.object(DataBase, "retrieve_all_titles", enumerate_then_replace):
            pruned = service.prune_completed()

        self.assertEqual(0, pruned)
        self.assertEqual(replacement, table.retrieve(operation_id))

    def test_capacity_is_proven_against_the_rows_on_disk(self):
        state = self.shared_state()
        service = TerminalOperationService(state, clock=self.clock)
        table = state.get_db(TERMINAL_OPERATION_TABLE)
        with mock.patch(
            "quasarr.providers.terminal_operations.MAXIMUM_TERMINAL_OPERATIONS", 2
        ):
            for index in range(2):
                package_id = package(index)
                service.begin(
                    terminal_operation_id(package_id), package_id, "downloaded"
                )
            package_id = package(9)
            rejected = service.begin(
                terminal_operation_id(package_id), package_id, "downloaded"
            )

        self.assertEqual("capacity", rejected["outcome"])
        self.assertIsNone(table.retrieve(terminal_operation_id(package(9))))
        self.assertEqual(2, len(table.retrieve_all_titles()))


if __name__ == "__main__":
    unittest.main()
