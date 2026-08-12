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
    normalize_crypter_key,
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
    def __init__(self):
        self.rows = {}
        self.lock = threading.Lock()
        self.mutation_count = 0
        self.retrieve_count = 0
        self.before_mutation = None

    def retrieve(self, key):
        self.retrieve_count += 1
        return self.rows.get(key)

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


class FakeSharedState:
    def __init__(self, cooldown_hours=24):
        self.values = {"crypter_cooldown_hours": cooldown_hours}
        self.databases = {
            "crypter_cooldowns": FakeDatabase(),
        }

    def get_db(self, table):
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
        decision = self.service.observe(
            "filecrypt", PACKAGE_A, "a" * 64, REASON
        )

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

    def test_invalid_observations_raise_without_database_writes(self):
        invalid = (
            ("hide", PACKAGE_A, "a" * 64, REASON),
            ("filecrypt", "invalid-package", "a" * 64, REASON),
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
        self.assertNotIn(
            "future_observation_field", snapshot["observations"][0]
        )
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

        with patch(
            "quasarr.providers.crypter_cooldowns.warn", create=True
        ) as warning:
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


if __name__ == "__main__":
    unittest.main()
