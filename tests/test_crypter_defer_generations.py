# -*- coding: utf-8 -*-

import json
import os
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from quasarr.downloads.packages import get_packages
from quasarr.providers import shared_state as provider_shared_state
from quasarr.providers.crypter_cooldowns import (
    CrypterCooldownService,
    decode_package_defer,
    package_defer_covers_fingerprint,
    package_defer_is_active,
)
from quasarr.providers.crypter_sweeps import (
    SWEEP_WINDOW_SECONDS,
    decision_snapshot,
    encode_decision_record,
)
from quasarr.storage.sqlite_database import DataBase

PACKAGE_A = "Quasarr_movies_" + "a" * 32
PACKAGE_B = "Quasarr_movies_" + "b" * 32
PACKAGE_C = "Quasarr_movies_" + "c" * 32
PACKAGE_D = "Quasarr_movies_" + "d" * 32
REASON = "ip_block_suspected"
NOW = 1_700_000_000
PROVISIONAL_WINDOW = 15 * 60
COOLDOWN_SECONDS = 24 * 60 * 60
SWEEP_A = "a" * 32
SWEEP_B = "b" * 32


def fingerprint(index):
    """A synthetic 64-character lowercase-hex link fingerprint."""
    return f"{index:064d}"


def offer_id(index):
    return f"{index:032d}"


FINGERPRINT_ONE = fingerprint(1)
FINGERPRINT_TWO = fingerprint(2)


class FakeClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class FakeDatabase:
    def __init__(self, tables=None):
        self.rows = {}
        self.tables = {} if tables is None else tables
        self.lock = threading.Lock()
        self.mutation_count = 0
        self.in_mutation = False
        # One-shot hook simulating a concurrent writer that lands just before
        # the next mutation of this table starts reading.
        self.before_write = None

    def _peer(self, table):
        if table not in self.tables:
            self.tables[table] = FakeDatabase(tables=self.tables)
        return self.tables[table]

    def _interleave(self, key):
        hook, self.before_write = self.before_write, None
        if hook is not None:
            hook(key)

    def _reject_calls_from_callback(self):
        if self.in_mutation:
            raise AssertionError("storage must never be read inside a mutation")

    def retrieve(self, key):
        self._reject_calls_from_callback()
        return self.rows.get(key)

    def retrieve_all_titles(self):
        self._reject_calls_from_callback()
        items = [[key, value] for key, value in sorted(self.rows.items())]
        return items if items else None

    def update_store(self, key, value):
        self.rows[key] = value
        return True

    def mutate_value(self, key, mutator):
        self._interleave(key)
        with self.lock:
            self.mutation_count += 1
            self.in_mutation = True
            try:
                value = mutator(self.rows.get(key))
            finally:
                self.in_mutation = False
            if value is None:
                self.rows.pop(key, None)
            else:
                self.rows[key] = value
            return value

    def mutate_values(self, targets, mutator):
        self._interleave(targets[0][1])
        with self.lock:
            self.mutation_count += 1
            databases = [self._peer(table) for table, _key in targets]
            values = mutator(
                tuple(
                    database.rows.get(key)
                    for database, (_table, key) in zip(databases, targets, strict=True)
                )
            )
            for database, (_table, key), value in zip(
                databases, targets, values, strict=True
            ):
                if value is None:
                    database.rows.pop(key, None)
                else:
                    database.rows[key] = value
            return tuple(values)


class FakeCache:
    linkgrabber_packages = []
    linkgrabber_links = []
    downloader_packages = []
    downloader_links = []
    is_collecting = False

    @staticmethod
    def get_stats():
        return {}


class FakeSharedState:
    def __init__(self):
        self.values = {
            "crypter_cooldown_hours": 24,
            "database": self.get_db,
            "external_address": "https://quasarr.invalid",
        }
        self.databases = {}
        for table in ("protected", "failed", "crypter_cooldowns"):
            self.databases[table] = FakeDatabase(tables=self.databases)

    def get_db(self, table):
        if table not in self.databases:
            self.databases[table] = FakeDatabase(tables=self.databases)
        return self.databases[table]

    def get_device(self):
        return MagicMock()


def protected_blob(title="Synthetic.Release.Example", deferred=None):
    blob = {
        "title": title,
        "links": [["https://filecrypt.invalid/container/1", "filecrypt"]],
        "password": "synthetic",
        "size_mb": 1024,
        "original_url": "https://source.invalid/release",
        "imdb_id": "tt0000000",
        "notifications": [{"channel": "discord", "message_id": "1"}],
    }
    if deferred is not None:
        blob["deferred"] = deferred
    return blob


def legacy_defer(**overrides):
    block = {
        "crypter": "filecrypt",
        "reason_code": REASON,
        "since_epoch": NOW,
        "retry_after_epoch": NOW + PROVISIONAL_WINDOW,
        "probe_requested": False,
        "observation_holds": 1,
    }
    block.update(overrides)
    return block


def generation_defer(**overrides):
    block = legacy_defer()
    block.update(
        {
            "schema_version": 2,
            "sweep_id": SWEEP_A,
            "link_fingerprint": FINGERPRINT_ONE,
        }
    )
    block.update(overrides)
    return block


def pending_member(index):
    return {
        "link_fingerprint": fingerprint(index),
        "result": "pending",
        "tested_epoch": 0,
        "offer_id": "",
        "offer_expires_epoch": 0,
        "response_instruction": "",
    }


def blocked_member(index, instruction="hold"):
    return {
        "link_fingerprint": fingerprint(index),
        "result": "blocked",
        "tested_epoch": NOW - 100,
        "offer_id": offer_id(index),
        "offer_expires_epoch": NOW,
        "response_instruction": instruction,
    }


def sweeping_record(sweep_id=SWEEP_A, opened_epoch=NOW):
    return {
        "schema_version": 2,
        "state": "sweeping",
        "reason_code": REASON,
        "sweep_id": sweep_id,
        "opened_epoch": opened_epoch,
        "deadline_epoch": opened_epoch + SWEEP_WINDOW_SECONDS,
        "members": [pending_member(1), pending_member(2)],
    }


def cohort_cooldown_record(sweep_id=SWEEP_A, retry_after_epoch=NOW + COOLDOWN_SECONDS):
    members = [blocked_member(index) for index in range(1, 5)]
    members.append(blocked_member(5, instruction="cooldown"))
    return {
        "schema_version": 2,
        "state": "cooldown",
        "reason_code": REASON,
        "sweep_id": sweep_id,
        "members": members,
        "cohort_size": len(members),
        "retry_after_epoch": retry_after_epoch,
        "live_offer": None,
    }


def legacy_cooldown_record(retry_after_epoch=NOW + COOLDOWN_SECONDS):
    return {
        "schema_version": 2,
        "state": "cooldown",
        "reason_code": REASON,
        "legacy_cooldown": True,
        "retry_after_epoch": retry_after_epoch,
        "legacy_evidence_count": 3,
    }


def healthy_record(sweep_id=SWEEP_A, until_epoch=NOW + SWEEP_WINDOW_SECONDS):
    return {
        "schema_version": 2,
        "state": "healthy",
        "sweep_id": sweep_id,
        "until_epoch": until_epoch,
        "retest_members": [],
        "live_offer": None,
    }


def individual_record(
    reason="legacy_v1_hold",
    generation_id=SWEEP_A,
    until_epoch=NOW + PROVISIONAL_WINDOW,
):
    return {
        "schema_version": 2,
        "state": "individual",
        "reason": reason,
        "generation_id": generation_id,
        "until_epoch": until_epoch,
        "live_offer": None,
    }


def snapshot_of(record, now=NOW):
    return decision_snapshot(record, now=now)


class GenerationDeferSchemaTests(unittest.TestCase):
    def test_generation_defer_decodes_to_the_exact_nine_key_shape(self):
        decoded = decode_package_defer({"deferred": generation_defer()})

        self.assertEqual(
            {
                "crypter": "filecrypt",
                "reason_code": REASON,
                "since_epoch": NOW,
                "retry_after_epoch": NOW + PROVISIONAL_WINDOW,
                "probe_requested": False,
                "observation_holds": 1,
                "schema_version": 2,
                "sweep_id": SWEEP_A,
                "link_fingerprint": FINGERPRINT_ONE,
            },
            decoded,
        )

    def test_legacy_defer_keeps_its_exact_six_key_shape(self):
        decoded = decode_package_defer({"deferred": legacy_defer()})

        self.assertEqual(legacy_defer(), decoded)

    def test_every_single_key_removal_is_rejected(self):
        for key in generation_defer():
            with self.subTest(key=key):
                block = generation_defer()
                block.pop(key)
                with self.assertRaises(ValueError):
                    decode_package_defer({"deferred": block})

    def test_any_additional_key_is_rejected(self):
        for extra in ("url", "link", "sweep_deadline_epoch", "generation_id"):
            with self.subTest(extra=extra):
                with self.assertRaises(ValueError):
                    decode_package_defer(
                        {"deferred": generation_defer(**{extra: "value"})}
                    )

    def test_schema_version_must_be_exactly_the_integer_two(self):
        for version in (1, 3, "2", 2.0, True, None, [2]):
            with self.subTest(version=version):
                with self.assertRaises(ValueError):
                    decode_package_defer(
                        {"deferred": generation_defer(schema_version=version)}
                    )

    def test_sweep_id_must_be_exactly_thirty_two_lowercase_hex_characters(self):
        for sweep_id in (
            "A" * 32,
            "a" * 31,
            "a" * 33,
            "g" * 32,
            "",
            None,
            1,
            ["a" * 32],
        ):
            with self.subTest(sweep_id=sweep_id):
                with self.assertRaises(ValueError):
                    decode_package_defer(
                        {"deferred": generation_defer(sweep_id=sweep_id)}
                    )

    def test_link_fingerprint_must_be_exactly_sixty_four_lowercase_hex_characters(self):
        for value in ("A" * 64, "0" * 63, "0" * 65, "z" * 64, "", None, 1):
            with self.subTest(link_fingerprint=value):
                with self.assertRaises(ValueError):
                    decode_package_defer(
                        {"deferred": generation_defer(link_fingerprint=value)}
                    )

    def test_a_hold_never_stores_a_raw_link(self):
        encoded = json.dumps(generation_defer())

        self.assertNotIn("http", encoded)
        self.assertNotIn("://", encoded)


class PackageDeferActivationTests(unittest.TestCase):
    def test_generation_hold_is_active_inside_its_own_sweep(self):
        self.assertTrue(
            package_defer_is_active(
                generation_defer(), snapshot_of(sweeping_record()), now=NOW
            )
        )

    def test_generation_hold_is_active_inside_its_own_cohort_cooldown(self):
        self.assertTrue(
            package_defer_is_active(
                generation_defer(), snapshot_of(cohort_cooldown_record()), now=NOW
            )
        )

    def test_generation_hold_dies_the_moment_the_decision_turns_healthy(self):
        self.assertFalse(
            package_defer_is_active(
                generation_defer(), snapshot_of(healthy_record()), now=NOW
            )
        )

    def test_generation_hold_dies_under_another_generation(self):
        for record in (
            sweeping_record(sweep_id=SWEEP_B),
            cohort_cooldown_record(sweep_id=SWEEP_B),
            individual_record(generation_id=SWEEP_B),
        ):
            with self.subTest(state=record["state"], sweep=SWEEP_B):
                self.assertFalse(
                    package_defer_is_active(
                        generation_defer(), snapshot_of(record), now=NOW
                    )
                )

    def test_generation_hold_follows_its_own_legacy_individual_decision(self):
        record = individual_record(until_epoch=NOW + PROVISIONAL_WINDOW)

        for now, expected in (
            (NOW + PROVISIONAL_WINDOW - 1, True),
            (NOW + PROVISIONAL_WINDOW, False),
            (NOW + PROVISIONAL_WINDOW + 1, False),
        ):
            with self.subTest(now=now):
                self.assertEqual(
                    expected,
                    package_defer_is_active(
                        generation_defer(), snapshot_of(record, now=now), now=now
                    ),
                )

    def test_generation_hold_ignores_an_individual_decision_of_another_reason(self):
        for reason in (
            "cohort_too_small",
            "cohort_oversized",
            "sweep_expired",
            "sweep_inconclusive",
        ):
            with self.subTest(reason=reason):
                self.assertFalse(
                    package_defer_is_active(
                        generation_defer(),
                        snapshot_of(individual_record(reason=reason)),
                        now=NOW,
                    )
                )

    def test_generation_hold_expires_with_its_cohort_cooldown(self):
        record = cohort_cooldown_record(retry_after_epoch=NOW + 60)

        for now, expected in ((NOW + 59, True), (NOW + 60, False), (NOW + 61, False)):
            with self.subTest(now=now):
                self.assertEqual(
                    expected,
                    package_defer_is_active(
                        generation_defer(), snapshot_of(record, now=now), now=now
                    ),
                )

    def test_generation_hold_is_inactive_without_a_decision(self):
        self.assertFalse(package_defer_is_active(generation_defer(), None, now=NOW))

    def test_generation_hold_is_inactive_under_a_marked_legacy_cooldown(self):
        self.assertFalse(
            package_defer_is_active(
                generation_defer(), snapshot_of(legacy_cooldown_record()), now=NOW
            )
        )

    def test_legacy_hold_keeps_its_own_timestamp_without_a_decision(self):
        for now, expected in (
            (NOW + PROVISIONAL_WINDOW - 1, True),
            (NOW + PROVISIONAL_WINDOW, False),
            (NOW + PROVISIONAL_WINDOW + 1, False),
        ):
            with self.subTest(now=now):
                self.assertEqual(
                    expected, package_defer_is_active(legacy_defer(), None, now=now)
                )

    def test_legacy_hold_survives_its_own_expiry_under_a_marked_legacy_cooldown(self):
        expired = legacy_defer(retry_after_epoch=NOW)

        self.assertTrue(
            package_defer_is_active(
                expired, snapshot_of(legacy_cooldown_record()), now=NOW
            )
        )

    def test_legacy_hold_is_never_adopted_by_a_version_two_decision(self):
        for record in (
            sweeping_record(),
            cohort_cooldown_record(),
            healthy_record(),
            individual_record(),
        ):
            with self.subTest(state=record["state"]):
                self.assertFalse(
                    package_defer_is_active(
                        legacy_defer(), snapshot_of(record), now=NOW
                    )
                )

    def test_an_unusable_decision_fails_closed(self):
        for decision in ("cooldown", 2, [], {}, {"state": "unknown"}, {"state": None}):
            with self.subTest(decision=decision):
                self.assertFalse(
                    package_defer_is_active(generation_defer(), decision, now=NOW)
                )
                self.assertFalse(
                    package_defer_is_active(legacy_defer(), decision, now=NOW)
                )

    def test_an_unusable_hold_fails_closed(self):
        broken = generation_defer()
        broken.pop("sweep_id")

        for deferred in (None, {}, "deferred", broken, {"crypter": "filecrypt"}):
            with self.subTest(deferred=deferred):
                self.assertFalse(
                    package_defer_is_active(
                        deferred, snapshot_of(sweeping_record()), now=NOW
                    )
                )


class PackageDeferFingerprintTests(unittest.TestCase):
    def test_a_generation_hold_speaks_only_for_its_own_fingerprint(self):
        deferred = generation_defer()

        self.assertTrue(package_defer_covers_fingerprint(deferred, FINGERPRINT_ONE))
        self.assertFalse(package_defer_covers_fingerprint(deferred, FINGERPRINT_TWO))

    def test_every_occurrence_of_the_held_fingerprint_stays_covered(self):
        deferred = generation_defer()
        occurrences = [FINGERPRINT_ONE, FINGERPRINT_ONE]

        self.assertEqual(
            [True, True],
            [
                package_defer_covers_fingerprint(deferred, occurrence)
                for occurrence in occurrences
            ],
        )

    def test_a_legacy_hold_speaks_for_the_whole_package(self):
        for value in (FINGERPRINT_ONE, FINGERPRINT_TWO):
            with self.subTest(link_fingerprint=value):
                self.assertTrue(package_defer_covers_fingerprint(legacy_defer(), value))

    def test_a_projected_hold_is_still_classified(self):
        service = CrypterCooldownService(FakeSharedState(), clock=FakeClock(NOW))
        projected = service.project_package_defer(
            generation_defer(),
            service.snapshot("filecrypt"),
            snapshot_of(sweeping_record()),
        )

        self.assertTrue(package_defer_covers_fingerprint(projected, FINGERPRINT_ONE))
        self.assertFalse(package_defer_covers_fingerprint(projected, FINGERPRINT_TWO))

    def test_an_unusable_hold_covers_nothing(self):
        for deferred in (None, {}, "deferred", {"crypter": "filecrypt"}):
            with self.subTest(deferred=deferred):
                self.assertFalse(
                    package_defer_covers_fingerprint(deferred, FINGERPRINT_ONE)
                )


class CrypterDecisionReadTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(NOW)
        self.shared_state = FakeSharedState()
        self.cooldowns = self.shared_state.databases["crypter_cooldowns"]
        self.service = CrypterCooldownService(self.shared_state, clock=self.clock)

    def test_a_missing_or_legacy_row_carries_no_decision(self):
        self.assertIsNone(self.service.crypter_decision("filecrypt"))

        self.cooldowns.update_store(
            "filecrypt",
            json.dumps(
                {
                    "state": "cooldown",
                    "reason_code": REASON,
                    "first_seen_epoch": NOW,
                    "last_seen_epoch": NOW,
                    "retry_after_epoch": NOW + COOLDOWN_SECONDS,
                    "observations": [],
                }
            ),
        )

        self.assertIsNone(self.service.crypter_decision("filecrypt"))

    def test_the_current_decision_is_projected_without_writing(self):
        self.cooldowns.update_store(
            "filecrypt", encode_decision_record(sweeping_record())
        )

        decision = self.service.crypter_decision("filecrypt")

        self.assertEqual("sweeping", decision["state"])
        self.assertEqual(SWEEP_A, decision["sweep_id"])
        self.assertEqual(0, self.cooldowns.mutation_count)

    def test_an_expired_decision_reads_as_no_decision(self):
        self.cooldowns.update_store(
            "filecrypt",
            encode_decision_record(cohort_cooldown_record(retry_after_epoch=NOW + 60)),
        )

        self.assertIsNotNone(self.service.crypter_decision("filecrypt"))
        self.clock.now = NOW + 60
        self.assertIsNone(self.service.crypter_decision("filecrypt"))
        self.assertEqual(0, self.cooldowns.mutation_count)

    def test_a_generation_is_never_carried_across_linkcrypters(self):
        protected = self.shared_state.databases["protected"]
        protected.update_store(
            PACKAGE_A, json.dumps(protected_blob(deferred=generation_defer()))
        )

        rebound = self.service.defer_package(
            PACKAGE_A, "junkies", REASON, NOW + PROVISIONAL_WINDOW, 0
        )

        self.assertEqual(legacy_defer(crypter="junkies"), rebound)


class CompareAndClearPackageDeferTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(NOW)
        self.shared_state = FakeSharedState()
        self.protected = self.shared_state.databases["protected"]
        self.protected.update_store(
            PACKAGE_A, json.dumps(protected_blob(deferred=generation_defer()))
        )
        self.service = CrypterCooldownService(self.shared_state, clock=self.clock)

    def _stored(self, package_id=PACKAGE_A):
        return json.loads(self.protected.rows[package_id])

    def test_clearing_removes_only_the_matching_generation_block(self):
        self.assertTrue(
            self.service.compare_and_clear_package_defer(PACKAGE_A, sweep_id=SWEEP_A)
        )

        self.assertEqual(protected_blob(), self._stored())

    def test_clearing_is_idempotent(self):
        self.service.compare_and_clear_package_defer(PACKAGE_A, sweep_id=SWEEP_A)

        self.assertFalse(
            self.service.compare_and_clear_package_defer(PACKAGE_A, sweep_id=SWEEP_A)
        )
        self.assertEqual(protected_blob(), self._stored())

    def test_another_generation_is_never_cleared(self):
        self.assertFalse(
            self.service.compare_and_clear_package_defer(PACKAGE_A, sweep_id=SWEEP_B)
        )

        self.assertEqual(generation_defer(), self._stored()["deferred"])

    def test_a_legacy_hold_is_never_cleared_by_a_generation(self):
        self.protected.update_store(
            PACKAGE_A, json.dumps(protected_blob(deferred=legacy_defer()))
        )

        self.assertFalse(
            self.service.compare_and_clear_package_defer(PACKAGE_A, sweep_id=SWEEP_A)
        )
        self.assertEqual(legacy_defer(), self._stored()["deferred"])

    def test_missing_and_undeferred_packages_report_false(self):
        self.protected.update_store(PACKAGE_B, json.dumps(protected_blob()))

        self.assertFalse(
            self.service.compare_and_clear_package_defer(PACKAGE_B, sweep_id=SWEEP_A)
        )
        self.assertFalse(
            self.service.compare_and_clear_package_defer(PACKAGE_C, sweep_id=SWEEP_A)
        )

    def test_invalid_arguments_are_rejected_without_writing(self):
        mutations = self.protected.mutation_count

        with self.assertRaises(ValueError):
            self.service.compare_and_clear_package_defer(
                "not-a-package-id", sweep_id=SWEEP_A
            )
        for sweep_id in ("A" * 32, "a" * 31, "", None):
            with self.subTest(sweep_id=sweep_id):
                with self.assertRaises(ValueError):
                    self.service.compare_and_clear_package_defer(
                        PACKAGE_A, sweep_id=sweep_id
                    )

        self.assertEqual(mutations, self.protected.mutation_count)
        self.assertEqual(generation_defer(), self._stored()["deferred"])

    def test_a_newer_generation_installed_meanwhile_survives(self):
        replacement = generation_defer(sweep_id=SWEEP_B)

        def concurrent_writer(_package_id):
            self.protected.update_store(
                PACKAGE_A, json.dumps(protected_blob(deferred=replacement))
            )

        self.protected.before_write = concurrent_writer

        self.assertFalse(
            self.service.compare_and_clear_package_defer(PACKAGE_A, sweep_id=SWEEP_A)
        )
        self.assertEqual(replacement, self._stored()["deferred"])


class ClearCrypterGenerationHoldsTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(NOW)
        self.shared_state = FakeSharedState()
        self.protected = self.shared_state.databases["protected"]
        self.cooldowns = self.shared_state.databases["crypter_cooldowns"]
        self.service = CrypterCooldownService(self.shared_state, clock=self.clock)

    def _store(self, package_id, deferred=None):
        self.protected.update_store(
            package_id, json.dumps(protected_blob(deferred=deferred))
        )

    def _deferred(self, package_id):
        return json.loads(self.protected.rows[package_id]).get("deferred")

    def test_only_the_matching_crypter_and_generation_is_cleared(self):
        self._store(PACKAGE_A, generation_defer())
        self._store(PACKAGE_B, generation_defer(sweep_id=SWEEP_B))
        self._store(PACKAGE_C, generation_defer(crypter="junkies"))
        self._store(PACKAGE_D, legacy_defer())

        result = self.service.clear_crypter_generation_holds(
            "filecrypt", sweep_id=SWEEP_A
        )

        self.assertEqual({"cleared": [PACKAGE_A], "rejected": []}, result)
        self.assertIsNone(self._deferred(PACKAGE_A))
        self.assertEqual(generation_defer(sweep_id=SWEEP_B), self._deferred(PACKAGE_B))
        self.assertEqual(generation_defer(crypter="junkies"), self._deferred(PACKAGE_C))
        self.assertEqual(legacy_defer(), self._deferred(PACKAGE_D))

    def test_results_are_reported_in_deterministic_package_order(self):
        for package_id in (PACKAGE_C, PACKAGE_A, PACKAGE_B):
            self._store(package_id, generation_defer())

        result = self.service.clear_crypter_generation_holds(
            "filecrypt", sweep_id=SWEEP_A
        )

        self.assertEqual(
            {"cleared": [PACKAGE_A, PACKAGE_B, PACKAGE_C], "rejected": []}, result
        )

    def test_cleanup_is_idempotent_and_preserves_every_other_field(self):
        self._store(PACKAGE_A, generation_defer())

        self.service.clear_crypter_generation_holds("filecrypt", sweep_id=SWEEP_A)
        repeated = self.service.clear_crypter_generation_holds(
            "filecrypt", sweep_id=SWEEP_A
        )

        self.assertEqual({"cleared": [], "rejected": []}, repeated)
        self.assertEqual(protected_blob(), json.loads(self.protected.rows[PACKAGE_A]))

    def test_unreadable_rows_are_skipped_without_touching_them(self):
        self.protected.update_store(PACKAGE_A, "not-json")
        self.protected.update_store(
            PACKAGE_B, json.dumps(protected_blob(deferred={"crypter": "filecrypt"}))
        )
        self._store(PACKAGE_C, generation_defer())

        result = self.service.clear_crypter_generation_holds(
            "filecrypt", sweep_id=SWEEP_A
        )

        self.assertEqual({"cleared": [PACKAGE_C], "rejected": []}, result)
        self.assertEqual("not-json", self.protected.rows[PACKAGE_A])
        self.assertEqual({"crypter": "filecrypt"}, self._deferred(PACKAGE_B))

    def test_rows_outside_the_package_id_contract_are_rejected(self):
        self.protected.update_store(
            "not-a-package-id", json.dumps(protected_blob(deferred=generation_defer()))
        )
        self._store(PACKAGE_A, generation_defer())

        result = self.service.clear_crypter_generation_holds(
            "filecrypt", sweep_id=SWEEP_A
        )

        self.assertEqual(
            {
                "cleared": [PACKAGE_A],
                "rejected": [
                    {"package_id": "not-a-package-id", "reason": "invalid_package_id"}
                ],
            },
            result,
        )
        self.assertEqual(
            generation_defer(),
            json.loads(self.protected.rows["not-a-package-id"])["deferred"],
        )

    def test_enumeration_never_runs_inside_a_mutation(self):
        self._store(PACKAGE_A, generation_defer())
        self._store(PACKAGE_B, generation_defer())

        result = self.service.clear_crypter_generation_holds(
            "filecrypt", sweep_id=SWEEP_A
        )

        self.assertEqual({"cleared": [PACKAGE_A, PACKAGE_B], "rejected": []}, result)

    def test_a_newer_generation_installed_meanwhile_survives_and_is_reported(self):
        self._store(PACKAGE_A, generation_defer())
        self._store(PACKAGE_B, generation_defer())
        replacement = generation_defer(sweep_id=SWEEP_B)

        def concurrent_writer(_package_id):
            self.protected.update_store(
                PACKAGE_B, json.dumps(protected_blob(deferred=replacement))
            )

        self.protected.before_write = concurrent_writer

        result = self.service.clear_crypter_generation_holds(
            "filecrypt", sweep_id=SWEEP_A
        )

        self.assertEqual(
            {
                "cleared": [PACKAGE_A],
                "rejected": [
                    {"package_id": PACKAGE_B, "reason": "generation_mismatch"}
                ],
            },
            result,
        )
        self.assertEqual(replacement, self._deferred(PACKAGE_B))

    def test_a_package_removed_meanwhile_is_reported_as_missing(self):
        self._store(PACKAGE_A, generation_defer())
        self._store(PACKAGE_B, generation_defer())

        def concurrent_remover(_package_id):
            self.protected.rows.pop(PACKAGE_B, None)

        self.protected.before_write = concurrent_remover

        result = self.service.clear_crypter_generation_holds(
            "filecrypt", sweep_id=SWEEP_A
        )

        self.assertEqual(
            {
                "cleared": [PACKAGE_A],
                "rejected": [{"package_id": PACKAGE_B, "reason": "not_found"}],
            },
            result,
        )

    def test_cleanup_never_touches_the_linkcrypter_decision(self):
        record = encode_decision_record(sweeping_record())
        self.cooldowns.update_store("filecrypt", record)
        self._store(PACKAGE_A, generation_defer())

        self.service.clear_crypter_generation_holds("filecrypt", sweep_id=SWEEP_A)

        self.assertEqual(record, self.cooldowns.rows["filecrypt"])
        self.assertEqual(0, self.cooldowns.mutation_count)

    def test_invalid_arguments_are_rejected_without_writing(self):
        self._store(PACKAGE_A, generation_defer())
        mutations = self.protected.mutation_count

        with self.assertRaises(ValueError):
            self.service.clear_crypter_generation_holds(
                "unsupported-crypter", sweep_id=SWEEP_A
            )
        with self.assertRaises(ValueError):
            self.service.clear_crypter_generation_holds("filecrypt", sweep_id="A" * 32)

        self.assertEqual(mutations, self.protected.mutation_count)
        self.assertEqual(generation_defer(), self._deferred(PACKAGE_A))


class GenerationHoldProjectionTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(NOW)
        self.shared_state = FakeSharedState()
        self.protected = self.shared_state.databases["protected"]
        self.cooldowns = self.shared_state.databases["crypter_cooldowns"]
        self.protected.update_store(
            PACKAGE_A, json.dumps(protected_blob(deferred=generation_defer()))
        )
        self.service = CrypterCooldownService(self.shared_state, clock=self.clock)

    def _decide(self, record):
        self.cooldowns.update_store("filecrypt", encode_decision_record(record))

    def _queue_item(self):
        def build_service(shared_state):
            return CrypterCooldownService(shared_state, clock=self.clock)

        with (
            patch(
                "quasarr.downloads.packages.JDPackageCache", return_value=FakeCache()
            ),
            patch(
                "quasarr.downloads.packages.get_download_category_from_package_id",
                return_value="movies",
            ),
            patch("quasarr.downloads.packages.CrypterCooldownService", build_service),
        ):
            return get_packages(self.shared_state, auto_start=False)["queue"][0]

    def test_a_hold_of_the_running_sweep_is_projected_as_active(self):
        self._decide(sweeping_record())

        item = self._queue_item()

        self.assertEqual(
            "[Waiting for linkcrypter retry] Synthetic.Release.Example",
            item["filename"],
        )
        self.assertTrue(item["deferred"]["active"])
        self.assertEqual("provisional", item["deferred"]["hold_type"])
        self.assertEqual("observing", item["deferred"]["state"])
        self.assertEqual(SWEEP_A, item["deferred"]["sweep_id"])

    def test_a_healthy_decision_deactivates_the_hold_immediately(self):
        self._decide(healthy_record())

        item = self._queue_item()

        self.assertEqual(
            "[CAPTCHA not solved!] Synthetic.Release.Example", item["filename"]
        )
        self.assertFalse(item["deferred"]["active"])
        self.assertEqual("none", item["deferred"]["hold_type"])
        # Logical invalidation never rewrites the stored package metadata.
        self.assertEqual(
            generation_defer(),
            json.loads(self.protected.rows[PACKAGE_A])["deferred"],
        )

    def test_a_newer_generation_deactivates_the_previous_hold(self):
        self._decide(sweeping_record(sweep_id=SWEEP_B))

        item = self._queue_item()

        self.assertFalse(item["deferred"]["active"])
        self.assertEqual(
            "[CAPTCHA not solved!] Synthetic.Release.Example", item["filename"]
        )

    def test_a_matching_cohort_cooldown_keeps_the_hold_active(self):
        self._decide(cohort_cooldown_record())

        item = self._queue_item()

        self.assertTrue(item["deferred"]["active"])
        self.assertEqual("crypter_cooldown", item["deferred"]["hold_type"])
        self.assertEqual("cooldown", item["deferred"]["state"])
        self.assertEqual(5, item["deferred"]["evidence_count"])
        self.assertEqual(NOW + COOLDOWN_SECONDS, item["deferred"]["retry_after_epoch"])

    def test_a_hold_without_any_decision_is_inactive(self):
        item = self._queue_item()

        self.assertFalse(item["deferred"]["active"])
        self.assertEqual(
            "[CAPTCHA not solved!] Synthetic.Release.Example", item["filename"]
        )

    def test_cleanup_that_never_ran_cannot_reactivate_an_invalidated_hold(self):
        self._decide(healthy_record())

        item = self._queue_item()

        self.assertFalse(item["deferred"]["active"])
        # The physical row is still there; only the decision decides.
        self.assertEqual(
            generation_defer(),
            json.loads(self.protected.rows[PACKAGE_A])["deferred"],
        )

    def test_a_legacy_hold_still_follows_its_own_timestamp(self):
        self.protected.update_store(
            PACKAGE_A, json.dumps(protected_blob(deferred=legacy_defer()))
        )

        active = self._queue_item()
        self.clock.now = NOW + PROVISIONAL_WINDOW + 1
        expired = self._queue_item()

        self.assertTrue(active["deferred"]["active"])
        self.assertEqual("provisional", active["deferred"]["hold_type"])
        self.assertFalse(expired["deferred"]["active"])

    def test_a_legacy_hold_under_a_cohort_cooldown_is_held_by_the_cooldown_only(self):
        self.protected.update_store(
            PACKAGE_A, json.dumps(protected_blob(deferred=legacy_defer()))
        )
        self._decide(cohort_cooldown_record())

        item = self._queue_item()

        self.assertEqual("crypter_cooldown", item["deferred"]["hold_type"])
        self.assertFalse(
            package_defer_is_active(
                legacy_defer(), snapshot_of(cohort_cooldown_record()), now=NOW
            )
        )

    def test_a_malformed_generation_hold_still_renders_the_package(self):
        self.protected.update_store(
            PACKAGE_A,
            json.dumps(protected_blob(deferred=generation_defer(sweep_id="A" * 32))),
        )
        self._decide(sweeping_record())

        item = self._queue_item()

        self.assertEqual(
            "[CAPTCHA not solved!] Synthetic.Release.Example", item["filename"]
        )
        self.assertNotIn("deferred", item)
        self.assertEqual(
            protected_blob(deferred=generation_defer(sweep_id="A" * 32)),
            json.loads(self.protected.rows[PACKAGE_A]),
        )


class RealDatabaseGenerationRaceTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.original_provider_values = provider_shared_state.values
        provider_shared_state.values = {
            "dbfile": os.path.join(self.tmpdir.name, "Quasarr.db")
        }
        self.databases = {
            "protected": DataBase("protected"),
            "crypter_cooldowns": DataBase("crypter_cooldowns"),
        }
        self.concurrent_protected = DataBase("protected")
        self.values = {"crypter_cooldown_hours": 24, "database": self.get_db}
        self.service = CrypterCooldownService(self, clock=FakeClock(NOW))

    def tearDown(self):
        for database in self.databases.values():
            database._conn.close()
        self.concurrent_protected._conn.close()
        provider_shared_state.values = self.original_provider_values
        self.tmpdir.cleanup()

    def get_db(self, table):
        return self.databases[table]

    def _store(self, package_id, deferred):
        self.databases["protected"].update_store(
            package_id, json.dumps(protected_blob(deferred=deferred))
        )

    def _deferred(self, package_id):
        return json.loads(self.databases["protected"].retrieve(package_id)).get(
            "deferred"
        )

    def test_a_stale_read_can_never_clear_a_newer_generation(self):
        self._store(PACKAGE_A, generation_defer())
        replacement = generation_defer(sweep_id=SWEEP_B)

        observed = self.service.get_package_defer(PACKAGE_A)
        self.concurrent_protected.update_store(
            PACKAGE_A, json.dumps(protected_blob(deferred=replacement))
        )

        self.assertEqual(SWEEP_A, observed["sweep_id"])
        self.assertFalse(
            self.service.compare_and_clear_package_defer(
                PACKAGE_A, sweep_id=observed["sweep_id"]
            )
        )
        self.assertEqual(replacement, self._deferred(PACKAGE_A))

    def test_a_generation_installed_after_enumeration_survives_the_cleanup(self):
        self._store(PACKAGE_A, generation_defer())
        self._store(PACKAGE_B, generation_defer())
        replacement = generation_defer(sweep_id=SWEEP_B)
        original = DataBase.retrieve_all_titles

        def enumerate_then_replace(database):
            rows = original(database)
            self.concurrent_protected.update_store(
                PACKAGE_B, json.dumps(protected_blob(deferred=replacement))
            )
            return rows

        with patch.object(DataBase, "retrieve_all_titles", enumerate_then_replace):
            result = self.service.clear_crypter_generation_holds(
                "filecrypt", sweep_id=SWEEP_A
            )

        self.assertEqual(
            {
                "cleared": [PACKAGE_A],
                "rejected": [
                    {"package_id": PACKAGE_B, "reason": "generation_mismatch"}
                ],
            },
            result,
        )
        self.assertIsNone(self._deferred(PACKAGE_A))
        self.assertEqual(replacement, self._deferred(PACKAGE_B))


if __name__ == "__main__":
    unittest.main()
