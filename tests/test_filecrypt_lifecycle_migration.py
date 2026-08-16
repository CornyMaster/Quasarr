# -*- coding: utf-8 -*-

import json
import unittest

from quasarr.providers.crypter_cooldowns import (
    MINIMUM_COOLDOWN_HOURS,
    _encode_record,
)
from quasarr.providers.crypter_sweeps import (
    SWEEP_SCHEMA_VERSION,
    SWEEP_WINDOW_SECONDS,
    encode_decision_record,
)
from quasarr.providers.filecrypt_lifecycle import (
    FILECRYPT_LIFECYCLE_MIGRATIONS_TABLE,
    FILECRYPT_LINK_STATES_TABLE,
    FILECRYPT_MIGRATION_KEY,
    FILECRYPT_SWEEP_KEY,
    FILECRYPT_SWEEP_STATE_TABLE,
    decode_link_state,
    decode_migration_marker,
    decode_sweep_header,
)
from quasarr.providers.filecrypt_lifecycle_service import (
    FILECRYPT_CRYPTER,
    FilecryptLifecycleService,
)
from tests.test_crypter_sweep_records import cohort_cooldown_record
from tests.test_filecrypt_lifecycle_service import (
    AtomicSharedState,
    FakeClock,
    SequentialIds,
    fp,
    pkg,
    url,
)

NOW = 1_700_000_000
COOLDOWN_SECS = MINIMUM_COOLDOWN_HOURS * 3600
SWEEP_ID = "a" * 32


def legacy_cooldown_row(observations, retry_after=NOW + COOLDOWN_SECS):
    return _encode_record(
        {
            "state": "cooldown",
            "reason_code": "ip_block_suspected",
            "first_seen_epoch": NOW - 100,
            "last_seen_epoch": NOW - 10,
            "retry_after_epoch": retry_after,
            "observations": observations,
        }
    )


def legacy_observing_row(observations):
    return _encode_record(
        {
            "state": "observing",
            "reason_code": "ip_block_suspected",
            "first_seen_epoch": NOW - 100,
            "last_seen_epoch": NOW - 10,
            "retry_after_epoch": 0,
            "observations": observations,
        }
    )


def v2_cohort_cooldown(sweep_id, retry_after=NOW + COOLDOWN_SECS):
    """Build a valid v2 cohort cooldown raw string using real codec fixtures."""
    return encode_decision_record(
        cohort_cooldown_record(
            sweep_id=sweep_id,
            opened_epoch=NOW - SWEEP_WINDOW_SECONDS,
            deadline_epoch=NOW,
            retry_after_epoch=retry_after,
        )
    )


def marked_legacy_cooldown_decision(retry_after=NOW + COOLDOWN_SECS):
    return encode_decision_record(
        {
            "schema_version": SWEEP_SCHEMA_VERSION,
            "state": "cooldown",
            "reason_code": "ip_block_suspected",
            "legacy_cooldown": True,
            "retry_after_epoch": retry_after,
            "legacy_evidence_count": 2,
        }
    )


def v2_defer(sweep_id, fingerprints, since=NOW - 50, retry_after=NOW + COOLDOWN_SECS):
    return {
        "schema_version": SWEEP_SCHEMA_VERSION,
        "crypter": "filecrypt",
        "reason_code": "ip_block_suspected",
        "since_epoch": since,
        "retry_after_epoch": retry_after,
        "probe_requested": False,
        "observation_holds": 0,
        "sweep_id": sweep_id,
        "link_fingerprints": sorted(fingerprints),
    }


def legacy_defer(since=NOW - 50, retry_after=NOW + COOLDOWN_SECS):
    return {
        "crypter": "filecrypt",
        "reason_code": "ip_block_suspected",
        "since_epoch": since,
        "retry_after_epoch": retry_after,
        "probe_requested": False,
        "observation_holds": 0,
    }


def package_with_defer(n, defer_dict):
    links = [[url(n), FILECRYPT_CRYPTER]]
    return json.dumps(
        {"title": "T", "password": "", "links": links, "deferred": defer_dict},
        separators=(",", ":"),
        sort_keys=True,
    )


def package_no_defer(n):
    links = [[url(n), FILECRYPT_CRYPTER]]
    return json.dumps(
        {"title": "T", "password": "", "links": links},
        separators=(",", ":"),
        sort_keys=True,
    )


class MigrationTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(NOW)
        self.ids = SequentialIds()
        self.state = AtomicSharedState()

    def service(self):
        return FilecryptLifecycleService(
            self.state, clock=self.clock, identifier_factory=self.ids
        )

    def set_old_decision(self, raw):
        self.state.get_db("crypter_cooldowns").store("filecrypt", raw)

    def old_decision_raw(self):
        return self.state.get_db("crypter_cooldowns").retrieve("filecrypt")

    def marker_raw(self):
        return self.state.get_db(FILECRYPT_LIFECYCLE_MIGRATIONS_TABLE).retrieve(
            FILECRYPT_MIGRATION_KEY
        )

    def header(self):
        raw = self.state.get_db(FILECRYPT_SWEEP_STATE_TABLE).retrieve(
            FILECRYPT_SWEEP_KEY
        )
        return None if raw is None else decode_sweep_header(raw)

    def link_state(self, fingerprint):
        raw = self.state.get_db(FILECRYPT_LINK_STATES_TABLE).retrieve(fingerprint)
        return None if raw is None else decode_link_state(raw)

    def protected_db(self):
        return self.state.get_db("protected")


class TestMigrateLegacyStateMatrix(MigrationTestCase):
    """Task 6A budget method 1: table-driven state migration cases."""

    def test_migrate_legacy_state_matrix(self):
        sweep_id = "a" * 32
        cases = [
            # (label, old_decision_raw, protected_rows, expect_status, checks)
            (
                "no_state",
                None,
                [],
                "complete",
                lambda r: (
                    self.assertEqual(r["held"], 0),
                    self.assertEqual(r["global_cooldown"], False),
                    self.assertIsNotNone(self.marker_raw()),
                    self.assertIsNone(self.old_decision_raw()),
                ),
            ),
            (
                "observing_only",
                legacy_observing_row(
                    [
                        {
                            "package_id": pkg(1),
                            "link_fingerprint": fp(1),
                            "seen_at_epoch": NOW - 5,
                        }
                    ]
                ),
                [],
                "complete",
                lambda r: (
                    self.assertEqual(r["held"], 0),
                    self.assertEqual(r["global_cooldown"], False),
                    self.assertIsNotNone(self.marker_raw()),
                    # observing row is not a valid cooldown, so it is removed
                    self.assertIsNone(self.old_decision_raw()),
                ),
            ),
            (
                "active_v1_cooldown_with_observation_and_legacy_defer",
                legacy_cooldown_row(
                    [
                        {
                            "package_id": pkg(1),
                            "link_fingerprint": fp(1),
                            "seen_at_epoch": NOW - 5,
                        }
                    ],
                    retry_after=NOW + 5000,
                ),
                [
                    [
                        pkg(1),
                        package_with_defer(
                            1, legacy_defer(since=NOW - 80, retry_after=NOW + 5000)
                        ),
                    ]
                ],
                "complete",
                lambda r: (
                    self.assertEqual(r["held"], 1),
                    self.assertEqual(r["global_cooldown"], True),
                    self.assertIsNotNone(self.header()),
                    self.assertEqual(self.header()["state"], "cooldown"),
                    self.assertIsNotNone(self.link_state(fp(1))),
                    self.assertEqual(self.link_state(fp(1))["state"], "held"),
                    self.assertEqual(
                        self.link_state(fp(1))["first_blocked_epoch"], NOW - 80
                    ),
                    self.assertEqual(
                        self.link_state(fp(1))["retry_after_epoch"], NOW + 5000
                    ),
                    # package deferred cleaned
                    self.assertIsNone(
                        json.loads(self.protected_db().retrieve(pkg(1))).get("deferred")
                    ),
                    self.assertIsNone(self.old_decision_raw()),
                ),
            ),
            (
                "active_v2_cohort_cooldown_with_v2_defer",
                v2_cohort_cooldown(sweep_id, retry_after=NOW + 6000),
                [
                    [
                        pkg(2),
                        package_with_defer(
                            2,
                            v2_defer(
                                sweep_id,
                                [fp(2)],
                                since=NOW - 30,
                                retry_after=NOW + 6000,
                            ),
                        ),
                    ]
                ],
                "complete",
                lambda r: (
                    self.assertEqual(r["held"], 1),
                    self.assertEqual(r["global_cooldown"], True),
                    self.assertIsNotNone(self.header()),
                    self.assertEqual(self.header()["state"], "cooldown"),
                    self.assertEqual(self.header()["generation_id"], sweep_id),
                    self.assertEqual(self.header()["retry_after_epoch"], NOW + 6000),
                    self.assertIsNotNone(self.link_state(fp(2))),
                    self.assertEqual(self.link_state(fp(2))["state"], "held"),
                    self.assertEqual(
                        self.link_state(fp(2))["first_blocked_epoch"], NOW - 30
                    ),
                    self.assertEqual(
                        self.link_state(fp(2))["retry_after_epoch"], NOW + 6000
                    ),
                    self.assertIsNone(self.old_decision_raw()),
                ),
            ),
            (
                "expired_v1_cooldown_no_migration_of_holds",
                legacy_cooldown_row(
                    [
                        {
                            "package_id": pkg(3),
                            "link_fingerprint": fp(3),
                            "seen_at_epoch": NOW - 5,
                        }
                    ],
                    retry_after=NOW - 1,  # expired
                ),
                [
                    [
                        pkg(3),
                        package_with_defer(
                            3, legacy_defer(since=NOW - 80, retry_after=NOW - 1)
                        ),
                    ]
                ],
                "complete",
                lambda r: (
                    self.assertEqual(r["held"], 0),
                    self.assertEqual(r["global_cooldown"], False),
                    self.assertIsNone(self.header()),
                    self.assertIsNone(self.link_state(fp(3))),
                    # expired defer still gets cleaned (represented as proven-expired)
                    self.assertIsNone(
                        json.loads(self.protected_db().retrieve(pkg(3))).get("deferred")
                    ),
                    self.assertIsNone(self.old_decision_raw()),
                ),
            ),
            (
                "v2_defer_fingerprints_multiple",
                v2_cohort_cooldown(sweep_id, retry_after=NOW + 7000),
                [
                    [
                        pkg(4),
                        package_with_defer(
                            4,
                            v2_defer(
                                sweep_id,
                                [fp(4), fp(5)],
                                since=NOW - 20,
                                retry_after=NOW + 7000,
                            ),
                        ),
                    ]
                ],
                "complete",
                lambda r: (
                    self.assertEqual(r["held"], 2),
                    self.assertIsNotNone(self.link_state(fp(4))),
                    self.assertIsNotNone(self.link_state(fp(5))),
                    self.assertEqual(
                        self.link_state(fp(4))["retry_after_epoch"], NOW + 7000
                    ),
                    self.assertEqual(
                        self.link_state(fp(5))["retry_after_epoch"], NOW + 7000
                    ),
                ),
            ),
            (
                "generationless_defer_unmatched_to_observation",
                legacy_cooldown_row(
                    [
                        {
                            "package_id": pkg(6),
                            "link_fingerprint": fp(6),
                            "seen_at_epoch": NOW - 5,
                        }
                    ],
                    retry_after=NOW + 3000,
                ),
                [
                    # pkg(7) has a legacy defer but no observation with that package_id
                    [
                        pkg(7),
                        package_with_defer(
                            7, legacy_defer(since=NOW - 50, retry_after=NOW + 3000)
                        ),
                    ]
                ],
                "complete",
                lambda r: (
                    self.assertEqual(r["held"], 0),
                    self.assertEqual(r["global_cooldown"], True),
                    # unmatched legacy defer is preserved byte-identical
                    self.assertIsNotNone(
                        json.loads(self.protected_db().retrieve(pkg(7))).get("deferred")
                    ),
                ),
            ),
            (
                "generationless_defer_matched_to_observation",
                legacy_cooldown_row(
                    [
                        {
                            "package_id": pkg(8),
                            "link_fingerprint": fp(8),
                            "seen_at_epoch": NOW - 5,
                        }
                    ],
                    retry_after=NOW + 4000,
                ),
                [
                    [
                        pkg(8),
                        package_with_defer(
                            8, legacy_defer(since=NOW - 60, retry_after=NOW + 4000)
                        ),
                    ]
                ],
                "complete",
                lambda r: (
                    self.assertEqual(r["held"], 1),
                    self.assertIsNotNone(self.link_state(fp(8))),
                    self.assertEqual(
                        self.link_state(fp(8))["first_blocked_epoch"], NOW - 60
                    ),
                    self.assertEqual(
                        self.link_state(fp(8))["retry_after_epoch"], NOW + 4000
                    ),
                    # represented -> cleaned
                    self.assertIsNone(
                        json.loads(self.protected_db().retrieve(pkg(8))).get("deferred")
                    ),
                ),
            ),
            (
                "marked_legacy_cooldown_fresh_header",
                marked_legacy_cooldown_decision(retry_after=NOW + 9000),
                [],
                "complete",
                lambda r: (
                    self.assertEqual(r["global_cooldown"], True),
                    self.assertIsNotNone(self.header()),
                    self.assertEqual(self.header()["state"], "cooldown"),
                    self.assertEqual(self.header()["retry_after_epoch"], NOW + 9000),
                    self.assertEqual(
                        self.header()["sweep_deadline_epoch"], max(1, NOW)
                    ),
                    self.assertIsNone(self.old_decision_raw()),
                ),
            ),
        ]
        for label, old_raw, protected, expect_status, checks in cases:
            with self.subTest(label):
                # Reset state per case
                self.setUp()
                if old_raw is not None:
                    self.set_old_decision(old_raw)
                for row in protected:
                    self.protected_db().store(row[0], row[1])
                svc = self.service()
                result = svc.migrate_legacy(protected_rows=protected or None)
                self.assertEqual(result["status"], expect_status, f"case={label}")
                checks(result)


class TestMigrationFailClosedAndAtomicMatrix(MigrationTestCase):
    """Task 6A budget method 2: fail-closed and atomic behavior."""

    def test_migration_fail_closed_and_atomic_matrix(self):
        sweep_id = "b" * 32
        cases = [
            (
                "malformed_marker_non_none",
                lambda: self.state.get_db(FILECRYPT_LIFECYCLE_MIGRATIONS_TABLE).store(
                    FILECRYPT_MIGRATION_KEY, "not valid json{"
                ),
                None,
                [],
                "unavailable",
            ),
            (
                "malformed_existing_lifecycle_header",
                lambda: self.state.get_db(FILECRYPT_SWEEP_STATE_TABLE).store(
                    FILECRYPT_SWEEP_KEY, "corrupt"
                ),
                legacy_cooldown_row([], retry_after=NOW + 5000),
                [],
                "unavailable",
            ),
            (
                "malformed_existing_link_state_for_target_fp",
                lambda: (
                    self.set_old_decision(
                        v2_cohort_cooldown(sweep_id, retry_after=NOW + 5000)
                    ),
                    self.state.get_db(FILECRYPT_LINK_STATES_TABLE).store(fp(10), "bad"),
                ),
                v2_cohort_cooldown(sweep_id, retry_after=NOW + 5000),
                [
                    [
                        pkg(10),
                        package_with_defer(
                            10,
                            v2_defer(sweep_id, [fp(10)], retry_after=NOW + 5000),
                        ),
                    ]
                ],
                "unavailable",
            ),
            (
                "concurrent_target_replacement",
                lambda: None,  # interleave hook set below
                v2_cohort_cooldown(sweep_id, retry_after=NOW + 5000),
                [
                    [
                        pkg(11),
                        package_with_defer(
                            11,
                            v2_defer(sweep_id, [fp(11)], retry_after=NOW + 5000),
                        ),
                    ]
                ],
                "conflict",
            ),
        ]
        for label, setup_fn, old_raw, protected, expect_status in cases:
            with self.subTest(label):
                self.setUp()
                if old_raw is not None:
                    self.set_old_decision(old_raw)
                for row in protected:
                    self.protected_db().store(row[0], row[1])

                if label == "malformed_existing_lifecycle_header":
                    # header is already set; old decision also needed
                    pass
                setup_fn()

                if label == "concurrent_target_replacement":
                    # Simulate concurrent write: change the old decision between
                    # pre-read and mutation callback. Hook must be on the db that
                    # mutate_values is called on (sweep state table).
                    cd_db = self.state.get_db("crypter_cooldowns")
                    sweep_db = self.state.get_db(FILECRYPT_SWEEP_STATE_TABLE)
                    sweep_db.before_mutation = lambda _db=cd_db: _db.store(
                        "filecrypt", "replaced concurrently"
                    )

                svc = self.service()
                result = svc.migrate_legacy(protected_rows=protected or None)
                self.assertEqual(result["status"], expect_status, f"case={label}")
                # No marker written on failure
                if expect_status in ("unavailable", "conflict"):
                    marker = decode_migration_marker(self.marker_raw())
                    self.assertIsNone(marker)


class TestMigrationIdempotenceAndPreOfferGate(MigrationTestCase):
    """Task 6A budget method 3: idempotence and route integration."""

    def test_migration_idempotence_and_pre_offer_gate(self):
        sweep_id = "c" * 32
        cases = [
            (
                "first_call_commits_second_returns_already_migrated",
                v2_cohort_cooldown(sweep_id, retry_after=NOW + 5000),
                [
                    [
                        pkg(20),
                        package_with_defer(
                            20,
                            v2_defer(sweep_id, [fp(20)], retry_after=NOW + 5000),
                        ),
                    ]
                ],
            ),
        ]
        for label, old_raw, protected in cases:
            with self.subTest(label):
                self.setUp()
                self.set_old_decision(old_raw)
                for row in protected:
                    self.protected_db().store(row[0], row[1])
                svc = self.service()

                r1 = svc.migrate_legacy(protected_rows=protected)
                self.assertEqual(r1["status"], "complete")
                self.assertEqual(r1["held"], 1)

                # Mutation count before second call
                sweep_db = self.state.get_db(FILECRYPT_SWEEP_STATE_TABLE)
                mutations_before = sweep_db.mutation_count

                r2 = svc.migrate_legacy(protected_rows=protected)
                self.assertEqual(r2["status"], "already_migrated")
                self.assertEqual(r2["held"], 0)
                self.assertEqual(r2["packages_cleaned"], 0)
                self.assertEqual(r2["global_cooldown"], False)

                # No mutation on second call (early return before mutate_values)
                self.assertEqual(sweep_db.mutation_count, mutations_before)

        # Unavailable/conflict returns 503 gate (route-level contract)
        with self.subTest("unavailable_halts_before_offer"):
            self.setUp()
            self.state.get_db(FILECRYPT_LIFECYCLE_MIGRATIONS_TABLE).store(
                FILECRYPT_MIGRATION_KEY, "corrupt"
            )
            svc = self.service()
            result = svc.migrate_legacy(protected_rows=None)
            self.assertEqual(result["status"], "unavailable")

        with self.subTest("conflict_halts_before_offer"):
            self.setUp()
            self.set_old_decision(v2_cohort_cooldown("d" * 32, retry_after=NOW + 5000))
            cd_db = self.state.get_db("crypter_cooldowns")
            sweep_db = self.state.get_db(FILECRYPT_SWEEP_STATE_TABLE)
            sweep_db.before_mutation = lambda: cd_db.store("filecrypt", "raced")
            svc = self.service()
            result = svc.migrate_legacy(
                protected_rows=[
                    [
                        pkg(21),
                        package_with_defer(
                            21,
                            v2_defer("d" * 32, [fp(21)], retry_after=NOW + 5000),
                        ),
                    ]
                ]
            )
            self.assertEqual(result["status"], "conflict")


if __name__ == "__main__":
    unittest.main()
