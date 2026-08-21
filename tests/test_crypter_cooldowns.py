# -*- coding: utf-8 -*-

import hashlib
import importlib.util
import json
import sys
import threading
import unittest
from unittest.mock import patch

import quasarr.providers.crypter_cooldowns as cooldown_module
from quasarr.downloads import (
    classify_links,
    detect_crypter,
    protected_crypter_keys,
    resolve_protected_crypter_key,
)
from quasarr.providers.crypter_cooldowns import (
    CrypterCooldownService,
    cooling_crypters,
    decode_package_defer,
    normalize_crypter_key,
    package_defer_covers_fingerprint,
    package_defer_is_active,
    validate_link_fingerprint,
)
from quasarr.providers.log import _contexts_to_str

PACKAGE_A = "Quasarr_movies_00000000000000000000000000000000"
PACKAGE_B = "Quasarr_movies_11111111111111111111111111111111"
PACKAGE_C = "Quasarr_movies_22222222222222222222222222222222"
PACKAGE_D = "Quasarr_movies_33333333333333333333333333333333"
REASON = "ip_block_suspected"


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
        self.retrieve_count = 0
        self.before_mutation = None

    def _peer(self, table):
        if table not in self.tables:
            self.tables[table] = FakeDatabase(self.tables)
        return self.tables[table]

    def retrieve(self, key):
        self.retrieve_count += 1
        return self.rows.get(key)

    def retrieve_all_titles(self):
        """Same contract as the SQLite table: ordered pairs, or None if empty."""
        items = [[key, value] for key, value in sorted(self.rows.items())]
        return items if items else None

    def update_store(self, key, value):
        self.rows[key] = value
        return True

    def mutate_value(self, key, mutator):
        with self.lock:
            self.mutation_count += 1
            if self.before_mutation is not None:
                before_mutation = self.before_mutation
                self.before_mutation = None
                before_mutation()
            value = mutator(self.rows.get(key))
            if value is None:
                self.rows.pop(key, None)
            else:
                self.rows[key] = value
            return value

    def mutate_values(self, targets, mutator):
        """One transaction over several tables, like the sqlite primitive."""
        with self.lock:
            self.mutation_count += 1
            if self.before_mutation is not None:
                before_mutation = self.before_mutation
                self.before_mutation = None
                before_mutation()
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


class FakeSharedState:
    def __init__(self, cooldown_hours=24):
        self.values = {"crypter_cooldown_hours": cooldown_hours}
        self.databases = {}
        self.databases["crypter_cooldowns"] = FakeDatabase(self.databases)

    def get_db(self, table):
        if table not in self.databases:
            self.databases[table] = FakeDatabase(self.databases)
        return self.databases[table]


class ProtectedCrypterResolverTests(unittest.TestCase):
    def test_protected_links_resolve_to_existing_detection_keys(self):
        links = (
            ["https://filecrypt.invalid/container/1", "filecrypt"],
            ["https://tolink.invalid/container/2", "tolink"],
            ["https://keeplinks.invalid/container/3", "keeplinks"],
        )

        for link in links:
            with self.subTest(link=link):
                detected, detected_type = detect_crypter(link[0])
                self.assertEqual("protected", detected_type)
                self.assertEqual(detected, resolve_protected_crypter_key(link))

        self.assertEqual(
            frozenset({"filecrypt", "tolink", "keeplinks", "junkies"}),
            protected_crypter_keys(),
        )

    def test_junkies_mirror_tag_uses_protected_resolver_and_bucket(self):
        link = ["https://container.invalid/item", "junkies"]

        self.assertEqual("junkies", resolve_protected_crypter_key(link))
        self.assertEqual(
            {"direct": [], "auto": [], "protected": [link]}, classify_links([link])
        )

    def test_non_protected_and_malformed_values_do_not_resolve(self):
        rejected = (
            ["https://hoster.invalid/file", "hoster"],
            ["https://hide.invalid/container", "hide"],
            ["https://source.invalid/release", "dw"],
            "filecrypt",
            "junkies",
            None,
            {},
            [],
            [None, "filecrypt"],
        )

        for value in rejected:
            with self.subTest(value=value):
                self.assertIsNone(resolve_protected_crypter_key(value))

        auto_link = ["https://hide.invalid/container", "hide"]
        self.assertEqual(
            {"direct": [], "auto": [auto_link], "protected": []},
            classify_links([auto_link]),
        )

    def test_cooldown_key_normalization_uses_only_protected_allowlist(self):
        self.assertEqual("filecrypt", normalize_crypter_key(" FileCrypt "))
        for value in ("hide", "dw", "hoster", "", None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    normalize_crypter_key(value)


class CooldownModuleBoundaryTests(unittest.TestCase):
    def test_module_import_does_not_require_downloads(self):
        spec = importlib.util.spec_from_file_location(
            "_crypter_cooldown_import_probe", cooldown_module.__file__
        )
        module = importlib.util.module_from_spec(spec)

        with patch.dict(sys.modules, {"quasarr.downloads": None}):
            spec.loader.exec_module(module)

    def test_log_context_uses_concise_cooldown_marker(self):
        context, source = _contexts_to_str(
            ["quasarr", "providers", "crypter_cooldowns"]
        )

        self.assertEqual("🔌⏳", context)
        self.assertEqual("", source)


class CrypterCooldownServiceTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(1_700_000_000)
        self.shared_state = FakeSharedState()
        self.service = CrypterCooldownService(self.shared_state, clock=self.clock)

    def _observe(self, package_id, fingerprint_character):
        return self.service.observe(
            "filecrypt", package_id, fingerprint_character * 64, REASON
        )

    def _enter_cooldown(self):
        decision = self._observe(PACKAGE_A, "a")
        self.clock.now += 1
        decision = self._observe(PACKAGE_B, "b")
        self.clock.now += 1
        decision = self._observe(PACKAGE_C, "c")
        return decision

    def test_first_observation_starts_provisional_hold(self):
        decision = self.service.observe("filecrypt", PACKAGE_A, "a" * 64, REASON)

        self.assertEqual("observing", decision["state"])
        self.assertEqual(1, decision["evidence_count"])
        self.assertEqual(
            self.clock.now + 15 * 60, decision["package_retry_after_epoch"]
        )

        stored = json.loads(
            self.shared_state.databases["crypter_cooldowns"].rows["filecrypt"]
        )
        self.assertEqual(
            {
                "state": "observing",
                "reason_code": REASON,
                "first_seen_epoch": self.clock.now,
                "last_seen_epoch": self.clock.now,
                "retry_after_epoch": 0,
                "observations": [
                    {
                        "package_id": PACKAGE_A,
                        "link_fingerprint": "a" * 64,
                        "seen_at_epoch": self.clock.now,
                    }
                ],
            },
            stored,
        )

    def test_duplicate_package_or_fingerprint_does_not_refresh_evidence(self):
        self._observe(PACKAGE_A, "a")

        self.clock.now += 10
        duplicate_package = self._observe(PACKAGE_A, "b")
        self.clock.now += 10
        duplicate_fingerprint = self._observe(PACKAGE_B, "a")
        self.clock.now += 10
        second_distinct = self._observe(PACKAGE_C, "c")

        self.assertEqual(1, duplicate_package["evidence_count"])
        self.assertEqual(1, duplicate_fingerprint["evidence_count"])
        self.assertEqual(2, second_distinct["evidence_count"])
        stored = json.loads(
            self.shared_state.databases["crypter_cooldowns"].rows["filecrypt"]
        )
        self.assertEqual(
            {PACKAGE_A, PACKAGE_C},
            {item["package_id"] for item in stored["observations"]},
        )
        self.assertEqual(
            self.clock.now - 30,
            next(
                item["seen_at_epoch"]
                for item in stored["observations"]
                if item["package_id"] == PACKAGE_A
            ),
        )

    def test_duplicate_keeps_original_provisional_retry_deadline(self):
        first = self._observe(PACKAGE_A, "a")
        first_seen = self.clock.now
        self.clock.now += 899

        duplicate = self._observe(PACKAGE_A, "b")
        distinct = self._observe(PACKAGE_B, "b")

        self.assertEqual(
            first["package_retry_after_epoch"],
            duplicate["package_retry_after_epoch"],
        )
        self.assertEqual(
            self.clock.now + 15 * 60,
            distinct["package_retry_after_epoch"],
        )
        stored = json.loads(
            self.shared_state.databases["crypter_cooldowns"].rows["filecrypt"]
        )
        self.assertEqual(
            {
                PACKAGE_A: first_seen,
                PACKAGE_B: self.clock.now,
            },
            {
                item["package_id"]: item["seen_at_epoch"]
                for item in stored["observations"]
            },
        )

    def test_duplicate_cannot_extend_evidence_beyond_fixed_window(self):
        self._observe(PACKAGE_A, "a")
        self.clock.now += 899
        duplicate = self._observe(PACKAGE_A, "b")
        after_duplicate = json.loads(
            self.shared_state.databases["crypter_cooldowns"].rows["filecrypt"]
        )
        self.assertEqual(self.clock.now - 899, after_duplicate["first_seen_epoch"])
        self.assertEqual(self.clock.now - 899, after_duplicate["last_seen_epoch"])
        self.clock.now += 2

        decision = self._observe(PACKAGE_B, "b")

        self.assertEqual(1, duplicate["evidence_count"])
        self.assertEqual(1, decision["evidence_count"])
        snapshot = self.service.snapshot("filecrypt")
        self.assertEqual(self.clock.now, snapshot["first_seen_epoch"])
        self.assertEqual(
            [PACKAGE_B],
            [item["package_id"] for item in snapshot["observations"]],
        )

    def test_three_distinct_observations_create_cooldown(self):
        decision = self._enter_cooldown()
        expected_retry = self.clock.now + 24 * 60 * 60

        self.assertEqual("cooldown", decision["state"])
        self.assertEqual(3, decision["evidence_count"])
        self.assertEqual(expected_retry, decision["package_retry_after_epoch"])
        self.assertTrue(self.service.is_cooling("filecrypt"))
        self.assertEqual(expected_retry, self.service.retry_after("filecrypt"))

        snapshot = self.service.snapshot("filecrypt")
        self.assertEqual("cooldown", snapshot["state"])
        self.assertEqual(3, snapshot["evidence_count"])
        self.assertEqual(expected_retry, snapshot["retry_after_epoch"])
        self.assertNotIn("probe", json.dumps(snapshot))

    def test_duplicate_evidence_does_not_extend_active_cooldown(self):
        initial = self._enter_cooldown()
        initial_retry_after = initial["package_retry_after_epoch"]
        initial_last_seen = self.clock.now

        self.clock.now += 60
        duplicate_package = self._observe(PACKAGE_A, "d")
        self.clock.now += 60
        duplicate_fingerprint = self._observe(PACKAGE_D, "a")

        self.assertEqual(
            initial_retry_after, duplicate_package["package_retry_after_epoch"]
        )
        self.assertEqual(
            initial_retry_after, duplicate_fingerprint["package_retry_after_epoch"]
        )
        snapshot = self.service.snapshot("filecrypt")
        self.assertEqual(initial_last_seen, snapshot["last_seen_epoch"])

    def test_observations_outside_window_are_pruned(self):
        self._observe(PACKAGE_A, "a")
        self.clock.now += 100
        self._observe(PACKAGE_B, "b")
        self.clock.now += 801

        decision = self._observe(PACKAGE_C, "c")

        self.assertEqual("observing", decision["state"])
        self.assertEqual(2, decision["evidence_count"])
        snapshot = self.service.snapshot("filecrypt")
        self.assertEqual(
            {PACKAGE_B, PACKAGE_C},
            {item["package_id"] for item in snapshot["observations"]},
        )
        self.assertEqual(self.clock.now - 801, snapshot["first_seen_epoch"])

    def test_cooldown_is_clamped_and_never_shortened_by_later_settings(self):
        self.shared_state.values["crypter_cooldown_hours"] = 1
        clamped = self._enter_cooldown()
        self.assertEqual(
            self.clock.now + 24 * 60 * 60,
            clamped["package_retry_after_epoch"],
        )

        self.service.record_success("filecrypt")
        self.shared_state.values["crypter_cooldown_hours"] = 48
        initial = self._enter_cooldown()
        initial_retry = initial["package_retry_after_epoch"]

        self.shared_state.values["crypter_cooldown_hours"] = 24
        self.clock.now += 60
        preserved = self._observe(PACKAGE_D, "d")
        self.assertEqual(initial_retry, preserved["package_retry_after_epoch"])

        self.clock.now = initial_retry - 60 * 60
        restarted = self._observe(PACKAGE_A, "a")
        self.assertEqual(
            self.clock.now + 24 * 60 * 60,
            restarted["package_retry_after_epoch"],
        )

    def test_success_removes_cooldown_row(self):
        self._enter_cooldown()

        self.assertIsNone(self.service.record_success("filecrypt"))

        self.assertNotIn(
            "filecrypt", self.shared_state.databases["crypter_cooldowns"].rows
        )
        self.assertEqual("available", self.service.snapshot("filecrypt")["state"])
        self.assertFalse(self.service.is_cooling("filecrypt"))
        self.assertEqual(0, self.service.retry_after("filecrypt"))

    def test_expired_cooldown_becomes_available(self):
        decision = self._enter_cooldown()
        self.clock.now = decision["package_retry_after_epoch"]

        self.assertEqual("available", self.service.snapshot("filecrypt")["state"])
        self.assertFalse(self.service.is_cooling("filecrypt"))
        self.assertNotIn(
            "filecrypt", self.shared_state.databases["crypter_cooldowns"].rows
        )

    def test_observations_accept_custom_category_package_ids(self):
        # Custom download categories may contain digits, so evidence for a
        # "movies4k" package must be recorded like any other package.
        custom_package = "Quasarr_movies4k_" + "4" * 32

        self.service.observe("filecrypt", custom_package, "a" * 64, REASON)

        stored = json.loads(
            self.shared_state.databases["crypter_cooldowns"].rows["filecrypt"]
        )
        self.assertEqual(
            [custom_package],
            [observation["package_id"] for observation in stored["observations"]],
        )

    def test_invalid_observations_raise_without_database_writes(self):
        invalid = (
            ("hide", PACKAGE_A, "a" * 64, REASON),
            ("filecrypt", "invalid-package", "a" * 64, REASON),
            ("filecrypt", "Quasarr_Movies_" + "0" * 32, "a" * 64, REASON),
            ("filecrypt", "Quasarr_movies-4k_" + "0" * 32, "a" * 64, REASON),
            ("filecrypt", PACKAGE_A, "A" * 64, REASON),
            ("filecrypt", PACKAGE_A, "a" * 63, REASON),
            ("filecrypt", PACKAGE_A, "z" * 64, REASON),
            ("filecrypt", PACKAGE_A, "a" * 64, "captcha_failed"),
        )
        database = self.shared_state.databases["crypter_cooldowns"]

        for args in invalid:
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    self.service.observe(*args)

        self.assertEqual(0, database.mutation_count)
        self.assertEqual({}, database.rows)

        self.assertEqual("a" * 64, validate_link_fingerprint("a" * 64))
        for value in ("A" * 64, "a" * 63, "z" * 64, None):
            with self.subTest(fingerprint=value):
                with self.assertRaises(ValueError):
                    validate_link_fingerprint(value)

    def test_persistence_contains_fingerprint_but_no_raw_url(self):
        raw_url = "https://filecrypt.invalid/private/container"
        fingerprint = hashlib.sha256(raw_url.encode("utf-8")).hexdigest()

        self.service.observe("filecrypt", PACKAGE_A, fingerprint, REASON)

        stored = self.shared_state.databases["crypter_cooldowns"].rows["filecrypt"]
        self.assertNotIn(raw_url, stored)
        self.assertNotIn('"url"', stored)
        self.assertIn(fingerprint, stored)

    def test_additive_persisted_fields_are_ignored_without_rewriting_the_row(self):
        database = self.shared_state.databases["crypter_cooldowns"]
        self._observe(PACKAGE_A, "a")
        persisted = json.loads(database.rows["filecrypt"])
        persisted["future_metadata"] = {"sensitive_marker": "do-not-log"}
        persisted["observations"][0]["future_observation_field"] = "ignored"
        raw_record = json.dumps(persisted)
        database.rows["filecrypt"] = raw_record
        database.mutation_count = 0

        snapshot = self.service.snapshot("filecrypt")

        self.assertEqual("observing", snapshot["state"])
        self.assertNotIn("future_metadata", snapshot)
        self.assertNotIn("future_observation_field", snapshot["observations"][0])
        self.assertEqual(raw_record, database.rows["filecrypt"])
        self.assertEqual(0, database.mutation_count)

    def test_invalid_persisted_rows_self_heal_with_sanitized_warning(self):
        database = self.shared_state.databases["crypter_cooldowns"]
        self._observe(PACKAGE_A, "a")
        valid_record = json.loads(database.rows["filecrypt"])
        missing_required = dict(valid_record)
        del missing_required["observations"]
        missing_observation_field = json.loads(json.dumps(valid_record))
        del missing_observation_field["observations"][0]["link_fingerprint"]
        unsupported_state = dict(valid_record)
        unsupported_state["state"] = "future-sensitive-state"
        invalid_rows = (
            "not-json-sensitive-marker",
            json.dumps(missing_required),
            json.dumps(missing_observation_field),
            json.dumps(unsupported_state),
        )

        for raw_record in invalid_rows:
            with self.subTest(raw_record=raw_record):
                database.rows["filecrypt"] = raw_record
                database.mutation_count = 0

                with patch(
                    "quasarr.providers.crypter_cooldowns.warn", create=True
                ) as warning:
                    snapshot = self.service.snapshot("filecrypt")

                self.assertEqual("available", snapshot["state"])
                self.assertNotIn("filecrypt", database.rows)
                self.assertEqual(1, database.mutation_count)
                warning.assert_called_once()
                warning_text = str(warning.call_args)
                self.assertIn("filecrypt", warning_text)
                self.assertNotIn(raw_record, warning_text)

    def test_malformed_required_field_types_self_heal_for_all_service_apis(self):
        database = self.shared_state.databases["crypter_cooldowns"]
        valid_record = {
            "state": "observing",
            "reason_code": REASON,
            "first_seen_epoch": self.clock.now,
            "last_seen_epoch": self.clock.now,
            "retry_after_epoch": 0,
            "observations": [
                {
                    "package_id": PACKAGE_A,
                    "link_fingerprint": "a" * 64,
                    "seen_at_epoch": self.clock.now,
                }
            ],
            "future_metadata": "sensitive-marker",
        }
        malformed_rows = []
        for field_name, invalid_value in (
            ("state", []),
            ("reason_code", []),
            ("first_seen_epoch", "not-an-integer"),
            ("last_seen_epoch", "not-an-integer"),
            ("retry_after_epoch", "not-an-integer"),
            ("observations", {}),
        ):
            record = json.loads(json.dumps(valid_record))
            record[field_name] = invalid_value
            malformed_rows.append((field_name, json.dumps(record)))
        for field_name, invalid_value in (
            ("package_id", []),
            ("link_fingerprint", {}),
            ("seen_at_epoch", "not-an-integer"),
        ):
            record = json.loads(json.dumps(valid_record))
            record["observations"][0][field_name] = invalid_value
            malformed_rows.append((f"observation.{field_name}", json.dumps(record)))

        service_calls = (
            (
                "snapshot",
                lambda: self.service.snapshot("filecrypt")["state"],
                "available",
                False,
            ),
            (
                "is_cooling",
                lambda: self.service.is_cooling("filecrypt"),
                False,
                False,
            ),
            (
                "retry_after",
                lambda: self.service.retry_after("filecrypt"),
                0,
                False,
            ),
            (
                "observe",
                lambda: self._observe(PACKAGE_B, "b")["state"],
                "observing",
                True,
            ),
        )

        for field_name, raw_record in malformed_rows:
            for api_name, call, expected, row_survives in service_calls:
                with self.subTest(field=field_name, api=api_name):
                    database.rows["filecrypt"] = raw_record
                    database.mutation_count = 0

                    with patch(
                        "quasarr.providers.crypter_cooldowns.warn", create=True
                    ) as warning:
                        result = call()

                    self.assertEqual(expected, result)
                    self.assertEqual(row_survives, "filecrypt" in database.rows)
                    self.assertEqual(1, database.mutation_count)
                    warning.assert_called_once()
                    warning_text = str(warning.call_args)
                    self.assertIn("filecrypt", warning_text)
                    self.assertNotIn(raw_record, warning_text)
                    self.assertNotIn("sensitive-marker", warning_text)

                    if not row_survives:
                        recovered = self._observe(PACKAGE_B, "b")
                        self.assertEqual("observing", recovered["state"])
                    snapshot = self.service.snapshot("filecrypt")
                    self.assertEqual("observing", snapshot["state"])
                    self.assertEqual(1, snapshot["evidence_count"])

    def test_observation_replaces_invalid_row_and_logs_sanitized_warning(self):
        database = self.shared_state.databases["crypter_cooldowns"]
        raw_record = "malformed-sensitive-marker"
        database.rows["filecrypt"] = raw_record

        with patch("quasarr.providers.crypter_cooldowns.warn", create=True) as warning:
            decision = self._observe(PACKAGE_A, "a")

        self.assertEqual("observing", decision["state"])
        self.assertEqual(1, decision["evidence_count"])
        persisted = json.loads(database.rows["filecrypt"])
        self.assertEqual(PACKAGE_A, persisted["observations"][0]["package_id"])
        warning.assert_called_once()
        warning_text = str(warning.call_args)
        self.assertIn("filecrypt", warning_text)
        self.assertNotIn(raw_record, warning_text)

    def test_cleanup_returns_concurrent_valid_replacement(self):
        database = self.shared_state.databases["crypter_cooldowns"]
        database.rows["filecrypt"] = "malformed-sensitive-marker"
        concurrent_record = {
            "state": "cooldown",
            "reason_code": REASON,
            "first_seen_epoch": self.clock.now,
            "last_seen_epoch": self.clock.now,
            "retry_after_epoch": self.clock.now + 24 * 60 * 60,
            "observations": [
                {
                    "package_id": PACKAGE_A,
                    "link_fingerprint": "a" * 64,
                    "seen_at_epoch": self.clock.now,
                }
            ],
        }
        concurrent_value = json.dumps(concurrent_record)
        database.before_mutation = lambda: database.rows.__setitem__(
            "filecrypt", concurrent_value
        )

        with patch("quasarr.providers.crypter_cooldowns.warn"):
            snapshot = self.service.snapshot("filecrypt")

        self.assertEqual("cooldown", snapshot["state"])
        self.assertEqual(1, snapshot["evidence_count"])
        self.assertEqual(concurrent_value, database.rows["filecrypt"])

    def test_missing_and_valid_snapshots_are_read_only(self):
        database = self.shared_state.databases["crypter_cooldowns"]

        self.assertEqual("available", self.service.snapshot("filecrypt")["state"])
        self.assertEqual(1, database.retrieve_count)
        self.assertEqual(0, database.mutation_count)

        self._observe(PACKAGE_A, "a")
        database.retrieve_count = 0
        database.mutation_count = 0

        self.assertEqual("observing", self.service.snapshot("filecrypt")["state"])
        self.assertFalse(self.service.is_cooling("filecrypt"))
        self.assertEqual(0, self.service.retry_after("filecrypt"))
        self.assertEqual(3, database.retrieve_count)
        self.assertEqual(0, database.mutation_count)


class CoolingCryptersProjectionTests(unittest.TestCase):
    """`cooling_crypters()` - the read-only answer to "is anything paused?".

    The operator projection in providers/statistics.py asks
    `crypter_decision("filecrypt")` by name; a cooldown on any other crypter
    is just as blocking and was never surfaced anywhere in the UI. This reads
    the table itself instead, so the answer follows whatever actually cooled.
    """

    def setUp(self):
        self.clock = FakeClock(1_000_000)
        self.shared_state = FakeSharedState()
        self.service = CrypterCooldownService(self.shared_state, clock=self.clock)

    def _cool(self, crypter):
        for index, package_id in enumerate((PACKAGE_A, PACKAGE_B, PACKAGE_C)):
            self.service.observe(
                crypter,
                package_id,
                hashlib.sha256(f"{crypter}{index}".encode()).hexdigest(),
                REASON,
            )
            self.clock.now += 1

    def test_no_rows_means_nothing_is_cooling(self):
        self.assertEqual([], cooling_crypters(self.shared_state, clock=self.clock))

    def test_a_cooling_crypter_is_reported_with_its_label_and_deadline(self):
        self._cool("filecrypt")

        cooling = cooling_crypters(self.shared_state, clock=self.clock)

        self.assertEqual(1, len(cooling))
        self.assertEqual("filecrypt", cooling[0]["crypter"])
        self.assertEqual("FileCrypt", cooling[0]["label"])
        self.assertGreater(cooling[0]["retry_after_epoch"], self.clock.now)
        self.assertEqual({"crypter", "label", "retry_after_epoch"}, set(cooling[0]))

    def test_every_cooling_crypter_is_reported_soonest_first(self):
        self._cool("filecrypt")
        self.clock.now += 3600
        self._cool("junkies")

        cooling = cooling_crypters(self.shared_state, clock=self.clock)

        self.assertEqual(["filecrypt", "junkies"], [row["crypter"] for row in cooling])
        deadlines = [row["retry_after_epoch"] for row in cooling]
        self.assertEqual(sorted(deadlines), deadlines)

    def test_an_observing_crypter_is_not_reported_as_cooling(self):
        # One observation is a provisional hold on that package, not a global
        # pause - announcing it would tell the user everything is blocked
        # when nothing is.
        self.service.observe("filecrypt", PACKAGE_A, "a" * 64, REASON)

        self.assertEqual([], cooling_crypters(self.shared_state, clock=self.clock))

    def test_an_expired_cooldown_is_not_reported(self):
        self._cool("filecrypt")
        cooling = cooling_crypters(self.shared_state, clock=self.clock)
        self.clock.now = cooling[0]["retry_after_epoch"]

        self.assertEqual([], cooling_crypters(self.shared_state, clock=self.clock))

    def test_legacy_block_mode_reports_nothing(self):
        self._cool("filecrypt")
        self.shared_state.values["crypter_block_mode"] = "fail"

        self.assertEqual([], cooling_crypters(self.shared_state, clock=self.clock))

    def test_a_lifecycle_cooldown_is_reported_even_with_no_legacy_row(self):
        # The version-two Filecrypt lifecycle keeps its header in
        # filecrypt_sweep_state, NOT in crypter_cooldowns. Reading only the
        # legacy table makes a real, active cooldown invisible - measured on
        # a live system that had crypter_cooldowns empty while
        # filecrypt_sweep_state held state="cooldown" with 21 hours left.
        header = {
            "schema_version": 1,
            "state": "cooldown",
            "generation_id": "a" * 32,
            "retry_after_epoch": self.clock.now + 7200,
            "sweep_deadline_epoch": self.clock.now - 60,
        }
        self.shared_state.get_db("filecrypt_sweep_state").update_store(
            "filecrypt", json.dumps(header)
        )

        cooling = cooling_crypters(self.shared_state, clock=self.clock)

        self.assertEqual(["filecrypt"], [row["crypter"] for row in cooling])
        self.assertEqual(self.clock.now + 7200, cooling[0]["retry_after_epoch"])
        self.assertEqual("FileCrypt", cooling[0]["label"])

    def test_an_expired_lifecycle_cooldown_is_not_reported(self):
        header = {
            "schema_version": 1,
            "state": "cooldown",
            "generation_id": "a" * 32,
            "retry_after_epoch": self.clock.now - 1,
            "sweep_deadline_epoch": self.clock.now - 60,
        }
        self.shared_state.get_db("filecrypt_sweep_state").update_store(
            "filecrypt", json.dumps(header)
        )

        self.assertEqual([], cooling_crypters(self.shared_state, clock=self.clock))

    def test_the_later_of_the_two_deadlines_wins_when_both_exist(self):
        # A crypter can carry a legacy row and a lifecycle header at once.
        # Whichever runs longer is the one actually gating handouts.
        self._cool("filecrypt")
        legacy = cooling_crypters(self.shared_state, clock=self.clock)[0]
        header = {
            "schema_version": 1,
            "state": "cooldown",
            "generation_id": "a" * 32,
            "retry_after_epoch": legacy["retry_after_epoch"] + 3600,
            "sweep_deadline_epoch": self.clock.now - 60,
        }
        self.shared_state.get_db("filecrypt_sweep_state").update_store(
            "filecrypt", json.dumps(header)
        )

        cooling = cooling_crypters(self.shared_state, clock=self.clock)

        self.assertEqual(1, len(cooling))
        self.assertEqual(
            legacy["retry_after_epoch"] + 3600, cooling[0]["retry_after_epoch"]
        )

    def test_a_malformed_lifecycle_header_never_breaks_the_answer(self):
        self.shared_state.get_db("filecrypt_sweep_state").update_store(
            "filecrypt", "{not json"
        )

        self.assertEqual([], cooling_crypters(self.shared_state, clock=self.clock))

    def test_an_unreadable_or_unknown_row_is_skipped_instead_of_raising(self):
        self._cool("filecrypt")
        database = self.shared_state.get_db("crypter_cooldowns")
        database.rows["not_a_crypter"] = "{}"
        database.rows["tolink"] = "{not json"

        cooling = cooling_crypters(self.shared_state, clock=self.clock)

        self.assertEqual(["filecrypt"], [row["crypter"] for row in cooling])


class LifecycleHeldOperatorActionsTests(unittest.TestCase):
    """ "Check now" and "Remove" must reach a package the LIFECYCLE holds.

    A version-two hold lives in `filecrypt_link_states`, keyed by link
    fingerprint - the protected blob carries no `deferred` block at all. Both
    operator actions used to gate on that block, so on a lifecycle install
    (where no package has one) every request was rejected `not_deferred` and
    the two buttons did nothing whatsoever. Measured on a live instance: 0 of
    413 protected packages carried the legacy key while 101 link-state rows
    held them.
    """

    def setUp(self):
        self.clock = FakeClock(1_700_000_000)
        self.shared_state = FakeSharedState()
        self.shared_state.values["crypter_block_mode"] = "defer"
        self.service = CrypterCooldownService(self.shared_state, clock=self.clock)
        # A protected package with links but WITHOUT any defer block.
        self.shared_state.get_db("protected").update_store(
            PACKAGE_A,
            json.dumps(
                {
                    "title": "Synthetic.Release.2031",
                    "links": [["https://filecrypt.invalid/Container/ABC123", "x"]],
                    "password": "",
                    "size_mb": 1,
                }
            ),
        )

    def _hold_the_link(self):
        """Put the package's only link under a lifecycle hold."""
        from quasarr.providers.crypter_candidates import link_fingerprint

        fingerprint = link_fingerprint(
            "filecrypt", "https://filecrypt.invalid/Container/ABC123"
        )
        self.shared_state.get_db("filecrypt_link_states").update_store(
            fingerprint,
            json.dumps(
                {
                    "schema_version": 1,
                    "state": "held",
                    "first_blocked_epoch": self.clock.now - 3600,
                    "retry_after_epoch": self.clock.now + 7200,
                    "lease": None,
                }
            ),
        )
        return fingerprint

    def _defer_block(self):
        raw = self.shared_state.get_db("protected").rows[PACKAGE_A]
        return json.loads(raw).get("deferred")

    def test_a_lifecycle_held_package_accepts_a_probe_request(self):
        self._hold_the_link()

        answer = self.service.request_probe([PACKAGE_A])

        self.assertEqual([PACKAGE_A], answer["requested"])
        self.assertEqual([], answer["rejected"])
        self.assertTrue(self._defer_block()["probe_requested"])

    def test_the_queued_marker_never_holds_or_displays_the_package_itself(self):
        # The marker exists only so the handout can see a queued probe. If it
        # also read as an active hold it would outrank the lifecycle
        # projection and replace the row's real evidence and sweep figures.
        self._hold_the_link()
        self.service.request_probe([PACKAGE_A])

        block = self._defer_block()
        self.assertEqual(0, block["retry_after_epoch"])
        decoded = decode_package_defer({"deferred": block})
        self.assertFalse(package_defer_is_active(decoded, None, now=self.clock.now))

    def test_the_marker_covers_the_packages_link_so_a_probe_can_be_offered(self):
        fingerprint = self._hold_the_link()
        self.service.request_probe([PACKAGE_A])

        decoded = decode_package_defer({"deferred": self._defer_block()})
        self.assertTrue(package_defer_covers_fingerprint(decoded, fingerprint))

    def test_requesting_a_probe_twice_stays_idempotent(self):
        self._hold_the_link()
        self.service.request_probe([PACKAGE_A])
        first = self.shared_state.get_db("protected").rows[PACKAGE_A]

        answer = self.service.request_probe([PACKAGE_A])

        self.assertEqual([PACKAGE_A], answer["requested"])
        self.assertEqual(first, self.shared_state.get_db("protected").rows[PACKAGE_A])

    def test_a_package_nothing_holds_is_still_rejected(self):
        # No lifecycle hold and no legacy block: there is nothing to probe.
        answer = self.service.request_probe([PACKAGE_A])

        self.assertEqual([], answer["requested"])
        self.assertEqual(
            [{"package_id": PACKAGE_A, "reason": "not_deferred"}], answer["rejected"]
        )
        self.assertIsNone(self._defer_block())

    def test_a_lifecycle_held_package_can_be_deleted(self):
        self._hold_the_link()

        self.assertEqual("deleted", self.service.delete_deferred_package(PACKAGE_A))
        self.assertNotIn(PACKAGE_A, self.shared_state.get_db("protected").rows)

    def test_a_package_nothing_holds_is_not_deletable_through_this_path(self):
        self.assertEqual(
            "not_deferred", self.service.delete_deferred_package(PACKAGE_A)
        )
        self.assertIn(PACKAGE_A, self.shared_state.get_db("protected").rows)

    def test_a_missing_package_still_reports_not_found(self):
        self.assertEqual("not_found", self.service.delete_deferred_package(PACKAGE_B))


if __name__ == "__main__":
    unittest.main()
