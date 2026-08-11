# -*- coding: utf-8 -*-

import hashlib
import json
import threading
import unittest

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

    def retrieve(self, key):
        return self.rows.get(key)

    def update_store(self, key, value):
        self.rows[key] = value
        return True

    def mutate_value(self, key, mutator):
        with self.lock:
            self.mutation_count += 1
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
            "protected": FakeDatabase(),
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

    def _seed_protected_package(self, package_id=PACKAGE_A, probe_requested=False):
        protected = {
            "title": "Synthetic.Release",
            "links": [["https://filecrypt.invalid/container/1", "filecrypt"]],
            "password": "",
            "deferred": {
                "crypter": "filecrypt",
                "reason_code": REASON,
                "since_epoch": self.clock.now,
                "retry_after_epoch": self.clock.now + 24 * 60 * 60,
                "probe_requested": probe_requested,
                "observation_holds": 1,
            },
        }
        self.shared_state.databases["protected"].update_store(
            package_id, json.dumps(protected)
        )

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

    def test_duplicate_package_or_fingerprint_refreshes_without_new_evidence(self):
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
            self.clock.now - 10,
            next(
                item["seen_at_epoch"]
                for item in stored["observations"]
                if item["package_id"] == PACKAGE_A
            ),
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

    def test_probe_request_is_consumed_exactly_once(self):
        self._seed_protected_package()

        result = self.service.request_probe([PACKAGE_A])

        self.assertIsInstance(result, dict)
        self.assertEqual([PACKAGE_A], result["requested"])
        self.assertTrue(self.service.consume_probe(PACKAGE_A, "filecrypt"))
        self.assertFalse(self.service.consume_probe(PACKAGE_A, "filecrypt"))
        protected = json.loads(
            self.shared_state.databases["protected"].rows[PACKAGE_A]
        )
        self.assertFalse(protected["deferred"]["probe_requested"])

    def test_blocked_probe_restarts_full_cooldown(self):
        initial = self._enter_cooldown()
        self._seed_protected_package()
        self.service.request_probe([PACKAGE_A])
        self.assertTrue(self.service.consume_probe(PACKAGE_A, "filecrypt"))
        self.clock.now += 60 * 60

        restarted = self._observe(PACKAGE_A, "a")

        self.assertGreater(
            restarted["package_retry_after_epoch"],
            initial["package_retry_after_epoch"],
        )
        self.assertEqual(
            self.clock.now + 24 * 60 * 60,
            restarted["package_retry_after_epoch"],
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

    def test_persisted_schema_rejects_probe_state_and_probe_fields(self):
        database = self.shared_state.databases["crypter_cooldowns"]
        self._observe(PACKAGE_A, "a")
        valid_record = json.loads(database.rows["filecrypt"])
        invalid_records = []

        probe_state = dict(valid_record)
        probe_state["state"] = "probe"
        invalid_records.append(probe_state)

        probe_field = dict(valid_record)
        probe_field["probe_requested"] = True
        invalid_records.append(probe_field)

        for record in invalid_records:
            with self.subTest(record=record):
                raw_record = json.dumps(record)
                database.rows["filecrypt"] = raw_record

                with self.assertRaises(ValueError):
                    self.service.snapshot("filecrypt")

                self.assertEqual(raw_record, database.rows["filecrypt"])


if __name__ == "__main__":
    unittest.main()
