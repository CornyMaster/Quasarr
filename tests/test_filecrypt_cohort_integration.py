# -*- coding: utf-8 -*-

"""Filecrypt cohort sweeps driven through the real routes and real SQLite.

Every case here calls the shipped Bottle callbacks - the SponsorsHelper handout
and report routes plus the operator probe route - against a real SQLite file in
a temporary directory. Nothing about the sweep, the holds, the counters, or the
terminal operations is simulated: the only doubles are the clock, the identity
factory, and the JDownloader device, which is the one boundary these routes are
not allowed to reach in a test.

The unit suites own the pure protocol and the transition layer. What is proven
here is the composition: that a handout, its report, and the stored rows agree
across a whole sweep, that a CLEAR anywhere in it really ends every hold and
really queues the exact fingerprints it invalidated, and that the terminal
confirmation of the package a sweep handed out is still exactly once.
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from bottle import Bottle, HTTPResponse

import quasarr.api.packages as packages_api
import quasarr.api.sponsors_helper as sponsors_helper_api
from quasarr.api.packages import setup_packages_routes
from quasarr.api.sponsors_helper import setup_sponsors_helper_routes
from quasarr.api.sponsors_helper.cohort_protocol import (
    CRYPTER_DEFER_CAPABILITY,
    FILECRYPT_COHORT_CAPABILITY,
    terminal_operation_id,
)
from quasarr.providers import shared_state as provider_shared_state
from quasarr.providers.crypter_candidates import (
    enumerate_filecrypt_candidates,
    link_fingerprint,
)
from quasarr.providers.crypter_cooldowns import CrypterCooldownService
from quasarr.providers.crypter_sweeps import (
    MAXIMUM_COHORT_SIZE,
    MINIMUM_CONCLUSIVE_COHORT_SIZE,
    OVERSIZED_COHORT_SENTINEL,
    SWEEP_WINDOW_SECONDS,
)
from quasarr.providers.terminal_operations import (
    TERMINAL_OPERATION_MARKER,
    submission_comment,
)
from quasarr.storage.sqlite_database import DataBase

NOW = 1_700_000_000
CRYPTER = "filecrypt"
OTHER_CRYPTER = "keeplinks"
REASON = "ip_block_suspected"
COOLDOWN_HOURS = 24
CAPABILITIES = [CRYPTER_DEFER_CAPABILITY, FILECRYPT_COHORT_CAPABILITY]
DEFER_ONLY_CAPABILITIES = [CRYPTER_DEFER_CAPABILITY]

DECRYPT_RULE = "/sponsors_helper/api/to_decrypt/"
DEFER_RULE = "/sponsors_helper/api/defer/"
ACCESS_RULE = "/sponsors_helper/api/crypter-access/"
DOWNLOAD_RULE = "/sponsors_helper/api/download/"
PROBE_RULE = "/api/packages/deferred/probe"

SUPPORTED_URLS = ["filecrypt.invalid", "keeplinks.invalid"]
HOSTER_URL = "https://hoster.invalid/file/1"


def package(index):
    return f"Quasarr_movies_{index:032x}"


def filecrypt_url(index):
    return f"https://filecrypt.invalid/container/{index}"


def other_crypter_url(index):
    return f"https://keeplinks.invalid/p/{index}"


def fingerprint_of(url, crypter=CRYPTER):
    return link_fingerprint(crypter, url)


def protected_blob(links, title="Synthetic.Release.2024.1080p", **extra):
    data = {"title": title, "password": "", "links": links}
    data.update(extra)
    return json.dumps(data)


def filecrypt_link(index):
    return [filecrypt_url(index), "he"]


class FakeClock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now


class SequentialIds:
    """Deterministic 32-hex sweep, generation and offer identities."""

    def __init__(self):
        self.count = 0

    def __call__(self):
        self.count += 1
        return f"{self.count:032x}"


class FakeLinkgrabber:
    def __init__(self, device):
        self.device = device

    def add_links(self, params=None):
        self.device.add_links_calls.append(params)
        if not self.device.add_links_succeeds:
            return False
        self.device.packages.append(
            {"uuid": len(self.device.packages) + 1, "comment": params[0]["comment"]}
        )
        return True

    def query_packages(self, params=None):
        return list(self.device.packages)

    def query_links(self, params=None):
        return list(self.device.links)


class FakeDownloads:
    def __init__(self, device):
        self.device = device

    def query_packages(self, params=None):
        return list(self.device.downloader_packages)

    def query_links(self, params=None):
        return list(self.device.downloader_links)


class FakeDevice:
    """The one boundary these routes may not really reach."""

    def __init__(self):
        self.packages = []
        self.links = []
        self.downloader_packages = []
        self.downloader_links = []
        self.add_links_calls = []
        self.add_links_succeeds = True
        self.linkgrabber = FakeLinkgrabber(self)
        self.downloads = FakeDownloads(self)


class RealSharedState:
    """Shared state whose every table is a real SQLite table of one file."""

    def __init__(self, dbfile, mode="defer"):
        self._databases = {}
        self.device = FakeDevice()
        self.device_available = True
        self.values = {
            "dbfile": dbfile,
            "database": self.get_db,
            "crypter_block_mode": mode,
            "crypter_cooldown_hours": COOLDOWN_HOURS,
            "helper_active": True,
            "helper_last_seen": 0,
            "external_address": "http://quasarr.invalid",
        }

    def get_db(self, table):
        if table not in self._databases:
            self._databases[table] = DataBase(table)
        return self._databases[table]

    def update(self, key, value):
        self.values[key] = value

    def run_device_request(self, request_name, request_fn, default=None):
        if not self.device_available:
            return default
        return request_fn(self.device)

    def close(self):
        for database in self._databases.values():
            database._conn.close()
        self._databases.clear()


def route_for(rule, setup=setup_sponsors_helper_routes):
    app = Bottle()
    setup(app)
    return next(route for route in app.routes if route.rule == rule)


class CohortIntegrationTestCase(unittest.TestCase):
    """One real database, one real route table, one injected clock."""

    mode = "defer"

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dbfile = os.path.join(directory.name, "Quasarr.db")
        self.original_values = provider_shared_state.values
        self.original_lock = provider_shared_state.lock
        # `DataBase` resolves its file through the process-global shared state.
        provider_shared_state.values = {"dbfile": self.dbfile}
        provider_shared_state.lock = None
        self.clock = FakeClock(NOW)
        self.ids = SequentialIds()
        self.state = RealSharedState(self.dbfile, mode=self.mode)
        self.addCleanup(self.restore_shared_state)
        patcher = mock.patch.object(
            CrypterCooldownService, "_new_identifier", lambda _self: self.ids()
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.helper_routes = {
            rule: route_for(rule)
            for rule in (DECRYPT_RULE, DEFER_RULE, ACCESS_RULE, DOWNLOAD_RULE)
        }
        self.probe_route = route_for(PROBE_RULE, setup=setup_packages_routes)

    def restore_shared_state(self):
        self.state.close()
        provider_shared_state.values = self.original_values
        provider_shared_state.lock = self.original_lock

    # --- driving the real callbacks ---------------------------------------
    def _clocked_service(self, state):
        return CrypterCooldownService(state, clock=self.clock)

    def call(self, rule, payload):
        """Invoke one shipped route callback exactly as Bottle would."""
        if rule == PROBE_RULE:
            module, route = packages_api, self.probe_route
        else:
            module, route = sponsors_helper_api, self.helper_routes[rule]
        with (
            mock.patch.object(module, "shared_state", self.state),
            mock.patch.object(module, "request", mock.Mock(json=payload)),
            mock.patch.object(module, "CrypterCooldownService", self._clocked_service),
        ):
            return route.callback()

    def attempt(self, rule, payload):
        """The status and decoded body of one call, however it answered."""
        try:
            body = self.call(rule, payload)
        except HTTPResponse as response:
            raw = response.body
            try:
                return response.status_code, json.loads(raw)
            except (TypeError, ValueError):
                return response.status_code, raw
        if isinstance(body, HTTPResponse):
            return body.status_code, json.loads(body.body)
        return 200, body

    # --- fixtures ----------------------------------------------------------
    def store_packages(self, rows):
        protected = self.state.get_db("protected")
        for package_id, blob in rows.items():
            protected.update_store(package_id, blob)

    def store_filecrypt_cohort(self, count, start=1):
        self.store_packages(
            {
                package(index): protected_blob([filecrypt_link(index)])
                for index in range(start, start + count)
            }
        )

    def protected_row(self, package_id):
        raw = self.state.get_db("protected").retrieve(package_id)
        return None if raw is None else json.loads(raw)

    def decision_row(self):
        raw = self.state.get_db("crypter_cooldowns").retrieve(CRYPTER)
        return None if raw is None else json.loads(raw)

    def service(self):
        return CrypterCooldownService(self.state, clock=self.clock)

    def inventory(self):
        return enumerate_filecrypt_candidates(
            self.state.get_db("protected").retrieve_all_titles()
        )

    def owner_of(self, fingerprint):
        """The occurrence the inventory hands out first for one fingerprint.

        Real fingerprints are hash ordered, so which package owns a given
        member is never the order the packages were stored in.
        """
        return next(
            candidate.occurrences[0]
            for candidate in self.inventory().candidates
            if candidate.fingerprint == fingerprint
        )

    # --- protocol helpers --------------------------------------------------
    def handout(self, capabilities=CAPABILITIES, **extra):
        payload = {"supported_urls": list(SUPPORTED_URLS)}
        if capabilities is not None:
            payload["capabilities"] = list(capabilities)
        payload.update(extra)
        status, body = self.attempt(DECRYPT_RULE, payload)
        if status != 200:
            return None
        return body["to_decrypt"]

    def offered(self, **extra):
        handout = self.handout(**extra)
        self.assertIsNotNone(handout, "expected a handout")
        self.assertIn("crypter_offer", handout)
        return handout["crypter_offer"], handout

    def report_blocked(self, offer, package_id):
        return self.attempt(
            DEFER_RULE,
            {
                "package_id": package_id,
                "crypter": CRYPTER,
                "reason_code": REASON,
                "link_fingerprint": offer["link_fingerprint"],
                "sweep_id": offer["sweep_id"],
                "offer_id": offer["offer_id"],
            },
        )

    def report_access(self, offer, package_id, access="clear"):
        return self.attempt(
            ACCESS_RULE,
            {
                "package_id": package_id,
                "crypter": CRYPTER,
                "access": access,
                "link_fingerprint": offer["link_fingerprint"],
                "sweep_id": offer["sweep_id"],
                "offer_id": offer["offer_id"],
            },
        )

    def sweep(self, results):
        """Answer one offer per entry of `results` and return every exchange.

        `results` is a list of `"blocked"`, `"clear"` or `"unknown"`, so a
        sweep of five members with a CLEAR in third place is written literally
        as the sequence it is.
        """
        exchanges = []
        for access in results:
            offer, handout = self.offered()
            if access == "blocked":
                status, body = self.report_blocked(offer, handout["id"])
            else:
                status, body = self.report_access(offer, handout["id"], access)
            exchanges.append(
                {
                    "offer": offer,
                    "handout": handout,
                    "access": access,
                    "status": status,
                    "body": body,
                }
            )
        return exchanges


class CohortDenominatorTests(CohortIntegrationTestCase):
    """Which inventory sizes may ever authorize a linkcrypter-wide cooldown."""

    def assert_never_cools(self, count):
        self.store_filecrypt_cohort(count)
        answers = [exchange["body"] for exchange in self.sweep(["blocked"] * count)]
        self.assertNotIn(
            "cooldown",
            [answer["instruction"] for answer in answers],
            f"{count} unique fingerprints must never reach a cooldown",
        )
        self.assertNotEqual("cooldown", self.service().snapshot(CRYPTER)["state"])
        return answers

    def test_a_single_filecrypt_package_is_handled_individually(self):
        self.store_filecrypt_cohort(1)
        offer, handout = self.offered()
        self.assertEqual("individual", offer["mode"])
        status, body = self.report_blocked(offer, handout["id"])

        self.assertEqual(200, status)
        self.assertEqual("legacy_failure", body["instruction"])
        self.assertEqual("individual", body["state"])
        self.assertEqual(0, body["sweep_total"])
        self.assertNotEqual("cooldown", self.service().snapshot(CRYPTER)["state"])

    def test_four_unique_fingerprints_end_the_sweep_without_cooling(self):
        answers = self.assert_never_cools(4)

        self.assertEqual("legacy_failure", answers[-1]["instruction"])
        self.assertEqual("individual", answers[-1]["state"])
        self.assertEqual(4, answers[-1]["sweep_total"])
        self.assertEqual(4, answers[-1]["sweep_tested"])

    def test_five_unique_fingerprints_are_the_smallest_cooling_cohort(self):
        self.store_filecrypt_cohort(MINIMUM_CONCLUSIVE_COHORT_SIZE)
        answers = [
            exchange["body"]
            for exchange in self.sweep(["blocked"] * MINIMUM_CONCLUSIVE_COHORT_SIZE)
        ]

        self.assertEqual(
            ["hold", "hold", "hold", "hold", "cooldown"],
            [answer["instruction"] for answer in answers],
        )
        self.assertEqual(5, answers[-1]["sweep_total"])
        self.assertEqual(5, answers[-1]["sweep_tested"])
        self.assertEqual(5, answers[-1]["evidence_count"])
        self.assertEqual("crypter_cooldown", answers[-1]["hold_type"])
        self.assertEqual(NOW + COOLDOWN_HOURS * 3600, answers[-1]["retry_after_epoch"])
        self.assertEqual("cooldown", self.service().snapshot(CRYPTER)["state"])
        self.assertEqual(5, self.service().count_active_deferred_packages())

    def test_an_oversized_inventory_offers_nothing_and_reports_the_sentinel(self):
        self.store_filecrypt_cohort(MAXIMUM_COHORT_SIZE + 1)
        handout = self.handout()

        self.assertNotIn("crypter_offer", handout)
        stored = self.decision_row()
        self.assertEqual("individual", stored["state"])
        self.assertEqual("cohort_oversized", stored["reason"])
        status, body = self.attempt(
            DEFER_RULE,
            {
                "package_id": package(1),
                "crypter": CRYPTER,
                "reason_code": REASON,
                "link_fingerprint": fingerprint_of(filecrypt_url(1)),
                "sweep_id": stored["generation_id"],
                "offer_id": "f" * 32,
            },
        )

        self.assertEqual(200, status)
        self.assertEqual("stale", body["instruction"])
        self.assertEqual(OVERSIZED_COHORT_SENTINEL, body["sweep_total"])
        self.assertEqual(0, body["sweep_tested"])


class CompleteSweepTests(CohortIntegrationTestCase):
    """A whole all-blocked sweep, and what it leaves on disk."""

    def test_a_complete_all_blocked_sweep_holds_every_member(self):
        self.store_filecrypt_cohort(5)
        exchanges = self.sweep(["blocked"] * 5)

        offered = [exchange["offer"]["link_fingerprint"] for exchange in exchanges]
        self.assertEqual(5, len(set(offered)), "each member is offered exactly once")
        self.assertEqual(
            sorted(offered),
            sorted(candidate.fingerprint for candidate in self.inventory().candidates),
        )
        sweep_ids = {exchange["offer"]["sweep_id"] for exchange in exchanges}
        self.assertEqual(1, len(sweep_ids), "one generation owns the whole sweep")

        stored = self.decision_row()
        self.assertEqual("cooldown", stored["state"])
        self.assertEqual(5, stored["cohort_size"])
        self.assertEqual(
            ["blocked"] * 5, [member["result"] for member in stored["members"]]
        )
        for index in range(1, 6):
            deferred = self.protected_row(package(index))["deferred"]
            self.assertEqual(CRYPTER, deferred["crypter"])
            self.assertEqual(
                sweep_ids.pop() if not sweep_ids else deferred["sweep_id"],
                deferred["sweep_id"],
            )
        self.assertEqual(5, self.service().count_active_deferred_packages())

    def test_the_sweep_counters_advance_one_tested_member_at_a_time(self):
        self.store_filecrypt_cohort(5)
        exchanges = self.sweep(["blocked"] * 5)

        self.assertEqual(
            [1, 2, 3, 4, 5],
            [exchange["body"]["sweep_tested"] for exchange in exchanges],
        )
        self.assertEqual(
            [5] * 5, [exchange["body"]["sweep_total"] for exchange in exchanges]
        )
        # While the sweep runs the rendered deadline is its own window; the
        # concluding answer already describes the cooldown it opened.
        self.assertEqual(
            [NOW + SWEEP_WINDOW_SECONDS] * 4,
            [exchange["body"]["sweep_deadline_epoch"] for exchange in exchanges[:-1]],
        )
        self.assertEqual(
            NOW + COOLDOWN_HOURS * 3600, exchanges[-1]["body"]["sweep_deadline_epoch"]
        )

    def test_the_handout_carries_exactly_the_offered_container(self):
        self.store_filecrypt_cohort(5)
        offer, handout = self.offered()

        urls = [entry[0] for entry in handout["url"]]
        self.assertEqual(1, len(urls))
        self.assertEqual(offer["link_fingerprint"], fingerprint_of(urls[0]))
        self.assertEqual(
            terminal_operation_id(handout["id"]), handout["terminal_operation_id"]
        )
        self.assertNotIn("url", offer)


class ClearPositionTests(CohortIntegrationTestCase):
    """A validated CLEAR ends the sweep wherever in it it arrives."""

    def assert_clear_at(self, position):
        """Drive a five-member sweep whose `position`-th answer is a CLEAR."""
        self.store_filecrypt_cohort(5)
        results = ["blocked"] * (position - 1) + ["clear"]
        exchanges = self.sweep(results)
        clearing = exchanges[-1]

        self.assertEqual(200, clearing["status"])
        self.assertIs(True, clearing["body"]["cleared"])
        self.assertEqual("healthy", clearing["body"]["state"])
        self.assertEqual(0, clearing["body"]["sweep_tested"])
        self.assertEqual(0, clearing["body"]["sweep_total"])

        stored = self.decision_row()
        self.assertEqual("healthy", stored["state"])
        blocked = sorted(
            exchange["offer"]["link_fingerprint"] for exchange in exchanges[:-1]
        )
        self.assertEqual(blocked, stored["retest_members"])
        self.assertEqual(0, self.service().count_active_deferred_packages())
        for index in range(1, 6):
            self.assertIsNone(self.service().get_package_defer(package(index)))
        return exchanges

    def test_a_clear_ends_the_sweep_at_every_member_position(self):
        for position in range(1, 6):
            with self.subTest(position=position):
                self.setUp()
                self.assert_clear_at(position)

    def test_a_clear_invalidates_the_holds_the_same_sweep_had_written(self):
        exchanges = self.assert_clear_at(4)

        self.assertEqual(
            ["hold", "hold", "hold"],
            [exchange["body"]["instruction"] for exchange in exchanges[:-1]],
        )
        # Every hold really existed before the CLEAR removed it.
        self.assertEqual(
            3, len({exchange["handout"]["id"] for exchange in exchanges[:-1]})
        )

    def test_the_retest_queue_is_offered_in_exact_ascending_order(self):
        exchanges = self.assert_clear_at(5)
        expected = sorted(
            exchange["offer"]["link_fingerprint"] for exchange in exchanges[:-1]
        )

        offered = []
        for _ in range(len(expected)):
            offer, handout = self.offered()
            self.assertEqual("retest", offer["mode"])
            offered.append(offer["link_fingerprint"])
            self.report_blocked(offer, handout["id"])

        self.assertEqual(expected, offered)

    def test_a_clear_during_the_health_window_keeps_the_untested_retests(self):
        self.assert_clear_at(5)
        first, handout = self.offered()
        status, body = self.report_access(first, handout["id"], "clear")

        self.assertEqual(200, status)
        self.assertIs(True, body["cleared"])
        stored = self.decision_row()
        self.assertEqual("healthy", stored["state"])
        self.assertNotIn(first["link_fingerprint"], stored["retest_members"])
        self.assertEqual(3, len(stored["retest_members"]))


class InventoryShapeTests(CohortIntegrationTestCase):
    """Packages, links and fingerprints are not the same thing."""

    def test_two_filecrypt_links_of_one_package_are_two_cohort_members(self):
        self.store_packages(
            {
                package(1): protected_blob([filecrypt_link(1), filecrypt_link(2)]),
                package(2): protected_blob([filecrypt_link(3)]),
                package(3): protected_blob([filecrypt_link(4)]),
                package(4): protected_blob([filecrypt_link(5)]),
            }
        )
        exchanges = self.sweep(["blocked"] * 5)

        self.assertEqual(5, exchanges[-1]["body"]["sweep_total"])
        self.assertEqual("cooldown", exchanges[-1]["body"]["instruction"])
        served = [exchange["handout"]["id"] for exchange in exchanges]
        self.assertEqual(
            2,
            served.count(package(1)),
            "both links of one package are tested separately",
        )
        for exchange in exchanges:
            urls = [entry[0] for entry in exchange["handout"]["url"]]
            self.assertEqual(1, len(urls), "never two Filecrypt links at once")

    def test_one_fingerprint_in_two_packages_counts_once(self):
        shared = filecrypt_link(1)
        self.store_packages(
            {
                package(1): protected_blob([shared]),
                package(2): protected_blob([list(shared)]),
                package(3): protected_blob([filecrypt_link(2)]),
                package(4): protected_blob([filecrypt_link(3)]),
                package(5): protected_blob([filecrypt_link(4)]),
                package(6): protected_blob([filecrypt_link(5)]),
            }
        )
        self.assertEqual(5, len(self.inventory().candidates))
        exchanges = self.sweep(["blocked"] * 5)

        self.assertEqual(5, exchanges[-1]["body"]["sweep_total"])
        self.assertEqual("cooldown", exchanges[-1]["body"]["instruction"])
        duplicated = fingerprint_of(filecrypt_url(1))
        self.assertEqual(
            1,
            sum(
                1
                for exchange in exchanges
                if exchange["offer"]["link_fingerprint"] == duplicated
            ),
            "a duplicated fingerprint is offered once, not once per package",
        )
        # Both occurrences are held, because both are the tested container.
        for package_id in (package(1), package(2)):
            self.assertIsNotNone(self.service().get_package_defer(package_id))

    def test_an_alternative_linkcrypter_stays_eligible_under_a_cohort_hold(self):
        self.store_packages(
            {
                package(1): protected_blob(
                    [filecrypt_link(1), [other_crypter_url(1), "he"]]
                ),
                package(2): protected_blob([filecrypt_link(2)]),
                package(3): protected_blob([filecrypt_link(3)]),
                package(4): protected_blob([filecrypt_link(4)]),
                package(5): protected_blob([filecrypt_link(5)]),
            }
        )
        self.sweep(["blocked"] * 5)
        handout = self.handout()

        self.assertIsNotNone(handout, "held Filecrypt links never hide other work")
        self.assertNotIn("crypter_offer", handout)
        urls = [entry[0] for entry in handout["url"]]
        self.assertEqual([other_crypter_url(1)], urls)

    def test_a_late_member_never_joins_the_frozen_cohort(self):
        self.store_filecrypt_cohort(5)
        exchanges = self.sweep(["blocked"] * 4)
        self.store_packages({package(6): protected_blob([filecrypt_link(6)])})
        self.assertEqual(6, len(self.inventory().candidates))

        offer, handout = self.offered()
        self.assertNotEqual(fingerprint_of(filecrypt_url(6)), offer["link_fingerprint"])
        status, body = self.report_blocked(offer, handout["id"])

        self.assertEqual(200, status)
        self.assertEqual("cooldown", body["instruction"])
        self.assertEqual(5, body["sweep_total"])
        self.assertEqual(5, self.decision_row()["cohort_size"])
        self.assertEqual(
            [5] * 5,
            [exchange["body"]["sweep_total"] for exchange in exchanges]
            + [body["sweep_total"]],
        )

    def test_a_deleted_untested_member_ends_the_sweep_inconclusive(self):
        self.store_filecrypt_cohort(5)
        exchanges = self.sweep(["blocked"] * 2)
        tested = {exchange["offer"]["link_fingerprint"] for exchange in exchanges}
        victim = next(
            candidate.occurrences[0].package_id
            for candidate in self.inventory().candidates
            if candidate.fingerprint not in tested
        )
        self.state.get_db("protected").delete(victim)

        offer, handout = self.offered()
        status, body = self.report_blocked(offer, handout["id"])

        self.assertEqual(200, status)
        self.assertEqual("legacy_failure", body["instruction"])
        self.assertEqual("individual", body["state"])
        self.assertEqual(5, body["sweep_total"])
        self.assertEqual(3, body["sweep_tested"])
        self.assertNotEqual("cooldown", self.service().snapshot(CRYPTER)["state"])


class UnknownAndProbeTests(CohortIntegrationTestCase):
    """UNKNOWN is not evidence, and a probe is an operator authorization."""

    def test_an_unknown_is_accepted_without_ever_cooling_the_cohort(self):
        self.store_filecrypt_cohort(5)
        exchanges = self.sweep(["blocked", "blocked", "unknown", "blocked", "blocked"])

        self.assertEqual(200, exchanges[2]["status"])
        self.assertEqual("unknown", exchanges[2]["body"]["accepted"])
        self.assertNotIn("cleared", exchanges[2]["body"])
        self.assertEqual("legacy_failure", exchanges[-1]["body"]["instruction"])
        self.assertEqual(5, exchanges[-1]["body"]["sweep_tested"])
        self.assertNotEqual("cooldown", self.service().snapshot(CRYPTER)["state"])
        stored = self.service().crypter_decision(CRYPTER)
        self.assertEqual("individual", stored["state"])

    def test_a_whole_cohort_of_unknowns_never_reaches_a_cooldown(self):
        self.store_filecrypt_cohort(5)
        exchanges = self.sweep(["unknown"] * 5)

        self.assertEqual(
            ["unknown"] * 5, [exchange["body"]["accepted"] for exchange in exchanges]
        )
        self.assertNotEqual("cooldown", self.service().snapshot(CRYPTER)["state"])
        self.assertEqual(0, self.service().count_active_deferred_packages())

    def test_a_queued_probe_hands_out_one_held_member_during_cooldown(self):
        self.store_filecrypt_cohort(5)
        exchanges = self.sweep(["blocked"] * 5)
        self.assertEqual("cooldown", exchanges[-1]["body"]["instruction"])
        self.assertIsNone(self.handout(), "a cooling cohort hands out nothing")

        probed = exchanges[0]["handout"]["id"]
        result = self.call(PROBE_RULE, {"package_ids": [probed]})
        self.assertEqual({"requested": [probed], "rejected": []}, result)

        offer, handout = self.offered()
        self.assertEqual("probe", offer["mode"])
        self.assertEqual(probed, handout["id"])
        self.assertEqual(
            exchanges[0]["offer"]["link_fingerprint"], offer["link_fingerprint"]
        )
        self.assertIsNone(self.handout(), "one probe authorizes exactly one handout")

    def test_a_probe_that_answers_clear_ends_the_whole_cooldown(self):
        self.store_filecrypt_cohort(5)
        exchanges = self.sweep(["blocked"] * 5)
        probed = exchanges[0]["handout"]["id"]
        self.assertEqual(
            [probed], self.call(PROBE_RULE, {"package_ids": [probed]})["requested"]
        )
        offer, handout = self.offered()
        status, body = self.report_access(offer, handout["id"], "clear")

        self.assertEqual(200, status)
        self.assertIs(True, body["cleared"])
        self.assertEqual("healthy", self.decision_row()["state"])
        self.assertEqual(0, self.service().count_active_deferred_packages())
        self.assertEqual(4, len(self.decision_row()["retest_members"]))


class ConcurrentFinalReportTests(CohortIntegrationTestCase):
    """A CLEAR that commits first is never overwritten by the final BLOCKED."""

    def test_a_clear_committed_mid_transaction_beats_the_final_blocked(self):
        self.store_filecrypt_cohort(5)
        self.sweep(["blocked"] * 4)
        offer, handout = self.offered()

        original = DataBase.mutate_values
        clearing = []

        def clear_then_commit(database, targets, mutator):
            if not clearing:
                clearing.append(True)
                status, body = self.report_access(offer, handout["id"], "clear")
                clearing.append((status, body))
            return original(database, targets, mutator)

        with mock.patch.object(DataBase, "mutate_values", clear_then_commit):
            status, body = self.report_blocked(offer, handout["id"])

        self.assertEqual((200, True), (clearing[1][0], clearing[1][1]["cleared"]))
        self.assertEqual(200, status)
        self.assertEqual("stale", body["instruction"])
        self.assertEqual("healthy", self.decision_row()["state"])
        self.assertNotEqual("cooldown", self.service().snapshot(CRYPTER)["state"])
        self.assertEqual(0, self.service().count_active_deferred_packages())


class CompatibilityModeTests(CohortIntegrationTestCase):
    """A version-one helper, and the operator kill switch."""

    def version_one_report(self, handout):
        return self.attempt(
            DEFER_RULE,
            {
                "package_id": handout["id"],
                "crypter": CRYPTER,
                "reason_code": REASON,
                "link_fingerprint": fingerprint_of(handout["url"][0][0]),
            },
        )[1]

    def test_a_defer_only_helper_never_opens_a_cohort_generation(self):
        self.store_filecrypt_cohort(5)
        answers = []
        while len(answers) < 5:
            handout = self.handout(capabilities=DEFER_ONLY_CAPABILITIES)
            if handout is None:
                break
            self.assertNotIn("crypter_offer", handout)
            self.assertNotIn("terminal_operation_id", handout)
            answers.append(self.version_one_report(handout))

        for answer in answers:
            self.assertEqual(
                {
                    "success",
                    "instruction",
                    "state",
                    "evidence_count",
                    "retry_after_epoch",
                    "hold_type",
                },
                set(answer),
                "a version-one answer never carries cohort counters",
            )
        self.assertEqual("hold", answers[0]["instruction"])
        self.assertEqual("provisional", answers[0]["hold_type"])
        stored = self.decision_row()
        self.assertNotIn("members", stored or {})
        self.assertNotIn("sweep_id", stored or {})
        self.assertNotIn("schema_version", stored or {})

    def test_the_version_one_evidence_rule_is_the_only_way_a_v1_helper_cools(self):
        # The legacy three-observation rule predates the cohort sweep and still
        # owns this helper; what it must never do is freeze a cohort.
        self.store_filecrypt_cohort(5)
        answers = []
        while len(answers) < 5:
            handout = self.handout(capabilities=DEFER_ONLY_CAPABILITIES)
            if handout is None:
                break
            answers.append(self.version_one_report(handout))

        self.assertEqual(
            ["hold", "hold", "cooldown"], [answer["instruction"] for answer in answers]
        )
        self.assertEqual("crypter_cooldown", answers[-1]["hold_type"])
        self.assertEqual(3, answers[-1]["evidence_count"])
        self.assertIsNone(self.handout(capabilities=DEFER_ONLY_CAPABILITIES))
        self.assertNotIn("cohort_size", self.decision_row())

    def test_a_defer_only_helper_never_gets_a_second_hold_for_one_package(self):
        self.store_filecrypt_cohort(5)
        handout = self.handout(capabilities=DEFER_ONLY_CAPABILITIES)
        payload = {
            "package_id": handout["id"],
            "crypter": CRYPTER,
            "reason_code": REASON,
            "link_fingerprint": fingerprint_of(handout["url"][0][0]),
        }
        first = self.attempt(DEFER_RULE, payload)[1]
        second = self.attempt(DEFER_RULE, payload)[1]

        self.assertEqual("hold", first["instruction"])
        self.assertEqual("legacy_failure", second["instruction"])
        self.assertEqual("none", second["hold_type"])
        self.assertEqual(0, second["retry_after_epoch"])


class FailModeTests(CohortIntegrationTestCase):
    """`fail` mode is a pure read bypass on every cohort surface."""

    mode = "fail"

    def test_fail_mode_offers_nothing_reports_nothing_and_stores_nothing(self):
        self.store_filecrypt_cohort(5)
        handout = self.handout()

        self.assertIsNotNone(handout)
        self.assertNotIn("crypter_offer", handout)
        self.assertIsNone(self.decision_row())

        status, body = self.attempt(
            DEFER_RULE,
            {
                "package_id": handout["id"],
                "crypter": CRYPTER,
                "reason_code": REASON,
                "link_fingerprint": fingerprint_of(filecrypt_url(1)),
                "sweep_id": f"{7:032x}",
                "offer_id": f"{8:032x}",
            },
        )

        self.assertEqual(200, status)
        self.assertEqual("legacy_failure", body["instruction"])
        self.assertEqual("available", body["state"])
        self.assertEqual("", body["sweep_id"])
        self.assertIsNone(self.decision_row())
        self.assertNotIn("deferred", self.protected_row(handout["id"]))

    def test_fail_mode_answers_a_cohort_access_report_with_a_stale_body(self):
        self.store_filecrypt_cohort(5)
        status, body = self.attempt(
            ACCESS_RULE,
            {
                "package_id": package(1),
                "crypter": CRYPTER,
                "access": "clear",
                "link_fingerprint": fingerprint_of(filecrypt_url(1)),
                "sweep_id": f"{7:032x}",
                "offer_id": f"{8:032x}",
            },
        )

        self.assertEqual(409, status)
        self.assertEqual("stale", body["instruction"])
        self.assertEqual(f"{8:032x}", body["offer_id"])
        self.assertIsNone(self.decision_row())


class TerminalConfirmationTests(CohortIntegrationTestCase):
    """The package a sweep handed out is still finished exactly once."""

    def download_payload(self, package_id, operation_id, urls=(HOSTER_URL,)):
        return {
            "name": "Synthetic.Release.2024.1080p",
            "package_id": package_id,
            "urls": list(urls),
            "password": "",
            "notification": {"solvers": [{"name": "synthetic"}]},
            "protocol_version": 2,
            "terminal_operation_id": operation_id,
        }

    def test_a_cleared_cohort_member_is_downloaded_and_confirmed_once(self):
        self.store_filecrypt_cohort(5)
        exchanges = self.sweep(["blocked", "blocked", "clear"])
        cleared = exchanges[-1]["handout"]
        operation_id = cleared["terminal_operation_id"]

        status, body = self.attempt(
            DOWNLOAD_RULE, self.download_payload(cleared["id"], operation_id)
        )

        self.assertEqual(200, status)
        self.assertEqual(
            {
                "success": True,
                "terminal_state": "downloaded",
                "package_removed": True,
                "package_terminal": True,
                "package_id": cleared["id"],
            },
            body,
        )
        self.assertEqual(1, len(self.state.device.add_links_calls))
        self.assertIsNone(self.protected_row(cleared["id"]))

    def test_a_repeated_terminal_confirmation_submits_nothing_new(self):
        self.store_filecrypt_cohort(5)
        cleared = self.sweep(["clear"])[-1]["handout"]
        payload = self.download_payload(cleared["id"], cleared["terminal_operation_id"])

        first = self.attempt(DOWNLOAD_RULE, payload)
        second = self.attempt(DOWNLOAD_RULE, payload)

        self.assertEqual(first, second)
        self.assertEqual(1, len(self.state.device.add_links_calls))

    def test_the_submission_carries_this_operations_own_evidence(self):
        self.store_filecrypt_cohort(5)
        cleared = self.sweep(["clear"])[-1]["handout"]
        self.attempt(
            DOWNLOAD_RULE,
            self.download_payload(cleared["id"], cleared["terminal_operation_id"]),
        )

        comment = self.state.device.add_links_calls[0][0]["comment"]
        self.assertTrue(comment.startswith(f"{cleared['id']} op:"))
        self.assertEqual(
            comment, submission_comment(cleared["id"], comment.split("op:")[1])
        )

    def test_a_terminal_identity_of_another_package_is_refused_outright(self):
        self.store_filecrypt_cohort(5)
        exchanges = self.sweep(["blocked"] * 5)
        held = exchanges[0]["handout"]

        status, body = self.attempt(
            DOWNLOAD_RULE,
            {
                "name": "Synthetic.Release.2024.1080p",
                "package_id": held["id"],
                "urls": [HOSTER_URL],
                "password": "",
                "notification": {"solvers": []},
                "protocol_version": 2,
                "terminal_operation_id": exchanges[1]["handout"][
                    "terminal_operation_id"
                ],
            },
        )

        # The identity is a digest of the package it belongs to, so one issued
        # for another package is not a conflict but an identity this route can
        # never admit.
        self.assertEqual(400, status)
        self.assertEqual([], self.state.device.add_links_calls)
        self.assertIsNotNone(self.protected_row(held["id"]))
        self.assertNotIn(TERMINAL_OPERATION_MARKER, self.protected_row(held["id"]))


if __name__ == "__main__":
    unittest.main()
