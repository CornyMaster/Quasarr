# -*- coding: utf-8 -*-

import json
import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from bottle import Bottle, HTTPError

from quasarr.api.sponsors_helper import setup_sponsors_helper_routes
from quasarr.providers import shared_state as provider_shared_state
from quasarr.providers.crypter_cooldowns import CrypterCooldownService
from quasarr.providers.notifications.helpers.common import build_solved_data
from quasarr.providers.statistics import StatsHelper
from quasarr.storage.sqlite_database import DataBase

PACKAGE_A = "Quasarr_movies_00000000000000000000000000000000"
PACKAGE_B = "Quasarr_movies_11111111111111111111111111111111"
PACKAGE_C = "Quasarr_movies_22222222222222222222222222222222"
PACKAGE_D = "Quasarr_movies_33333333333333333333333333333333"
CRYPTER = "filecrypt"
REASON = "ip_block_suspected"
NOW = 1_700_000_000
OBSERVATION_WINDOW = 15 * 60
COOLDOWN_SECONDS = 24 * 60 * 60
OBSERVATIONS_KEY = "crypter_block_observations"
COOLDOWNS_KEY = "crypter_cooldowns"
PROBES_KEY = "crypter_probes"
DEFERRED_KEY = "deferred_packages"
FAILURE_KEYS = (
    "failed_downloads",
    "failed_decryptions_automatic",
    "failed_decryptions_manual",
)
SOLVER_KEYS = (
    "captcha_decryptions_automatic",
    "captcha_decryptions_manual",
)


def filecrypt_link(index=1):
    return [f"https://filecrypt.invalid/container/{index}", CRYPTER]


class MutableClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class RealDatabaseSharedState:
    """Shared state backed by the real SQLite layer, one connection per table."""

    def __init__(self, dbfile, block_mode="defer"):
        self._databases = {}
        self.values = {
            "dbfile": dbfile,
            "database": self.get_db,
            "crypter_cooldown_hours": 24,
            "crypter_block_mode": block_mode,
        }

    def get_db(self, table):
        if table not in self._databases:
            self._databases[table] = DataBase(table)
        return self._databases[table]

    def update(self, key, value):
        self.values[key] = value

    def close(self):
        for database in self._databases.values():
            database._conn.close()
        self._databases.clear()


class CrypterStatisticsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.dbfile = os.path.join(self.tmpdir.name, "Quasarr.db")
        self.original_values = provider_shared_state.values
        self.original_lock = provider_shared_state.lock
        provider_shared_state.values = {"dbfile": self.dbfile}
        provider_shared_state.lock = None
        self.addCleanup(self.restore_provider_shared_state)
        self.state = RealDatabaseSharedState(self.dbfile)
        self.addCleanup(self.state.close)
        self.clock = MutableClock(NOW)
        self.service = CrypterCooldownService(self.state, clock=self.clock)
        self.stats = StatsHelper(self.state, clock=self.clock)
        self.fail_mock = None
        self.notify_mock = None

    def restore_provider_shared_state(self):
        provider_shared_state.values = self.original_values
        provider_shared_state.lock = self.original_lock

    # --- fixtures -------------------------------------------------------

    def store_protected(self, package_id, links=None, title="Synthetic.Release"):
        self.state.get_db("protected").update_store(
            package_id,
            json.dumps(
                {
                    "title": title,
                    "links": links if links is not None else [filecrypt_link()],
                    "password": "",
                }
            ),
        )

    def counters(self):
        stats = self.stats.get_stats()
        return {
            key: stats[key]
            for key in (OBSERVATIONS_KEY, COOLDOWNS_KEY, PROBES_KEY, DEFERRED_KEY)
        }

    # --- route drivers --------------------------------------------------

    def call_route(self, rule, payload):
        app = Bottle()
        setup_sponsors_helper_routes(app)
        route = next(route for route in app.routes if route.rule == rule)

        with (
            mock.patch("quasarr.api.sponsors_helper.shared_state", self.state),
            mock.patch("quasarr.api.sponsors_helper.request", mock.Mock(json=payload)),
            mock.patch(
                "quasarr.api.sponsors_helper.CrypterCooldownService",
                return_value=self.service,
            ),
            mock.patch("quasarr.api.sponsors_helper.fail") as fail,
            mock.patch(
                "quasarr.api.sponsors_helper.update_release_notification"
            ) as notify,
        ):
            self.fail_mock = fail
            self.notify_mock = notify
            return route.callback()

    def report_block(self, package_id, fingerprint_character):
        return self.call_route(
            "/sponsors_helper/api/defer/",
            {
                "package_id": package_id,
                "crypter": CRYPTER,
                "reason_code": REASON,
                "link_fingerprint": fingerprint_character * 64,
            },
        )

    def report_clear(self, package_id):
        return self.call_route(
            "/sponsors_helper/api/crypter-access/",
            {"package_id": package_id, "crypter": CRYPTER, "access": "clear"},
        )

    def request_handout(self):
        return self.call_route(
            "/sponsors_helper/api/to_decrypt/",
            {
                "supported_urls": ["filecrypt.invalid"],
                "capabilities": ["crypter_defer_v1"],
            },
        )

    def report_block_series(self, reports):
        results = [
            self.report_block(package_id, character)
            for package_id, character in reports
        ]
        return results[-1]

    def enter_cooldown(self):
        """Drive the three distinct block reports that create one cooldown."""
        for package_id in (PACKAGE_A, PACKAGE_B, PACKAGE_C):
            self.store_protected(package_id)
        final = self.report_block_series(
            ((PACKAGE_A, "a"), (PACKAGE_B, "b"), (PACKAGE_C, "c"))
        )
        self.assertEqual("cooldown", final["state"])


class CrypterTransitionCounterTests(CrypterStatisticsTestCase):
    def test_new_counters_start_at_zero_and_only_transitions_are_persisted_rows(self):
        self.assertEqual(
            {
                OBSERVATIONS_KEY: 0,
                COOLDOWNS_KEY: 0,
                PROBES_KEY: 0,
                DEFERRED_KEY: 0,
            },
            self.counters(),
        )

        statistics_db = self.state.get_db("statistics")

        for key in (OBSERVATIONS_KEY, COOLDOWNS_KEY, PROBES_KEY):
            self.assertEqual("0", statistics_db.retrieve(key))
        # The gauge is derived on every read, so a stale row could only lie.
        self.assertIsNone(statistics_db.retrieve(DEFERRED_KEY))

    def test_counters_persist_across_a_fresh_database_view(self):
        self.store_protected(PACKAGE_A)
        self.report_block(PACKAGE_A, "a")

        restarted_state = RealDatabaseSharedState(self.dbfile)
        self.addCleanup(restarted_state.close)
        restarted_stats = StatsHelper(restarted_state, clock=self.clock)
        restarted = restarted_stats.get_stats()

        self.assertEqual(1, restarted[OBSERVATIONS_KEY])
        self.assertEqual(0, restarted[COOLDOWNS_KEY])
        self.assertEqual(1, restarted[DEFERRED_KEY])

    def test_first_block_report_counts_one_observation_and_no_cooldown(self):
        self.store_protected(PACKAGE_A)

        result = self.report_block(PACKAGE_A, "a")

        self.assertEqual("hold", result["instruction"])
        self.assertEqual(
            {
                OBSERVATIONS_KEY: 1,
                COOLDOWNS_KEY: 0,
                PROBES_KEY: 0,
                DEFERRED_KEY: 1,
            },
            self.counters(),
        )

    def test_duplicate_evidence_never_counts_a_second_observation(self):
        self.store_protected(PACKAGE_A)
        self.store_protected(PACKAGE_B)
        self.report_block(PACKAGE_A, "a")

        # Same package and fingerprint, same package with a new fingerprint,
        # and a new package replaying a known fingerprint are all duplicates.
        self.report_block(PACKAGE_A, "a")
        self.report_block(PACKAGE_A, "b")
        self.report_block(PACKAGE_B, "a")

        self.assertEqual(1, self.counters()[OBSERVATIONS_KEY])
        self.assertEqual(0, self.counters()[COOLDOWNS_KEY])
        self.assertEqual(1, len(self.service.snapshot(CRYPTER)["observations"]))

    def test_three_distinct_observations_count_three_and_exactly_one_cooldown(self):
        self.enter_cooldown()

        self.assertEqual(
            {
                OBSERVATIONS_KEY: 3,
                COOLDOWNS_KEY: 1,
                PROBES_KEY: 0,
                DEFERRED_KEY: 3,
            },
            self.counters(),
        )

    def test_reports_during_an_active_cooldown_never_count_a_second_cooldown(self):
        self.enter_cooldown()
        self.store_protected(PACKAGE_D)

        # A repeated blocked report for a package already holding evidence,
        # then a genuinely new distinct package, both while cooling.
        self.report_block(PACKAGE_A, "a")
        self.assertEqual(3, self.counters()[OBSERVATIONS_KEY])

        self.report_block(PACKAGE_D, "d")

        self.assertEqual(4, self.counters()[OBSERVATIONS_KEY])
        self.assertEqual(1, self.counters()[COOLDOWNS_KEY])

    def test_a_new_cooldown_after_expiry_counts_again(self):
        self.enter_cooldown()
        self.clock.now = NOW + COOLDOWN_SECONDS + 1
        self.assertEqual("available", self.service.snapshot(CRYPTER)["state"])

        final = self.report_block_series(
            ((PACKAGE_A, "d"), (PACKAGE_B, "e"), (PACKAGE_C, "f"))
        )

        self.assertEqual("cooldown", final["state"])
        self.assertEqual(6, self.counters()[OBSERVATIONS_KEY])
        self.assertEqual(2, self.counters()[COOLDOWNS_KEY])


class CrypterProbeCounterTests(CrypterStatisticsTestCase):
    def test_requesting_a_probe_counts_nothing_until_it_is_spent(self):
        self.enter_cooldown()

        self.assertEqual(
            {"requested": [PACKAGE_A], "rejected": []},
            self.service.request_probe([PACKAGE_A]),
        )
        self.assertEqual(0, self.counters()[PROBES_KEY])

        # Re-requesting the queued probe rewrites nothing and counts nothing.
        self.service.request_probe([PACKAGE_A])
        self.assertEqual(0, self.counters()[PROBES_KEY])

        handout = self.request_handout()

        self.assertEqual(PACKAGE_A, handout["to_decrypt"]["id"])
        self.assertEqual(1, self.counters()[PROBES_KEY])

    def test_only_the_handed_out_package_spends_a_probe(self):
        self.enter_cooldown()
        self.service.request_probe([PACKAGE_A, PACKAGE_B])

        handout = self.request_handout()

        self.assertEqual(PACKAGE_A, handout["to_decrypt"]["id"])
        self.assertEqual(1, self.counters()[PROBES_KEY])
        self.assertTrue(self.service.get_package_defer(PACKAGE_B)["probe_requested"])

    def test_a_spent_probe_is_never_counted_twice(self):
        self.enter_cooldown()
        self.service.request_probe([PACKAGE_A])
        self.request_handout()

        with self.assertRaises(HTTPError) as context:
            self.request_handout()

        self.assertEqual(404, context.exception.status_code)
        self.assertEqual(1, self.counters()[PROBES_KEY])
        self.assertFalse(self.service.get_package_defer(PACKAGE_A)["probe_requested"])

    def test_a_repeated_blocked_probe_report_counts_no_new_transition(self):
        self.enter_cooldown()
        self.service.request_probe([PACKAGE_A])
        self.request_handout()

        # The probe ran and the linkcrypter is still blocked, so the helper
        # reports the same package again.
        self.report_block(PACKAGE_A, "a")

        self.assertEqual(
            {
                OBSERVATIONS_KEY: 3,
                COOLDOWNS_KEY: 1,
                PROBES_KEY: 1,
                DEFERRED_KEY: 3,
            },
            self.counters(),
        )


class DeferredPackageGaugeTests(CrypterStatisticsTestCase):
    def test_deferred_gauge_counts_only_active_holds(self):
        self.store_protected(PACKAGE_A)
        self.store_protected(PACKAGE_B)
        self.store_protected(PACKAGE_C, links=[["https://hoster.invalid/file", "rg"]])
        self.report_block(PACKAGE_A, "a")
        self.report_block(PACKAGE_B, "b")

        self.assertEqual(2, self.counters()[DEFERRED_KEY])

        self.clock.now = NOW + OBSERVATION_WINDOW + 1

        self.assertEqual(0, self.counters()[DEFERRED_KEY])

    def test_clearing_after_a_successful_probe_drops_the_gauge(self):
        self.enter_cooldown()
        self.service.request_probe([PACKAGE_A])
        self.request_handout()
        self.assertEqual(3, self.counters()[DEFERRED_KEY])

        self.assertEqual(
            {"success": True, "state": "available", "cleared": True},
            self.report_clear(PACKAGE_A),
        )

        # Clearing the linkcrypter leaves the other packages on their own
        # provisional deadlines instead of the crypter-wide one.
        self.assertEqual(2, self.counters()[DEFERRED_KEY])
        self.assertEqual(1, self.counters()[PROBES_KEY])

        self.clock.now = NOW + OBSERVATION_WINDOW + 1

        self.assertEqual(1, self.counters()[DEFERRED_KEY])

    def test_deleting_a_deferred_package_drops_the_gauge(self):
        self.enter_cooldown()

        self.assertEqual("deleted", self.service.delete_deferred_package(PACKAGE_A))

        self.assertEqual(2, self.counters()[DEFERRED_KEY])
        self.assertEqual(3, self.counters()[OBSERVATIONS_KEY])

    def test_gauge_never_goes_negative_when_clears_repeat(self):
        self.enter_cooldown()
        for package_id in (PACKAGE_A, PACKAGE_B, PACKAGE_C):
            self.service.clear_package_defer(package_id)
            self.service.clear_package_defer(package_id)

        self.assertEqual(0, self.counters()[DEFERRED_KEY])

        self.service.delete_deferred_package(PACKAGE_A)

        self.assertEqual(0, self.counters()[DEFERRED_KEY])

    def test_fail_block_mode_reports_no_deferred_packages_and_writes_nothing(self):
        self.enter_cooldown()
        protected_db = self.state.get_db("protected")
        stored = {
            package_id: protected_db.retrieve(package_id)
            for package_id in (PACKAGE_A, PACKAGE_B, PACKAGE_C)
        }
        cooldown_row = self.state.get_db("crypter_cooldowns").retrieve(CRYPTER)

        self.state.values["crypter_block_mode"] = "fail"

        self.assertEqual(0, self.counters()[DEFERRED_KEY])
        self.assertEqual(3, self.counters()[OBSERVATIONS_KEY])
        self.assertEqual(
            stored,
            {
                package_id: protected_db.retrieve(package_id)
                for package_id in (PACKAGE_A, PACKAGE_B, PACKAGE_C)
            },
        )
        self.assertEqual(
            cooldown_row, self.state.get_db("crypter_cooldowns").retrieve(CRYPTER)
        )


class DeferNonEffectTests(CrypterStatisticsTestCase):
    def test_defer_transitions_never_touch_failure_statistics_or_notifications(self):
        self.enter_cooldown()
        self.report_block(PACKAGE_A, "a")
        stats = self.stats.get_stats()

        for key in FAILURE_KEYS + SOLVER_KEYS + ("packages_downloaded",):
            with self.subTest(key=key):
                self.assertEqual(0, stats[key])
        self.fail_mock.assert_not_called()
        self.notify_mock.assert_not_called()
        self.assertIsNone(self.state.get_db("failed").retrieve(PACKAGE_A))

    def test_clear_reports_never_touch_failure_statistics_or_notifications(self):
        self.enter_cooldown()

        self.report_clear(PACKAGE_A)
        stats = self.stats.get_stats()

        for key in FAILURE_KEYS + SOLVER_KEYS:
            with self.subTest(key=key):
                self.assertEqual(0, stats[key])
        self.fail_mock.assert_not_called()
        self.notify_mock.assert_not_called()

    def test_solver_statistics_contract_still_reads_the_name_key(self):
        solved = build_solved_data(
            {"solvers": [{"name": "2Captcha", "attempts": 2, "cost": 0.5}]}
        )

        self.assertEqual(
            ["2Captcha"],
            [entry["solver_display"] for entry in solved["solvers"]],
        )
        self.assertEqual(2, solved["solvers"][0]["attempts"])

        renamed = build_solved_data(
            {"solvers": [{"provider": "2Captcha", "attempts": 2}]}
        )

        self.assertEqual(
            ["Unknown"],
            [entry["solver_display"] for entry in renamed["solvers"]],
        )


class CounterAtomicityTests(CrypterStatisticsTestCase):
    def test_a_competing_write_between_read_and_write_cannot_be_lost(self):
        statistics_db = self.state.get_db("statistics")
        self.stats.increment_crypter_block_observations()
        self.assertEqual("1", statistics_db.retrieve(OBSERVATIONS_KEY))
        competing = {"written": False}
        original_retrieve = DataBase.retrieve

        def retrieve_then_write(database, key):
            value = original_retrieve(database, key)
            if (
                database._table == "statistics"
                and key == OBSERVATIONS_KEY
                and not competing["written"]
            ):
                competing["written"] = True
                writer = DataBase("statistics")
                try:
                    writer.update_store(key, "100")
                finally:
                    writer._conn.close()
            return value

        with mock.patch.object(DataBase, "retrieve", retrieve_then_write):
            self.stats.increment_crypter_block_observations()

        # A read-then-write increment reads 1, lets the competing write commit
        # 100, and then stores 2 - losing a committed value.
        self.assertEqual(
            "101" if competing["written"] else "2",
            statistics_db.retrieve(OBSERVATIONS_KEY),
        )

    def test_concurrent_increments_from_separate_connections_are_all_recorded(self):
        connections = []
        guard = threading.Lock()

        def database(table):
            instance = DataBase(table)
            with guard:
                connections.append(instance)
            return instance

        state = SimpleNamespace(values={"database": database})
        errors = []

        def worker():
            try:
                helper = StatsHelper(state)
                for _ in range(4):
                    helper.increment_crypter_block_observations()
            except Exception as error:  # pragma: no cover - reported below
                errors.append(error)

        threads = [threading.Thread(target=worker) for _ in range(3)]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(60)

            self.assertEqual([], errors)
            for thread in threads:
                self.assertFalse(thread.is_alive())
            self.assertEqual(
                "12", self.state.get_db("statistics").retrieve(OBSERVATIONS_KEY)
            )
        finally:
            for connection in connections:
                connection._conn.close()


if __name__ == "__main__":
    unittest.main()
