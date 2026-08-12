# -*- coding: utf-8 -*-

import json
import threading
import unittest
from unittest.mock import MagicMock, patch

from quasarr.downloads.packages import delete_database_packages, get_packages
from quasarr.providers.crypter_cooldowns import CrypterCooldownService

PACKAGE_A = "Quasarr_movies_00000000000000000000000000000000"
PACKAGE_B = "Quasarr_movies_11111111111111111111111111111111"
PACKAGE_C = "Quasarr_movies_22222222222222222222222222222222"
PACKAGE_D = "Quasarr_movies_33333333333333333333333333333333"
REASON = "ip_block_suspected"
NOW = 1_700_000_000
PROVISIONAL_WINDOW = 15 * 60


class FakeClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class FakeDatabase:
    def __init__(self, frozen=False):
        self.rows = {}
        self.lock = threading.Lock()
        self.frozen = frozen
        self.mutation_count = 0
        self.retrieve_count = 0

    def retrieve(self, key):
        self.retrieve_count += 1
        return self.rows.get(key)

    def retrieve_all_titles(self):
        items = [[key, value] for key, value in sorted(self.rows.items())]
        return items if items else None

    def update_store(self, key, value):
        self.rows[key] = value
        return True

    def delete(self, key):
        if not self.frozen:
            self.rows.pop(key, None)
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
    def __init__(self, frozen_protected=False):
        self.values = {"crypter_cooldown_hours": 24}
        self.databases = {
            "protected": FakeDatabase(frozen=frozen_protected),
            "failed": FakeDatabase(),
            "crypter_cooldowns": FakeDatabase(),
        }
        self.device_calls = 0

    def get_db(self, table):
        return self.databases[table]

    def get_device(self):
        self.device_calls += 1
        return MagicMock()


def protected_blob(title="Synthetic.Release.Example"):
    return {
        "title": title,
        "links": [["https://filecrypt.invalid/container/1", "filecrypt"]],
        "password": "synthetic",
        "size_mb": 1024,
        "original_url": "https://source.invalid/release",
        "imdb_id": "tt0000000",
        "notifications": [{"channel": "discord", "message_id": "1"}],
    }


class DeferredPackageMetadataTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(NOW)
        self.shared_state = FakeSharedState()
        self.protected = self.shared_state.databases["protected"]
        self.protected.update_store(PACKAGE_A, json.dumps(protected_blob()))
        self.service = CrypterCooldownService(self.shared_state, clock=self.clock)

    def _stored(self, package_id=PACKAGE_A):
        return json.loads(self.protected.rows[package_id])

    def test_defer_writes_only_the_defined_schema(self):
        deferred = self.service.defer_package(
            PACKAGE_A, "filecrypt", REASON, NOW + PROVISIONAL_WINDOW, 1
        )

        expected = {
            "crypter": "filecrypt",
            "reason_code": REASON,
            "since_epoch": NOW,
            "retry_after_epoch": NOW + PROVISIONAL_WINDOW,
            "probe_requested": False,
            "observation_holds": 1,
        }
        self.assertEqual(expected, deferred)
        self.assertEqual(expected, self._stored()["deferred"])

    def test_defer_preserves_every_existing_protected_field(self):
        original = protected_blob()

        self.service.defer_package(
            PACKAGE_A, "filecrypt", REASON, NOW + PROVISIONAL_WINDOW, 1
        )

        stored = self._stored()
        self.assertEqual(original, {k: v for k, v in stored.items() if k != "deferred"})

    def test_defer_rejects_invalid_arguments_without_writing(self):
        invalid_calls = (
            ("not-a-package-id", "filecrypt", REASON, NOW, 1),
            (PACKAGE_A, "hide", REASON, NOW, 1),
            (PACKAGE_A, "filecrypt", "unsupported_reason", NOW, 1),
            (PACKAGE_A, "filecrypt", REASON, -1, 1),
            (PACKAGE_A, "filecrypt", REASON, NOW, -1),
        )

        for call in invalid_calls:
            with self.subTest(call=call):
                with self.assertRaises(ValueError):
                    self.service.defer_package(*call)

        self.assertEqual(0, self.protected.mutation_count)
        self.assertNotIn("deferred", self._stored())

    def test_defer_never_creates_a_missing_protected_package(self):
        self.assertIsNone(
            self.service.defer_package(
                PACKAGE_B, "filecrypt", REASON, NOW + PROVISIONAL_WINDOW, 1
            )
        )
        self.assertNotIn(PACKAGE_B, self.protected.rows)

    def test_get_package_defer_reads_without_mutating(self):
        self.assertIsNone(self.service.get_package_defer(PACKAGE_A))

        self.service.defer_package(
            PACKAGE_A, "filecrypt", REASON, NOW + PROVISIONAL_WINDOW, 1
        )
        mutations = self.protected.mutation_count

        self.assertEqual(
            self._stored()["deferred"], self.service.get_package_defer(PACKAGE_A)
        )
        self.assertIsNone(self.service.get_package_defer(PACKAGE_B))
        self.assertEqual(mutations, self.protected.mutation_count)

    def test_malformed_defer_metadata_is_ignored_without_losing_the_package(self):
        blob = protected_blob()
        blob["deferred"] = {"crypter": "filecrypt", "observation_holds": "one"}
        self.protected.update_store(PACKAGE_A, json.dumps(blob))

        self.assertIsNone(self.service.get_package_defer(PACKAGE_A))
        self.assertIn(PACKAGE_A, self.protected.rows)
        self.assertEqual(blob["title"], self._stored()["title"])

    def test_clear_package_defer_removes_only_the_deferred_block(self):
        self.service.defer_package(
            PACKAGE_A, "filecrypt", REASON, NOW + PROVISIONAL_WINDOW, 1
        )

        self.assertTrue(self.service.clear_package_defer(PACKAGE_A))
        self.assertEqual(protected_blob(), self._stored())
        self.assertFalse(self.service.clear_package_defer(PACKAGE_A))
        self.assertFalse(self.service.clear_package_defer(PACKAGE_B))

    def test_expired_hold_keeps_one_observation_hold_forever(self):
        first = self.service.defer_package(
            PACKAGE_A, "filecrypt", REASON, NOW + PROVISIONAL_WINDOW, 1
        )
        self.assertEqual(NOW + PROVISIONAL_WINDOW, first["retry_after_epoch"])

        # The provisional hold expires, which restores selection eligibility.
        self.clock.now = NOW + PROVISIONAL_WINDOW + 1
        expired = self.service.get_package_defer(PACKAGE_A)
        self.assertEqual(1, expired["observation_holds"])
        self.assertEqual(NOW + PROVISIONAL_WINDOW, expired["retry_after_epoch"])

        # A second isolated BLOCKED result must not buy another provisional hold.
        second = self.service.defer_package(
            PACKAGE_A,
            "filecrypt",
            REASON,
            self.clock.now + PROVISIONAL_WINDOW,
            1,
        )
        self.assertEqual(
            {
                "crypter": "filecrypt",
                "reason_code": REASON,
                "since_epoch": NOW,
                "retry_after_epoch": NOW + PROVISIONAL_WINDOW,
                "probe_requested": False,
                "observation_holds": 1,
            },
            second,
        )

    def test_confirmed_cooldown_still_extends_an_exhausted_package_hold(self):
        self.service.defer_package(
            PACKAGE_A, "filecrypt", REASON, NOW + PROVISIONAL_WINDOW, 1
        )

        # A crypter-wide cooldown consumes no provisional hold budget, so it may
        # extend the deadline even after the package spent its single hold.
        cooldown = self.service.defer_package(
            PACKAGE_A, "filecrypt", REASON, NOW + 24 * 60 * 60, 0
        )

        self.assertEqual(NOW + 24 * 60 * 60, cooldown["retry_after_epoch"])
        self.assertEqual(1, cooldown["observation_holds"])
        self.assertEqual(NOW, cooldown["since_epoch"])


class DeferredQueueProjectionTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(NOW)
        self.shared_state = FakeSharedState()
        self.protected = self.shared_state.databases["protected"]
        self.protected.update_store(PACKAGE_A, json.dumps(protected_blob()))
        self.service = CrypterCooldownService(self.shared_state, clock=self.clock)

    def _get_packages(self):
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
            return get_packages(self.shared_state, auto_start=False)

    def _enter_crypter_cooldown(self, observed_at):
        self.clock.now = observed_at
        for package_id, fingerprint in (
            (PACKAGE_B, "b"),
            (PACKAGE_C, "c"),
            (PACKAGE_D, "d"),
        ):
            self.service.observe("filecrypt", package_id, fingerprint * 64, REASON)

    def test_untouched_protected_package_keeps_its_existing_projection(self):
        downloads = self._get_packages()

        item = downloads["queue"][0]
        self.assertEqual("protected", item["type"])
        self.assertEqual(
            "[CAPTCHA not solved!] Synthetic.Release.Example", item["filename"]
        )
        self.assertNotIn("deferred", item)
        # Packages without defer metadata must not read cooldown state at all.
        self.assertEqual(
            0, self.shared_state.databases["crypter_cooldowns"].retrieve_count
        )

    def test_deferred_package_stays_in_queue_and_never_in_history(self):
        self.service.defer_package(
            PACKAGE_A, "filecrypt", REASON, NOW + PROVISIONAL_WINDOW, 1
        )

        downloads = self._get_packages()

        self.assertEqual([], downloads["history"])
        self.assertEqual({}, self.shared_state.databases["failed"].rows)
        self.assertEqual(1, len(downloads["queue"]))
        item = downloads["queue"][0]
        self.assertEqual(PACKAGE_A, item["nzo_id"])
        self.assertEqual("protected", item["type"])
        self.assertEqual("movies", item["cat"])
        self.assertEqual(
            "[Waiting for linkcrypter retry] Synthetic.Release.Example",
            item["filename"],
        )
        self.assertEqual(
            {
                "crypter": "filecrypt",
                "reason_code": REASON,
                "since_epoch": NOW,
                "retry_after_epoch": NOW + PROVISIONAL_WINDOW,
                "probe_requested": False,
                "observation_holds": 1,
                "state": "available",
                "evidence_count": 0,
                "hold_type": "provisional",
                "active": True,
            },
            item["deferred"],
        )

    def test_expired_provisional_hold_restores_the_normal_projection(self):
        self.service.defer_package(
            PACKAGE_A, "filecrypt", REASON, NOW + PROVISIONAL_WINDOW, 1
        )
        self.clock.now = NOW + PROVISIONAL_WINDOW + 1

        item = self._get_packages()["queue"][0]

        self.assertEqual(
            "[CAPTCHA not solved!] Synthetic.Release.Example", item["filename"]
        )
        self.assertFalse(item["deferred"]["active"])
        self.assertEqual("none", item["deferred"]["hold_type"])
        self.assertEqual(1, item["deferred"]["observation_holds"])

    def test_crypter_cooldown_supersedes_an_expired_package_hold(self):
        self.service.defer_package(
            PACKAGE_A, "filecrypt", REASON, NOW + PROVISIONAL_WINDOW, 1
        )
        self._enter_crypter_cooldown(NOW + 600)
        self.clock.now = NOW + PROVISIONAL_WINDOW + 1

        item = self._get_packages()["queue"][0]

        self.assertEqual(
            "[Waiting for linkcrypter retry] Synthetic.Release.Example",
            item["filename"],
        )
        self.assertEqual("cooldown", item["deferred"]["state"])
        self.assertEqual("crypter_cooldown", item["deferred"]["hold_type"])
        self.assertEqual(3, item["deferred"]["evidence_count"])
        self.assertEqual(
            NOW + 600 + 24 * 60 * 60, item["deferred"]["retry_after_epoch"]
        )
        # The projection is dynamic; it never rewrites the package metadata.
        self.assertEqual(
            NOW + PROVISIONAL_WINDOW,
            json.loads(self.protected.rows[PACKAGE_A])["deferred"]["retry_after_epoch"],
        )

    def test_malformed_defer_metadata_still_renders_the_package(self):
        blob = protected_blob()
        blob["deferred"] = {"crypter": "unknown-crypter"}
        self.protected.update_store(PACKAGE_A, json.dumps(blob))

        item = self._get_packages()["queue"][0]

        self.assertEqual(
            "[CAPTCHA not solved!] Synthetic.Release.Example", item["filename"]
        )
        self.assertNotIn("deferred", item)


class DatabasePackageDeletionTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(NOW)
        self.shared_state = FakeSharedState()
        self.protected = self.shared_state.databases["protected"]
        self.failed = self.shared_state.databases["failed"]
        self.protected.update_store(PACKAGE_A, json.dumps(protected_blob()))
        self.protected.update_store(PACKAGE_B, json.dumps(protected_blob()))
        self.failed.update_store(PACKAGE_A, json.dumps({"title": "stale"}))
        self.service = CrypterCooldownService(self.shared_state, clock=self.clock)
        self.service.defer_package(
            PACKAGE_A, "filecrypt", REASON, NOW + PROVISIONAL_WINDOW, 1
        )

    def test_batch_delete_handles_mixed_ids_without_touching_jdownloader(self):
        result = delete_database_packages(
            self.shared_state,
            [PACKAGE_A, PACKAGE_B, PACKAGE_C, "not-a-package-id", PACKAGE_A],
        )

        self.assertEqual(
            {
                "deleted": [PACKAGE_A],
                "rejected": [
                    {"package_id": PACKAGE_B, "reason": "not_deferred"},
                    {"package_id": PACKAGE_C, "reason": "not_found"},
                    {
                        "package_id": "not-a-package-id",
                        "reason": "invalid_package_id",
                    },
                    {"package_id": PACKAGE_A, "reason": "duplicate"},
                ],
            },
            result,
        )
        self.assertNotIn(PACKAGE_A, self.protected.rows)
        self.assertNotIn(PACKAGE_A, self.failed.rows)
        self.assertIn(PACKAGE_B, self.protected.rows)
        self.assertEqual(0, self.shared_state.device_calls)

    def test_batch_delete_verifies_each_id_against_the_database(self):
        shared_state = FakeSharedState(frozen_protected=True)
        shared_state.databases["protected"].update_store(
            PACKAGE_A, json.dumps(protected_blob())
        )
        CrypterCooldownService(shared_state, clock=self.clock).defer_package(
            PACKAGE_A, "filecrypt", REASON, NOW + PROVISIONAL_WINDOW, 1
        )

        result = delete_database_packages(shared_state, [PACKAGE_A])

        self.assertEqual(
            {
                "deleted": [],
                "rejected": [{"package_id": PACKAGE_A, "reason": "delete_failed"}],
            },
            result,
        )
        self.assertIn(PACKAGE_A, shared_state.databases["protected"].rows)

    def test_batch_delete_accepts_only_protected_packages(self):
        with self.assertRaises(ValueError):
            delete_database_packages(self.shared_state, [PACKAGE_A], "failed")

        self.assertIn(PACKAGE_A, self.protected.rows)


if __name__ == "__main__":
    unittest.main()
