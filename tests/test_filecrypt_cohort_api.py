# -*- coding: utf-8 -*-

import hashlib
import json
import os
import tempfile
import threading
import unittest
from unittest import mock

from bottle import Bottle, HTTPError, HTTPResponse

from quasarr.api.sponsors_helper import setup_sponsors_helper_routes
from quasarr.api.sponsors_helper.cohort_protocol import (
    COHORT_REPORT,
    CRYPTER_DEFER_CAPABILITY,
    FILECRYPT_COHORT_CAPABILITY,
    MALFORMED_REPORT,
    VERSION_ONE_REPORT,
    classify_access_report,
    classify_blocked_report,
    helper_supports_cohort,
    normalize_access_report,
    normalize_blocked_report,
    render_access_response,
    render_crypter_offer,
    render_defer_response,
    terminal_operation_id,
)
from quasarr.providers import shared_state as provider_shared_state
from quasarr.providers.crypter_candidates import (
    enumerate_filecrypt_candidates,
    link_fingerprint,
)
from quasarr.providers.crypter_cooldowns import CrypterCooldownService
from quasarr.providers.crypter_sweeps import (
    OVERSIZED_COHORT_SENTINEL,
    bypass_decision,
)
from quasarr.storage.sqlite_database import DataBase

NOW = 1_700_000_000
CRYPTER = "filecrypt"
REASON = "ip_block_suspected"
COOLDOWN_SECONDS = 24 * 60 * 60
SWEEP_WINDOW = 15 * 60
DEFER_RULE = "/sponsors_helper/api/defer/"
ACCESS_RULE = "/sponsors_helper/api/crypter-access/"
DECRYPT_RULE = "/sponsors_helper/api/to_decrypt/"
CAPABILITIES = [CRYPTER_DEFER_CAPABILITY, FILECRYPT_COHORT_CAPABILITY]


def package(index):
    return f"Quasarr_movies_{index:032x}"


def filecrypt_url(index):
    return f"https://filecrypt.invalid/container/{index}"


def fingerprint_of(url):
    return link_fingerprint(CRYPTER, url)


def protected_blob(links, title="Release.Title", **extra):
    data = {"title": title, "password": "", "links": links}
    data.update(extra)
    return json.dumps(data)


def filecrypt_rows(count, start=1):
    return {
        package(index): protected_blob([[filecrypt_url(index), CRYPTER]])
        for index in range(start, start + count)
    }


class FakeClock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


class SequentialIds:
    """Deterministic 32-hex sweep, generation, and offer identities."""

    def __init__(self):
        self.count = 0

    def __call__(self):
        self.count += 1
        return f"{self.count:032x}"


class AtomicDatabase:
    def __init__(self, rows=None, tables=None):
        self.rows = dict(rows or {})
        self.tables = {} if tables is None else tables
        self.lock = threading.RLock()
        self.before_mutation = None
        self.enumerations = 0

    def _peer(self, table):
        if table not in self.tables:
            self.tables[table] = AtomicDatabase(tables=self.tables)
        return self.tables[table]

    def _interleave(self):
        hook, self.before_mutation = self.before_mutation, None
        if hook is not None:
            hook()

    def retrieve(self, key):
        with self.lock:
            return self.rows.get(key)

    def retrieve_all_titles(self):
        with self.lock:
            self.enumerations += 1
            items = [[key, value] for key, value in sorted(self.rows.items())]
            return items or None

    def store(self, key, value):
        with self.lock:
            self.rows[key] = value
            return True

    def update_store(self, key, value):
        return self.store(key, value)

    def mutate_value(self, key, mutator):
        with self.lock:
            self._interleave()
            value = mutator(self.rows.get(key))
            if value is None:
                self.rows.pop(key, None)
            else:
                self.rows[key] = value
            return value

    def mutate_values(self, targets, mutator):
        with self.lock:
            self._interleave()
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


class AtomicSharedState:
    def __init__(self, protected_rows=None, mode="defer"):
        self.databases = {}
        self.databases["protected"] = AtomicDatabase(
            protected_rows, tables=self.databases
        )
        for table in ("crypter_cooldowns", "crypter_events"):
            self.databases[table] = AtomicDatabase(tables=self.databases)
        self.values = {"crypter_block_mode": mode, "crypter_cooldown_hours": 24}

    def get_db(self, table):
        if table not in self.databases:
            self.databases[table] = AtomicDatabase(tables=self.databases)
        return self.databases[table]

    def update(self, key, value):
        self.values[key] = value


def route_for(rule):
    app = Bottle()
    setup_sponsors_helper_routes(app)
    return next(route for route in app.routes if route.rule == rule)


class CohortApiTestCase(unittest.TestCase):
    """Every route test drives the real service against an atomic fake store."""

    def setUp(self):
        self.clock = FakeClock(NOW)
        self.ids = SequentialIds()
        self.state = AtomicSharedState()
        patcher = mock.patch.object(
            CrypterCooldownService, "_new_identifier", lambda _self: self.ids()
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def service(self):
        return CrypterCooldownService(self.state, clock=self.clock)

    def store(self, rows):
        for key, value in rows.items():
            self.state.databases["protected"].update_store(key, value)

    def call(self, rule, payload):
        route = route_for(rule)
        with (
            mock.patch("quasarr.api.sponsors_helper.shared_state", self.state),
            mock.patch("quasarr.api.sponsors_helper.request", mock.Mock(json=payload)),
            mock.patch(
                "quasarr.api.sponsors_helper.CrypterCooldownService",
                lambda state: CrypterCooldownService(state, clock=self.clock),
            ),
        ):
            return route.callback()

    def to_decrypt(self, capabilities=CAPABILITIES, **extra):
        payload = {"supported_urls": ["filecrypt.invalid", "tolink.invalid"]}
        if capabilities is not None:
            payload["capabilities"] = list(capabilities)
        payload.update(extra)
        return self.call(DECRYPT_RULE, payload)["to_decrypt"]

    def decision_row(self):
        raw = self.state.databases["crypter_cooldowns"].rows.get(CRYPTER)
        return None if raw is None else json.loads(raw)

    def package_row(self, package_id):
        return json.loads(self.state.databases["protected"].rows[package_id])

    def blocked_payload(self, offer, package_id):
        return {
            "package_id": package_id,
            "crypter": CRYPTER,
            "reason_code": REASON,
            "link_fingerprint": offer["link_fingerprint"],
            "sweep_id": offer["sweep_id"],
            "offer_id": offer["offer_id"],
        }

    def access_payload(self, offer, package_id, access="clear"):
        return {
            "package_id": package_id,
            "crypter": CRYPTER,
            "access": access,
            "link_fingerprint": offer["link_fingerprint"],
            "sweep_id": offer["sweep_id"],
            "offer_id": offer["offer_id"],
        }

    def owner_of(self, offer):
        """The deterministic first occurrence; real fingerprints are hash-ordered."""
        inventory = enumerate_filecrypt_candidates(
            self.state.databases["protected"].retrieve_all_titles()
        )
        return next(
            candidate.occurrences[0].package_id
            for candidate in inventory.candidates
            if candidate.fingerprint == offer["link_fingerprint"]
        )

    def handout_offer(self):
        handout = self.to_decrypt()
        return handout["crypter_offer"], handout

    def drive_blocked(self, count):
        """Answer `count` sweep offers with BLOCKED and return the last response."""
        response = None
        for _ in range(count):
            offer, handout = self.handout_offer()
            response = self.call(DEFER_RULE, self.blocked_payload(offer, handout["id"]))
        return response


class CohortProtocolTests(unittest.TestCase):
    def test_cohort_behaviour_needs_both_capabilities(self):
        cases = (
            (None, False),
            ([], False),
            ([CRYPTER_DEFER_CAPABILITY], False),
            ([FILECRYPT_COHORT_CAPABILITY], False),
            (["Crypter_Defer_V1", FILECRYPT_COHORT_CAPABILITY], False),
            ([CRYPTER_DEFER_CAPABILITY, FILECRYPT_COHORT_CAPABILITY], True),
        )

        for capabilities, expected in cases:
            with self.subTest(capabilities=capabilities):
                payload = {} if capabilities is None else {"capabilities": capabilities}
                self.assertIs(expected, helper_supports_cohort(payload))

    def test_terminal_operation_id_is_the_stable_documented_digest(self):
        package_id = package(7)

        operation_id = terminal_operation_id(package_id)

        self.assertEqual(
            hashlib.sha256(
                f"sponsors-helper-terminal-v2\n{package_id}".encode()
            ).hexdigest(),
            operation_id,
        )
        self.assertEqual(64, len(operation_id))
        self.assertEqual(operation_id.lower(), operation_id)
        self.assertNotEqual(operation_id, terminal_operation_id(package(8)))

    def test_a_rendered_offer_carries_no_url_and_matches_its_occurrence(self):
        url = filecrypt_url(1)
        rows = {package(1): protected_blob([[url, CRYPTER]])}
        inventory = enumerate_filecrypt_candidates(list(rows.items()))
        occurrence = inventory.candidates[0].occurrences[0]
        offer = {
            "mode": "sweep",
            "sweep_id": "a" * 32,
            "offer_id": "b" * 32,
            "link_fingerprint": occurrence.fingerprint,
            "deadline_epoch": NOW + SWEEP_WINDOW,
        }

        rendered = render_crypter_offer(offer, occurrence)

        self.assertEqual(
            {
                "capability": FILECRYPT_COHORT_CAPABILITY,
                "mode": "sweep",
                "crypter": CRYPTER,
                "sweep_id": "a" * 32,
                "offer_id": "b" * 32,
                "link_fingerprint": occurrence.fingerprint,
                "deadline_epoch": NOW + SWEEP_WINDOW,
            },
            rendered,
        )
        self.assertNotIn(url, json.dumps(rendered))
        self.assertIsNone(render_crypter_offer(offer, None))
        self.assertIsNone(render_crypter_offer(None, occurrence))

    def test_only_a_strictly_valid_offer_identity_is_a_cohort_report(self):
        valid = {
            "package_id": package(1),
            "crypter": CRYPTER,
            "reason_code": REASON,
            "link_fingerprint": "c" * 64,
            "sweep_id": "a" * 32,
            "offer_id": "b" * 32,
        }

        self.assertEqual(valid, normalize_blocked_report(dict(valid)))

        broken = (
            ("sweep_id", None),
            ("sweep_id", "A" * 32),
            ("sweep_id", "a" * 31),
            ("offer_id", ""),
            ("offer_id", "b" * 64),
            ("link_fingerprint", "c" * 63),
            ("link_fingerprint", 5),
            ("crypter", "tolink"),
        )
        for field, value in broken:
            with self.subTest(field=field, value=value):
                payload = dict(valid)
                payload[field] = value
                self.assertIsNone(normalize_blocked_report(payload))

        for field in ("sweep_id", "offer_id", "link_fingerprint"):
            with self.subTest(missing=field):
                payload = dict(valid)
                payload.pop(field)
                self.assertIsNone(normalize_blocked_report(payload))

        self.assertIsNone(normalize_blocked_report(None))
        self.assertIsNone(normalize_blocked_report([]))

    def test_cohort_access_reports_accept_only_clear_and_unknown(self):
        valid = {
            "package_id": package(1),
            "crypter": CRYPTER,
            "access": "clear",
            "link_fingerprint": "c" * 64,
            "sweep_id": "a" * 32,
            "offer_id": "b" * 32,
        }

        self.assertEqual(valid, normalize_access_report(dict(valid)))
        self.assertEqual(
            "unknown",
            normalize_access_report({**valid, "access": "unknown"})["access"],
        )
        for access in ("CLEAR", "blocked", "", None, 1):
            with self.subTest(access=access):
                self.assertIsNone(normalize_access_report({**valid, "access": access}))
        self.assertIsNone(normalize_access_report({**valid, "sweep_id": "nope"}))

    def test_rendered_responses_have_the_exact_documented_cardinality(self):
        decision = {
            "instruction": "hold",
            "state": "sweeping",
            "hold_type": "provisional",
            "accepted": "",
            "cleared": False,
            "evidence_count": 2,
            "retry_after_epoch": NOW + SWEEP_WINDOW,
            "sweep_id": "a" * 32,
            "sweep_tested": 2,
            "sweep_total": 5,
            "sweep_deadline_epoch": NOW + SWEEP_WINDOW,
            "events": {},
        }

        self.assertEqual(
            {
                "success": True,
                "instruction": "hold",
                "state": "sweeping",
                "hold_type": "provisional",
                "evidence_count": 2,
                "retry_after_epoch": NOW + SWEEP_WINDOW,
                "sweep_id": "a" * 32,
                "sweep_tested": 2,
                "sweep_total": 5,
                "sweep_deadline_epoch": NOW + SWEEP_WINDOW,
            },
            render_defer_response(decision),
        )

        body, status = render_access_response(decision, offer_id="b" * 32)
        self.assertEqual(409, status)
        self.assertEqual(
            {
                "success": True,
                "instruction": "stale",
                "state": "sweeping",
                "sweep_id": "a" * 32,
                "offer_id": "b" * 32,
                "sweep_tested": 2,
                "sweep_total": 5,
                "sweep_deadline_epoch": NOW + SWEEP_WINDOW,
            },
            body,
        )

        cleared = {**decision, "instruction": "", "cleared": True, "state": "healthy"}
        body, status = render_access_response(cleared, offer_id="b" * 32)
        self.assertEqual(200, status)
        self.assertEqual("healthy", body["state"])
        self.assertIs(True, body["cleared"])
        self.assertNotIn("accepted", body)

        unknown = {**decision, "instruction": "", "accepted": "unknown"}
        body, status = render_access_response(unknown, offer_id="b" * 32)
        self.assertEqual(200, status)
        self.assertEqual("unknown", body["accepted"])
        self.assertNotIn("cleared", body)

        body, status = render_access_response(bypass_decision(), offer_id="b" * 32)
        self.assertEqual(409, status)
        self.assertEqual("stale", body["instruction"])

    def test_report_classification_is_tri_state(self):
        identity = {"sweep_id": "a" * 32, "offer_id": "b" * 32}
        blocked = {
            "package_id": package(1),
            "crypter": CRYPTER,
            "reason_code": REASON,
            "link_fingerprint": "c" * 64,
        }
        access = {
            "package_id": package(1),
            "crypter": CRYPTER,
            "access": "clear",
            "link_fingerprint": "c" * 64,
        }

        for name, base, classify in (
            ("blocked", blocked, classify_blocked_report),
            ("access", access, classify_access_report),
        ):
            cases = (
                # No cohort identity at all is ordinary version-one work, and a
                # version-one report is not held to the exact cohort spelling.
                (base, VERSION_ONE_REPORT),
                ({**base, "crypter": "Filecrypt"}, VERSION_ONE_REPORT),
                ({**base, "link_fingerprint": "nope"}, VERSION_ONE_REPORT),
                ({**base, **identity}, COHORT_REPORT),
                # Explicit but incomplete or misspelled cohort intent.
                ({**base, "sweep_id": "a" * 32}, MALFORMED_REPORT),
                ({**base, "offer_id": "b" * 32}, MALFORMED_REPORT),
                ({**base, **identity, "sweep_id": None}, MALFORMED_REPORT),
                ({**base, **identity, "offer_id": "b" * 31}, MALFORMED_REPORT),
                ({**base, **identity, "sweep_id": "A" * 32}, MALFORMED_REPORT),
                ({**base, **identity, "link_fingerprint": "c" * 63}, MALFORMED_REPORT),
                ({**base, **identity, "package_id": 7}, MALFORMED_REPORT),
                ({**base, **identity, "crypter": "Filecrypt"}, MALFORMED_REPORT),
                ({**base, **identity, "crypter": " filecrypt"}, MALFORMED_REPORT),
                ({**base, **identity, "crypter": "filecrypt "}, MALFORMED_REPORT),
                ({**base, **identity, "crypter": "tolink"}, MALFORMED_REPORT),
                (None, VERSION_ONE_REPORT),
                ([], VERSION_ONE_REPORT),
            )
            for payload, expected in cases:
                with self.subTest(route=name, payload=payload):
                    kind, report = classify(payload)
                    self.assertEqual(expected, kind)
                    self.assertIs(expected == COHORT_REPORT, report is not None)

    def test_the_route_specific_field_decides_the_last_third_of_the_intent(self):
        identity = {
            "package_id": package(1),
            "crypter": CRYPTER,
            "link_fingerprint": "c" * 64,
            "sweep_id": "a" * 32,
            "offer_id": "b" * 32,
        }

        self.assertEqual(MALFORMED_REPORT, classify_blocked_report(dict(identity))[0])
        self.assertEqual(
            MALFORMED_REPORT,
            classify_blocked_report({**identity, "reason_code": 7})[0],
        )
        self.assertEqual(MALFORMED_REPORT, classify_access_report(dict(identity))[0])
        for access in ("CLEAR", "blocked", "", None):
            with self.subTest(access=access):
                self.assertEqual(
                    MALFORMED_REPORT,
                    classify_access_report({**identity, "access": access})[0],
                )


class CohortHandoutTests(CohortApiTestCase):
    def test_only_a_both_capability_request_receives_cohort_fields(self):
        self.store(filecrypt_rows(5))

        cases = (
            (None, False),
            ([CRYPTER_DEFER_CAPABILITY], False),
            ([FILECRYPT_COHORT_CAPABILITY], False),
            (CAPABILITIES, True),
        )
        for capabilities, cohort in cases:
            with self.subTest(capabilities=capabilities):
                handout = self.to_decrypt(capabilities=capabilities)
                self.assertIs(cohort, "crypter_offer" in handout)
                self.assertIs(cohort, "terminal_operation_id" in handout)

    def test_an_incapable_request_never_creates_a_cohort_decision(self):
        self.store(filecrypt_rows(5))

        for capabilities in (None, [CRYPTER_DEFER_CAPABILITY]):
            with self.subTest(capabilities=capabilities):
                self.to_decrypt(capabilities=capabilities)
                self.assertIsNone(self.decision_row())

    def test_the_offer_is_the_exact_first_occurrence_with_no_second_filecrypt_link(
        self,
    ):
        self.store(filecrypt_rows(5))

        offer, handout = self.handout_offer()

        self.assertEqual(self.owner_of(offer), handout["id"])
        self.assertEqual("sweep", offer["mode"])
        self.assertEqual(CRYPTER, offer["crypter"])
        self.assertEqual(FILECRYPT_COHORT_CAPABILITY, offer["capability"])
        self.assertEqual(NOW + SWEEP_WINDOW, offer["deadline_epoch"])
        urls = [entry[0] for entry in handout["url"]]
        self.assertEqual(offer["link_fingerprint"], fingerprint_of(urls[0]))
        self.assertEqual(1, sum(1 for url in urls if "filecrypt" in url))

    def test_the_terminal_operation_id_is_stable_for_ordinary_and_cohort_work(self):
        self.store(
            {
                package(1): protected_blob([["https://tolink.invalid/x", "tolink"]]),
            }
        )

        ordinary = self.to_decrypt(
            **{"supported_urls": ["tolink.invalid"]},
        )
        self.assertNotIn("crypter_offer", ordinary)
        self.assertEqual(
            terminal_operation_id(package(1)), ordinary["terminal_operation_id"]
        )
        self.assertEqual(
            ordinary["terminal_operation_id"],
            self.to_decrypt(**{"supported_urls": ["tolink.invalid"]})[
                "terminal_operation_id"
            ],
        )

    def test_a_second_filecrypt_link_in_the_offered_package_is_withheld(self):
        first, second = filecrypt_url(1), filecrypt_url(2)
        self.store(
            {
                package(1): protected_blob(
                    [[first, CRYPTER], [second, CRYPTER]],
                ),
                package(2): protected_blob([[filecrypt_url(3), CRYPTER]]),
            }
        )

        offer, handout = self.handout_offer()

        urls = [entry[0] for entry in handout["url"]]
        self.assertEqual(1, len(urls))
        self.assertEqual(offer["link_fingerprint"], fingerprint_of(urls[0]))

    def test_a_pending_second_fingerprint_bypasses_a_tested_hold(self):
        first, second = filecrypt_url(1), filecrypt_url(2)
        self.store(
            {
                package(1): protected_blob([[first, CRYPTER], [second, CRYPTER]]),
                package(2): protected_blob([[filecrypt_url(3), CRYPTER]]),
                package(3): protected_blob([[filecrypt_url(4), CRYPTER]]),
                package(4): protected_blob([[filecrypt_url(5), CRYPTER]]),
                package(5): protected_blob([[filecrypt_url(6), CRYPTER]]),
            }
        )
        offer, handout = self.handout_offer()
        self.call(DEFER_RULE, self.blocked_payload(offer, handout["id"]))
        held = offer["link_fingerprint"]

        next_offer, next_handout = self.handout_offer()

        self.assertNotEqual(held, next_offer["link_fingerprint"])
        handed = [fingerprint_of(entry[0]) for entry in next_handout["url"]]
        self.assertNotIn(held, handed)
        self.assertIn(next_offer["link_fingerprint"], handed)

    def test_a_tested_link_never_hides_a_pending_link_of_the_same_package(self):
        first, second = filecrypt_url(1), filecrypt_url(2)
        rows = {package(1): protected_blob([[first, CRYPTER], [second, CRYPTER]])}
        rows.update(filecrypt_rows(8, start=2))
        self.store(rows)
        owned = {fingerprint_of(first), fingerprint_of(second)}

        # Answer offers until exactly one of the two containers of the first
        # package is tested; a hash-ordered cohort offers them in no fixed slot.
        for _ in range(len(rows) + 1):
            offer, handout = self.handout_offer()
            held = offer["link_fingerprint"]
            self.call(DEFER_RULE, self.blocked_payload(offer, handout["id"]))
            if held in owned:
                break
        else:
            self.fail("no container of the two-link package was ever offered")

        remaining = self.call(
            DECRYPT_RULE,
            {
                "supported_urls": ["filecrypt.invalid"],
                "capabilities": CAPABILITIES,
                "excluded_package_ids": sorted(rows.keys() - {package(1)}),
            },
        )["to_decrypt"]

        self.assertEqual(package(1), remaining["id"])
        self.assertEqual(
            sorted(owned - {held}),
            [fingerprint_of(entry[0]) for entry in remaining["url"]],
        )

    def test_every_occurrence_of_a_tested_fingerprint_stays_blocked(self):
        shared = filecrypt_url(1)
        self.store(
            {
                package(1): protected_blob([[shared, CRYPTER]]),
                package(2): protected_blob([[shared, CRYPTER]]),
                package(3): protected_blob([[filecrypt_url(3), CRYPTER]]),
                package(4): protected_blob([[filecrypt_url(4), CRYPTER]]),
                package(5): protected_blob([[filecrypt_url(5), CRYPTER]]),
                package(6): protected_blob([[filecrypt_url(6), CRYPTER]]),
            }
        )
        offer, handout = self.handout_offer()
        while offer["link_fingerprint"] != fingerprint_of(shared):
            self.call(DEFER_RULE, self.blocked_payload(offer, handout["id"]))
            offer, handout = self.handout_offer()
        # Two packages carry this container, and the offer names the lowest
        # package ID of the two rather than any other occurrence.
        self.assertEqual(package(1), handout["id"])
        self.assertEqual(self.owner_of(offer), handout["id"])
        self.call(DEFER_RULE, self.blocked_payload(offer, handout["id"]))

        service = self.service()
        for package_id in (package(1), package(2)):
            deferred = service.get_package_defer(package_id)
            self.assertIsNotNone(deferred)
            self.assertEqual([fingerprint_of(shared)], deferred["link_fingerprints"])

        for _ in range(4):
            try:
                handout = self.call(
                    DECRYPT_RULE,
                    {
                        "supported_urls": ["filecrypt.invalid"],
                        "capabilities": CAPABILITIES,
                    },
                )["to_decrypt"]
            except HTTPError:
                break
            later = handout.get("crypter_offer")
            if later is None:
                break
            self.assertNotEqual(fingerprint_of(shared), later["link_fingerprint"])
            self.assertNotIn(
                fingerprint_of(shared),
                [fingerprint_of(entry[0]) for entry in handout["url"]],
            )
            self.call(DEFER_RULE, self.blocked_payload(later, handout["id"]))

    def test_a_held_package_keeps_offering_its_alternative_linkcrypter(self):
        self.store(
            {
                package(1): protected_blob(
                    [
                        [filecrypt_url(1), CRYPTER],
                        ["https://tolink.invalid/alternative", "tolink"],
                    ]
                ),
                package(2): protected_blob([[filecrypt_url(2), CRYPTER]]),
                package(3): protected_blob([[filecrypt_url(3), CRYPTER]]),
            }
        )
        offer, handout = self.handout_offer()
        while handout["id"] != package(1):
            self.call(DEFER_RULE, self.blocked_payload(offer, handout["id"]))
            offer, handout = self.handout_offer()
        self.call(DEFER_RULE, self.blocked_payload(offer, handout["id"]))

        remaining = self.call(
            DECRYPT_RULE,
            {
                "supported_urls": ["filecrypt.invalid", "tolink.invalid"],
                "capabilities": CAPABILITIES,
                "excluded_package_ids": [package(2), package(3)],
            },
        )["to_decrypt"]

        self.assertEqual(package(1), remaining["id"])
        self.assertEqual(
            ["https://tolink.invalid/alternative"],
            [entry[0] for entry in remaining["url"]],
        )

    def test_the_shared_candidate_predicate_decides_cohort_membership(self):
        self.store(
            {
                package(1): protected_blob([[filecrypt_url(1), CRYPTER]]),
                package(2): protected_blob(
                    [[filecrypt_url(2), CRYPTER]], disabled=True
                ),
                package(3): json.dumps(
                    {"title": "No.Password", "links": [[filecrypt_url(3), CRYPTER]]}
                ),
                package(4): protected_blob([]),
                package(5): protected_blob([[filecrypt_url(5), CRYPTER]]),
                package(6): protected_blob([[filecrypt_url(6), CRYPTER]]),
            }
        )

        self.handout_offer()

        members = self.decision_row()["members"]
        self.assertEqual(
            sorted(fingerprint_of(filecrypt_url(index)) for index in (1, 5, 6)),
            [entry["link_fingerprint"] for entry in members],
        )

    def test_an_oversized_inventory_never_offers_a_partial_cohort(self):
        self.store(filecrypt_rows(101))

        handout = self.to_decrypt()

        self.assertNotIn("crypter_offer", handout)
        self.assertIn("terminal_operation_id", handout)
        record = self.decision_row()
        self.assertEqual("individual", record["state"])
        self.assertEqual("cohort_oversized", record["reason"])

    def test_an_unreadable_inventory_hands_out_work_without_an_offer(self):
        self.store(filecrypt_rows(5))

        with mock.patch(
            "quasarr.api.sponsors_helper.enumerate_filecrypt_candidates",
            side_effect=RuntimeError("inventory unavailable"),
        ):
            handout = self.to_decrypt()

        self.assertNotIn("crypter_offer", handout)
        self.assertIsNone(self.decision_row())

    def test_fail_mode_hands_out_no_cohort_offer_and_writes_nothing(self):
        self.state.values["crypter_block_mode"] = "fail"
        self.store(filecrypt_rows(5))

        handout = self.to_decrypt()

        self.assertNotIn("crypter_offer", handout)
        self.assertIn("terminal_operation_id", handout)
        self.assertIsNone(self.decision_row())

    def test_a_retest_is_offered_in_exact_ascending_order_after_a_clear(self):
        self.store(filecrypt_rows(5))
        blocked = []
        for _ in range(3):
            offer, handout = self.handout_offer()
            blocked.append(offer["link_fingerprint"])
            self.call(DEFER_RULE, self.blocked_payload(offer, handout["id"]))
        offer, handout = self.handout_offer()
        self.call(ACCESS_RULE, self.access_payload(offer, handout["id"]))

        retests = []
        for _ in range(len(blocked)):
            retest, retest_handout = self.handout_offer()
            self.assertEqual("retest", retest["mode"])
            retests.append(retest["link_fingerprint"])
            self.call(
                ACCESS_RULE,
                self.access_payload(retest, retest_handout["id"], access="unknown"),
            )

        self.assertEqual(sorted(blocked), retests)

    def test_a_queued_probe_hands_out_one_held_member_during_cooldown(self):
        self.store(filecrypt_rows(5))
        self.drive_blocked(5)
        self.assertEqual("cooldown", self.decision_row()["state"])
        service = self.service()
        held = [
            package_id
            for package_id in (package(index) for index in range(1, 6))
            if service.get_package_defer(package_id)
        ]
        self.assertTrue(held)
        service.request_probe(held)

        handout = self.to_decrypt()

        self.assertEqual("probe", handout["crypter_offer"]["mode"])
        self.assertIs(
            False, self.service().get_package_defer(handout["id"])["probe_requested"]
        )

    def test_no_route_log_contains_a_raw_url_or_fingerprint(self):
        self.store(filecrypt_rows(5))

        with (
            mock.patch("quasarr.api.sponsors_helper.debug") as debug,
            mock.patch("quasarr.api.sponsors_helper.info") as info,
            mock.patch("quasarr.api.sponsors_helper.warn") as warn,
        ):
            offer, handout = self.handout_offer()
            self.call(DEFER_RULE, self.blocked_payload(offer, handout["id"]))
            logged = " ".join(
                str(argument)
                for recorder in (debug, info, warn)
                for call in recorder.mock_calls
                for argument in call.args
            )

        self.assertNotIn("filecrypt.invalid", logged)
        self.assertNotIn(offer["link_fingerprint"], logged)
        self.assertNotIn(offer["offer_id"], logged)


class CohortDeferMatrixTests(CohortApiTestCase):
    def test_a_pending_sweep_member_answers_an_exact_provisional_hold(self):
        self.store(filecrypt_rows(5))
        offer, handout = self.handout_offer()

        response = self.call(DEFER_RULE, self.blocked_payload(offer, handout["id"]))

        self.assertEqual(
            {
                "success": True,
                "instruction": "hold",
                "state": "sweeping",
                "hold_type": "provisional",
                "evidence_count": 1,
                "retry_after_epoch": NOW + SWEEP_WINDOW,
                "sweep_id": offer["sweep_id"],
                "sweep_tested": 1,
                "sweep_total": 5,
                "sweep_deadline_epoch": NOW + SWEEP_WINDOW,
            },
            response,
        )

    def test_a_complete_all_blocked_cohort_answers_an_exact_cooldown(self):
        self.store(filecrypt_rows(5))

        response = self.drive_blocked(5)

        self.assertEqual("cooldown", response["instruction"])
        self.assertEqual("cooldown", response["state"])
        self.assertEqual("crypter_cooldown", response["hold_type"])
        self.assertEqual(5, response["evidence_count"])
        self.assertEqual(NOW + COOLDOWN_SECONDS, response["retry_after_epoch"])
        self.assertEqual(5, response["sweep_tested"])
        self.assertEqual(5, response["sweep_total"])

    def test_a_small_complete_cohort_holds_without_cooling(self):
        self.store(filecrypt_rows(4))

        response = self.drive_blocked(4)

        self.assertEqual("hold", response["instruction"])
        self.assertEqual("individual", response["state"])
        self.assertEqual("provisional", response["hold_type"])
        self.assertEqual(NOW + SWEEP_WINDOW, response["retry_after_epoch"])
        self.assertEqual(4, response["sweep_total"])
        self.assertEqual("individual", self.decision_row()["state"])

    def test_an_unverifiable_inventory_can_never_cool(self):
        self.store(filecrypt_rows(5))
        self.drive_blocked(4)
        offer, handout = self.handout_offer()

        with mock.patch(
            "quasarr.api.sponsors_helper.enumerate_filecrypt_candidates",
            side_effect=RuntimeError("inventory unavailable"),
        ):
            response = self.call(DEFER_RULE, self.blocked_payload(offer, handout["id"]))

        self.assertEqual("hold", response["instruction"])
        self.assertEqual("individual", response["state"])
        record = self.decision_row()
        self.assertEqual("individual", record["state"])
        self.assertEqual("inventory_unavailable", record["reason"])

    def test_a_stale_offer_is_answered_with_http_200_and_no_local_hold(self):
        self.store(filecrypt_rows(5))
        offer, handout = self.handout_offer()
        package_id = handout["id"]
        superseded = dict(offer)
        superseded["offer_id"] = "f" * 32
        before = dict(self.state.databases["protected"].rows)

        response = self.call(DEFER_RULE, self.blocked_payload(superseded, package_id))

        self.assertEqual("stale", response["instruction"])
        self.assertEqual("sweeping", response["state"])
        self.assertEqual("none", response["hold_type"])
        self.assertEqual(0, response["retry_after_epoch"])
        self.assertIs(True, response["success"])
        self.assertEqual(before, self.state.databases["protected"].rows)

    def test_a_stale_report_during_an_oversized_window_reports_the_sentinel(self):
        self.store(filecrypt_rows(101))
        self.to_decrypt()
        record = self.decision_row()

        response = self.call(
            DEFER_RULE,
            self.blocked_payload(
                {
                    "link_fingerprint": fingerprint_of(filecrypt_url(1)),
                    "sweep_id": record["generation_id"],
                    "offer_id": "f" * 32,
                },
                package(1),
            ),
        )

        self.assertEqual("stale", response["instruction"])
        self.assertEqual(OVERSIZED_COHORT_SENTINEL, response["sweep_total"])

    def test_a_duplicate_accepted_report_replays_its_decision(self):
        self.store(filecrypt_rows(5))
        offer, handout = self.handout_offer()
        payload = self.blocked_payload(offer, handout["id"])
        first = self.call(DEFER_RULE, payload)
        stored = dict(self.state.databases["protected"].rows)

        replay = self.call(DEFER_RULE, dict(payload))

        self.assertEqual(first, replay)
        self.assertEqual(stored, self.state.databases["protected"].rows)

    def test_fail_mode_answers_the_cohort_shaped_bypass_without_state_access(self):
        self.state.values["crypter_block_mode"] = "fail"
        state = self.state
        state.get_db = mock.Mock(
            side_effect=AssertionError("fail mode must not read state")
        )

        response = self.call(
            DEFER_RULE,
            {
                "package_id": package(1),
                "crypter": CRYPTER,
                "reason_code": REASON,
                "link_fingerprint": "c" * 64,
                "sweep_id": "a" * 32,
                "offer_id": "b" * 32,
            },
        )

        self.assertEqual(
            {
                "success": True,
                "instruction": "legacy_failure",
                "state": "available",
                "hold_type": "none",
                "evidence_count": 0,
                "retry_after_epoch": 0,
                "sweep_id": "",
                "sweep_tested": 0,
                "sweep_total": 0,
                "sweep_deadline_epoch": 0,
            },
            response,
        )
        state.get_db.assert_not_called()

    def test_a_report_naming_no_offer_falls_back_to_v1_and_keeps_the_cohort_state(
        self,
    ):
        self.store(filecrypt_rows(5))
        offer, handout = self.handout_offer()
        before = self.state.databases["crypter_cooldowns"].rows[CRYPTER]

        response = self.call(
            DEFER_RULE,
            {
                "package_id": handout["id"],
                "crypter": CRYPTER,
                "reason_code": REASON,
                "link_fingerprint": offer["link_fingerprint"],
            },
        )

        self.assertEqual(
            {
                "success",
                "instruction",
                "state",
                "hold_type",
                "evidence_count",
                "retry_after_epoch",
            },
            set(response),
        )
        self.assertEqual("legacy_failure", response["instruction"])
        self.assertEqual("observing", response["state"])
        self.assertEqual(
            before, self.state.databases["crypter_cooldowns"].rows[CRYPTER]
        )


class InternalFailureBodyTests(CohortApiTestCase):
    """An unexpected failure tells the helper nothing about this server."""

    SECRET = "/var/lib/quasarr/Quasarr.db is locked by Synthetic.Release.2024"

    def blowing_up(self, name):
        return mock.patch.object(
            CrypterCooldownService, name, side_effect=RuntimeError(self.SECRET)
        )

    def unexpected(self, rule, payload, method):
        with self.blowing_up(method), self.assertRaises(HTTPError) as context:
            self.call(rule, payload)
        return context.exception

    def test_every_report_route_answers_one_fixed_body(self):
        self.store(filecrypt_rows(5))
        offer, handout = self.handout_offer()
        cases = (
            (
                DEFER_RULE,
                self.blocked_payload(offer, handout["id"]),
                "record_cohort_blocked",
            ),
            (
                ACCESS_RULE,
                self.access_payload(offer, handout["id"]),
                "record_cohort_access",
            ),
            (
                DEFER_RULE,
                {
                    "package_id": handout["id"],
                    "crypter": CRYPTER,
                    "reason_code": REASON,
                    "link_fingerprint": offer["link_fingerprint"],
                },
                "record_version_one_report",
            ),
        )

        for rule, payload, method in cases:
            with self.subTest(method=method):
                error = self.unexpected(rule, payload, method)

                self.assertEqual(500, error.status_code)
                self.assertEqual("Internal server error", error.body)
                self.assertNotIn("Synthetic", str(error.body))
                self.assertNotIn("Quasarr.db", str(error.body))

    def test_the_handout_route_answers_the_same_fixed_body(self):
        self.store(filecrypt_rows(5))

        with (
            mock.patch(
                "quasarr.api.sponsors_helper.select_helper_package",
                side_effect=RuntimeError(self.SECRET),
            ),
            self.assertRaises(HTTPError) as context,
        ):
            self.to_decrypt()

        self.assertEqual(500, context.exception.status_code)
        self.assertEqual("Internal server error", context.exception.body)


class LegacyReportPrecedenceTests(CohortApiTestCase):
    LEGACY_KEYS = {
        "success",
        "instruction",
        "state",
        "hold_type",
        "evidence_count",
        "retry_after_epoch",
    }

    def legacy_defer(self, package_id=None):
        return self.call(
            DEFER_RULE,
            {
                "package_id": package_id or package(1),
                "crypter": CRYPTER,
                "reason_code": REASON,
                "link_fingerprint": fingerprint_of(filecrypt_url(1)),
            },
        )

    def test_the_first_and_later_legacy_reports_keep_their_exact_bodies(self):
        self.store(filecrypt_rows(2))

        first = self.legacy_defer()
        later = self.legacy_defer()

        self.assertEqual(
            {
                "success": True,
                "instruction": "hold",
                "state": "observing",
                "hold_type": "provisional",
                "evidence_count": 1,
                "retry_after_epoch": NOW + 900,
            },
            first,
        )
        self.assertEqual(
            {
                "success": True,
                "instruction": "legacy_failure",
                "state": "observing",
                "hold_type": "none",
                "evidence_count": 1,
                "retry_after_epoch": 0,
            },
            later,
        )

    def test_a_legacy_report_reads_every_cohort_state_without_changing_it(self):
        cases = (
            ("sweeping", "legacy_failure", "observing", "none", 1, 0),
            ("cooldown", "cooldown", "cooldown", "crypter_cooldown", 5, None),
            ("healthy", "legacy_failure", "available", "none", 0, 0),
            ("individual", "legacy_failure", "available", "none", 0, 0),
        )

        for state, instruction, legacy_state, hold_type, evidence, retry in cases:
            with self.subTest(state=state):
                self.setUp()
                self.store(filecrypt_rows(5))
                if state == "sweeping":
                    self.handout_offer()
                elif state == "cooldown":
                    self.drive_blocked(5)
                elif state == "healthy":
                    offer, handout = self.handout_offer()
                    self.call(ACCESS_RULE, self.access_payload(offer, handout["id"]))
                else:
                    self.setUp()
                    self.store(filecrypt_rows(4))
                    self.drive_blocked(4)
                before = self.state.databases["crypter_cooldowns"].rows[CRYPTER]

                response = self.legacy_defer()

                self.assertEqual(self.LEGACY_KEYS, set(response))
                self.assertEqual(instruction, response["instruction"])
                self.assertEqual(legacy_state, response["state"])
                self.assertEqual(hold_type, response["hold_type"])
                self.assertEqual(evidence, response["evidence_count"])
                if retry is not None:
                    self.assertEqual(retry, response["retry_after_epoch"])
                else:
                    self.assertEqual(
                        NOW + COOLDOWN_SECONDS, response["retry_after_epoch"]
                    )
                self.assertEqual(
                    before, self.state.databases["crypter_cooldowns"].rows[CRYPTER]
                )

    def test_a_legacy_clear_enters_health_and_queues_every_blocked_fingerprint(self):
        self.store(filecrypt_rows(5))
        blocked = []
        for _ in range(3):
            offer, handout = self.handout_offer()
            blocked.append(offer["link_fingerprint"])
            self.call(DEFER_RULE, self.blocked_payload(offer, handout["id"]))
        held = [
            package_id
            for package_id in (package(index) for index in range(1, 6))
            if self.service().get_package_defer(package_id)
        ]
        self.assertTrue(held)

        response = self.call(
            ACCESS_RULE,
            {"package_id": held[0], "crypter": CRYPTER, "access": "clear"},
        )

        self.assertEqual(
            {"success": True, "state": "available", "cleared": True}, response
        )
        record = self.decision_row()
        self.assertEqual("healthy", record["state"])
        self.assertEqual(NOW + SWEEP_WINDOW, record["until_epoch"])
        self.assertEqual(sorted(blocked), record["retest_members"])
        service = self.service()
        self.assertEqual("available", service.snapshot(CRYPTER)["state"])
        for package_id in held:
            deferred = service.get_package_defer(package_id)
            if deferred is not None:
                self.assertFalse(
                    service.project_package_defer(
                        deferred,
                        service.crypter_projection(CRYPTER).snapshot,
                        service.crypter_projection(CRYPTER).decision,
                    )["active"]
                )

    def test_a_legacy_clear_clears_a_marked_legacy_cooldown(self):
        self.store(filecrypt_rows(3))
        for index in range(1, 4):
            self.call(
                DEFER_RULE,
                {
                    "package_id": package(index),
                    "crypter": CRYPTER,
                    "reason_code": REASON,
                    "link_fingerprint": fingerprint_of(filecrypt_url(index)),
                },
            )
        self.assertEqual("cooldown", self.service().snapshot(CRYPTER)["state"])

        response = self.call(
            ACCESS_RULE,
            {"package_id": package(1), "crypter": CRYPTER, "access": "clear"},
        )

        self.assertEqual(
            {"success": True, "state": "available", "cleared": True}, response
        )
        self.assertEqual("available", self.service().snapshot(CRYPTER)["state"])
        self.assertEqual("healthy", self.decision_row()["state"])

    def test_a_repeated_legacy_clear_stays_idempotent(self):
        self.store(filecrypt_rows(5))
        offer, handout = self.handout_offer()
        self.call(DEFER_RULE, self.blocked_payload(offer, handout["id"]))
        owner = self.owner_of(offer)

        first = self.call(
            ACCESS_RULE, {"package_id": owner, "crypter": CRYPTER, "access": "clear"}
        )
        record = self.decision_row()
        self.clock.now = NOW + 60
        second = self.call(
            ACCESS_RULE, {"package_id": owner, "crypter": CRYPTER, "access": "clear"}
        )

        self.assertEqual(first, second)
        self.assertEqual("healthy", self.decision_row()["state"])
        self.assertEqual(
            record["retest_members"], self.decision_row()["retest_members"]
        )

    def test_a_physical_cleanup_failure_never_changes_the_acknowledgement(self):
        self.store(filecrypt_rows(5))
        offer, handout = self.handout_offer()
        self.call(DEFER_RULE, self.blocked_payload(offer, handout["id"]))
        owner = self.owner_of(offer)

        with mock.patch.object(
            CrypterCooldownService,
            "clear_crypter_generation_holds",
            side_effect=RuntimeError("storage unavailable"),
        ):
            response = self.call(
                ACCESS_RULE,
                {"package_id": owner, "crypter": CRYPTER, "access": "clear"},
            )

        self.assertEqual(
            {"success": True, "state": "available", "cleared": True}, response
        )
        self.assertEqual("healthy", self.decision_row()["state"])

    def test_fail_mode_legacy_clear_returns_the_compatibility_body_untouched(self):
        self.state.values["crypter_block_mode"] = "fail"
        self.state.get_db = mock.Mock(
            side_effect=AssertionError("fail mode must not read state")
        )

        response = self.call(
            ACCESS_RULE,
            {"package_id": package(1), "crypter": CRYPTER, "access": "clear"},
        )

        self.assertEqual(
            {"success": True, "state": "available", "cleared": True}, response
        )
        self.state.get_db.assert_not_called()


class CohortAccessTests(CohortApiTestCase):
    def test_a_cohort_clear_acknowledges_the_exact_offer_and_is_idempotent(self):
        self.store(filecrypt_rows(5))
        offer, handout = self.handout_offer()
        self.call(DEFER_RULE, self.blocked_payload(offer, handout["id"]))
        second, second_handout = self.handout_offer()

        response = self.call(
            ACCESS_RULE, self.access_payload(second, second_handout["id"])
        )

        self.assertEqual(
            {
                "success": True,
                "state": "healthy",
                "cleared": True,
                "sweep_id": second["sweep_id"],
                "offer_id": second["offer_id"],
                "sweep_tested": response["sweep_tested"],
                "sweep_total": response["sweep_total"],
                "sweep_deadline_epoch": response["sweep_deadline_epoch"],
            },
            response,
        )
        self.clock.now = NOW + 30
        self.assertEqual(
            response,
            self.call(ACCESS_RULE, self.access_payload(second, second_handout["id"])),
        )

    def test_a_cohort_unknown_is_accepted_without_ending_the_sweep(self):
        self.store(filecrypt_rows(5))
        offer, handout = self.handout_offer()

        response = self.call(
            ACCESS_RULE, self.access_payload(offer, handout["id"], access="unknown")
        )

        self.assertEqual(
            {
                "success": True,
                "state": "sweeping",
                "accepted": "unknown",
                "sweep_id": offer["sweep_id"],
                "offer_id": offer["offer_id"],
                "sweep_tested": 1,
                "sweep_total": 5,
                "sweep_deadline_epoch": NOW + SWEEP_WINDOW,
            },
            response,
        )
        self.assertEqual("sweeping", self.decision_row()["state"])

    def test_a_stale_access_report_is_http_409_and_non_destructive(self):
        self.store(filecrypt_rows(5))
        offer, handout = self.handout_offer()
        superseded = dict(offer)
        superseded["offer_id"] = "f" * 32
        before = dict(self.state.databases["crypter_cooldowns"].rows)

        result = self.call(ACCESS_RULE, self.access_payload(superseded, handout["id"]))

        self.assertIsInstance(result, HTTPResponse)
        self.assertEqual(409, result.status_code)
        body = json.loads(result.body)
        self.assertEqual("stale", body["instruction"])
        self.assertIs(True, body["success"])
        self.assertEqual("f" * 32, body["offer_id"])
        self.assertEqual(before, self.state.databases["crypter_cooldowns"].rows)

    def test_fail_mode_answers_a_cohort_access_report_with_a_stale_acknowledgement(
        self,
    ):
        self.state.values["crypter_block_mode"] = "fail"
        self.state.get_db = mock.Mock(
            side_effect=AssertionError("fail mode must not read state")
        )

        result = self.call(
            ACCESS_RULE,
            {
                "package_id": package(1),
                "crypter": CRYPTER,
                "access": "clear",
                "link_fingerprint": "c" * 64,
                "sweep_id": "a" * 32,
                "offer_id": "b" * 32,
            },
        )

        self.assertEqual(409, result.status_code)
        self.assertEqual("stale", json.loads(result.body)["instruction"])
        self.state.get_db.assert_not_called()

    def test_an_unsupported_cohort_access_value_stays_on_the_legacy_rejection(self):
        self.store(filecrypt_rows(5))
        offer, handout = self.handout_offer()

        with self.assertRaises(HTTPError) as context:
            self.call(
                ACCESS_RULE,
                self.access_payload(offer, handout["id"], access="blocked"),
            )

        self.assertEqual(400, context.exception.status_code)
        self.assertEqual("sweeping", self.decision_row()["state"])

    def test_a_cohort_clear_releases_every_matching_filecrypt_hold(self):
        self.store(filecrypt_rows(5))
        self.drive_blocked(3)
        service = self.service()
        held = [
            package_id
            for package_id in (package(index) for index in range(1, 6))
            if service.get_package_defer(package_id)
        ]
        self.assertEqual(3, len(held))
        offer, handout = self.handout_offer()

        self.call(ACCESS_RULE, self.access_payload(offer, handout["id"]))

        service = self.service()
        for package_id in held:
            self.assertIsNone(service.get_package_defer(package_id))


class MalformedCohortIntentTests(CohortApiTestCase):
    """Explicit cohort intent that does not parse never reaches version one."""

    BLOCKED_BREAKAGE = (
        {"sweep_id": "not-a-sweep"},
        {"offer_id": ""},
        {"offer_id": None},
        {"crypter": "Filecrypt"},
        {"crypter": " filecrypt"},
        {"link_fingerprint": "c" * 63},
        {"reason_code": 7},
    )
    ACCESS_BREAKAGE = (
        {"sweep_id": "a" * 31},
        {"offer_id": "b" * 64},
        {"crypter": "FILECRYPT"},
        {"crypter": "tolink"},
        {"access": "blocked"},
    )

    def blocked(self, offer, package_id, **overrides):
        payload = self.blocked_payload(offer, package_id)
        payload.update(overrides)
        return payload

    def access(self, offer, package_id, **overrides):
        payload = self.access_payload(offer, package_id)
        payload.update(overrides)
        return payload

    def assert_rejected(self, rule, payload):
        with self.assertRaises(HTTPError) as raised:
            self.call(rule, payload)
        self.assertEqual(400, raised.exception.status_code)

    def assert_state_survives(self, action):
        decision = self.state.databases["crypter_cooldowns"].rows[CRYPTER]
        packages = dict(self.state.databases["protected"].rows)

        action()

        self.assertEqual(
            decision, self.state.databases["crypter_cooldowns"].rows[CRYPTER]
        )
        self.assertEqual(packages, self.state.databases["protected"].rows)

    def test_a_malformed_blocked_report_survives_a_live_sweep_byte_identically(self):
        self.store(filecrypt_rows(5))
        offer, handout = self.handout_offer()

        for overrides in self.BLOCKED_BREAKAGE:
            with self.subTest(**overrides):
                self.assert_state_survives(
                    lambda overrides=overrides: self.assert_rejected(
                        DEFER_RULE, self.blocked(offer, handout["id"], **overrides)
                    )
                )
        self.assertEqual("sweeping", self.decision_row()["state"])

    def test_a_malformed_access_report_survives_a_live_cooldown_byte_identically(self):
        self.store(filecrypt_rows(5))
        self.drive_blocked(5)
        offer = {
            "link_fingerprint": fingerprint_of(filecrypt_url(1)),
            "sweep_id": self.decision_row()["sweep_id"],
            "offer_id": "f" * 32,
        }

        for overrides in self.ACCESS_BREAKAGE:
            with self.subTest(**overrides):
                self.assert_state_survives(
                    lambda overrides=overrides: self.assert_rejected(
                        ACCESS_RULE, self.access(offer, package(1), **overrides)
                    )
                )
        self.assertEqual("cooldown", self.decision_row()["state"])

    def test_a_malformed_blocked_report_never_opens_a_legacy_observation(self):
        self.store(filecrypt_rows(5))

        self.assert_rejected(
            DEFER_RULE,
            {
                "package_id": package(1),
                "crypter": CRYPTER,
                "reason_code": REASON,
                "link_fingerprint": fingerprint_of(filecrypt_url(1)),
                "offer_id": "b" * 32,
            },
        )

        self.assertIsNone(self.decision_row())
        self.assertIsNone(self.service().get_package_defer(package(1)))

    def test_a_report_without_any_cohort_field_still_takes_the_version_one_route(self):
        self.store(filecrypt_rows(5))

        response = self.call(
            DEFER_RULE,
            {
                "package_id": package(1),
                "crypter": "Filecrypt",
                "reason_code": REASON,
                "link_fingerprint": fingerprint_of(filecrypt_url(1)),
            },
        )

        self.assertEqual("hold", response["instruction"])
        self.assertIsNotNone(self.service().get_package_defer(package(1)))


class OfferOccurrenceBindingTests(CohortApiTestCase):
    """A handout is bound to the exact stored occurrence, not to a fingerprint."""

    def handout_naming(self, wanted, attempts=10):
        """Answer offers with BLOCKED until the wanted member is the one offered."""
        for _ in range(attempts):
            offer, handout = self.handout_offer()
            if offer["link_fingerprint"] == wanted:
                return offer, handout
            self.call(DEFER_RULE, self.blocked_payload(offer, handout["id"]))
        raise AssertionError("the wanted cohort member was never offered")

    def test_only_the_selected_index_of_a_duplicated_fingerprint_is_handed_out(self):
        stored = filecrypt_url(1)
        duplicate = f"{stored}#mirror"
        rows = filecrypt_rows(4, start=2)
        rows[package(1)] = protected_blob([[stored, CRYPTER], [duplicate, CRYPTER]])
        self.store(rows)
        shared = fingerprint_of(stored)
        self.assertEqual(shared, fingerprint_of(duplicate))

        offer, handout = self.handout_naming(shared)

        urls = [entry[0] for entry in handout["url"]]
        self.assertEqual(package(1), handout["id"])
        self.assertEqual([stored], [url for url in urls if "filecrypt" in url])
        self.assertEqual(stored, urls[0])
        self.assertNotIn(duplicate, urls)
        self.assertEqual(shared, offer["link_fingerprint"])

    def test_the_bound_occurrence_is_the_inventorys_own_first_occurrence(self):
        stored = filecrypt_url(1)
        rows = filecrypt_rows(4, start=2)
        rows[package(1)] = protected_blob(
            [["https://tolink.invalid/one", "tolink"], [stored, CRYPTER]]
        )
        self.store(rows)

        offer, handout = self.handout_naming(fingerprint_of(stored))

        inventory = enumerate_filecrypt_candidates(
            self.state.databases["protected"].retrieve_all_titles()
        )
        occurrence = next(
            candidate.occurrences[0]
            for candidate in inventory.candidates
            if candidate.fingerprint == offer["link_fingerprint"]
        )
        self.assertEqual(1, occurrence.link_index)
        urls = [entry[0] for entry in handout["url"]]
        self.assertEqual(occurrence.package_id, handout["id"])
        self.assertEqual(stored, urls[0])

    def test_a_fingerprint_shared_by_two_packages_hands_out_the_lowest_one(self):
        shared_url = filecrypt_url(1)
        rows = filecrypt_rows(4, start=3)
        rows[package(1)] = protected_blob([[shared_url, CRYPTER]])
        rows[package(2)] = protected_blob([[f"{shared_url}#other", CRYPTER]])
        self.store(rows)

        _offer, handout = self.handout_naming(fingerprint_of(shared_url))

        self.assertEqual(package(1), handout["id"])
        self.assertEqual([shared_url], [entry[0] for entry in handout["url"]])


class ManualProbeTargetingTests(CohortApiTestCase):
    """A queued probe authorizes one package, so the offer must name its member."""

    def cooled_cohort(self):
        self.store(filecrypt_rows(5))
        self.drive_blocked(5)
        self.assertEqual("cooldown", self.decision_row()["state"])

    def package_other_than_the_first_member(self):
        first = self.decision_row()["members"][0]["link_fingerprint"]
        return next(
            package(index)
            for index in range(1, 6)
            if fingerprint_of(filecrypt_url(index)) != first
        )

    def test_a_probe_offers_the_queued_package_not_the_cohort_head(self):
        self.cooled_cohort()
        target = self.package_other_than_the_first_member()
        self.service().request_probe([target])

        handout = self.to_decrypt()

        index = int(target.rsplit("_", 1)[1], 16)
        self.assertEqual(target, handout["id"])
        self.assertEqual("probe", handout["crypter_offer"]["mode"])
        self.assertEqual(
            fingerprint_of(filecrypt_url(index)),
            handout["crypter_offer"]["link_fingerprint"],
        )
        self.assertEqual([filecrypt_url(index)], [entry[0] for entry in handout["url"]])
        self.assertIs(
            False, self.service().get_package_defer(target)["probe_requested"]
        )

    def test_a_probe_on_a_multi_member_package_offers_its_lowest_stored_link(self):
        rows = filecrypt_rows(3, start=2)
        rows[package(1)] = protected_blob(
            [[filecrypt_url(1), CRYPTER], [filecrypt_url(9), CRYPTER]]
        )
        self.store(rows)
        self.drive_blocked(5)
        self.assertEqual("cooldown", self.decision_row()["state"])
        self.service().request_probe([package(1)])

        handout = self.to_decrypt()

        self.assertEqual(package(1), handout["id"])
        self.assertEqual(
            fingerprint_of(filecrypt_url(1)),
            handout["crypter_offer"]["link_fingerprint"],
        )
        self.assertEqual([filecrypt_url(1)], [entry[0] for entry in handout["url"]])

    def test_an_untyped_handout_never_spends_a_cohort_probe(self):
        self.cooled_cohort()
        target = self.package_other_than_the_first_member()
        self.service().request_probe([target])

        with self.assertRaises(HTTPError) as raised:
            self.to_decrypt(capabilities=[CRYPTER_DEFER_CAPABILITY])

        self.assertEqual(404, raised.exception.status_code)
        self.assertIs(True, self.service().get_package_defer(target)["probe_requested"])


class OfferOwnershipTests(CohortApiTestCase):
    """A report may only write rows the reporting package still owns."""

    def stranger_for(self, package_id):
        return next(
            package(index) for index in range(1, 6) if package(index) != package_id
        )

    def assert_stale_and_untouched(self, payload):
        packages = dict(self.state.databases["protected"].rows)

        response = self.call(DEFER_RULE, payload)

        self.assertEqual("stale", response["instruction"])
        self.assertEqual(0, response["retry_after_epoch"])
        self.assertEqual("none", response["hold_type"])
        self.assertEqual(packages, self.state.databases["protected"].rows)

    def test_a_report_from_a_package_without_the_offered_link_is_stale(self):
        self.store(filecrypt_rows(5))
        offer, handout = self.handout_offer()

        self.assert_stale_and_untouched(
            self.blocked_payload(offer, self.stranger_for(handout["id"]))
        )

    def test_a_report_naming_an_arbitrary_canonical_package_is_stale(self):
        self.store(filecrypt_rows(5))
        offer, _handout = self.handout_offer()

        self.assert_stale_and_untouched(self.blocked_payload(offer, package(999)))

    def test_a_report_from_a_deleted_package_is_stale(self):
        self.store(filecrypt_rows(5))
        offer, handout = self.handout_offer()
        self.state.databases["protected"].rows.pop(handout["id"])

        self.assert_stale_and_untouched(self.blocked_payload(offer, handout["id"]))

    def test_a_report_from_a_package_whose_links_changed_is_stale(self):
        self.store(filecrypt_rows(5))
        offer, handout = self.handout_offer()
        self.state.databases["protected"].update_store(
            handout["id"], protected_blob([[filecrypt_url(77), CRYPTER]])
        )

        self.assert_stale_and_untouched(self.blocked_payload(offer, handout["id"]))

    def test_a_stale_access_report_from_a_foreign_package_releases_nothing(self):
        self.store(filecrypt_rows(5))
        self.drive_blocked(3)
        offer, handout = self.handout_offer()
        held = dict(self.state.databases["protected"].rows)

        response = self.call(
            ACCESS_RULE,
            self.access_payload(offer, self.stranger_for(handout["id"])),
        )

        self.assertIsInstance(response, HTTPResponse)
        self.assertEqual(409, response.status_code)
        self.assertEqual("stale", json.loads(response.body)["instruction"])
        self.assertEqual(held, self.state.databases["protected"].rows)
        self.assertEqual("sweeping", self.decision_row()["state"])

    def test_an_unreadable_inventory_holds_the_package_that_proves_the_link(self):
        self.store(filecrypt_rows(5))
        offer, handout = self.handout_offer()

        with mock.patch(
            "quasarr.api.sponsors_helper.enumerate_filecrypt_candidates",
            side_effect=RuntimeError("inventory unavailable"),
        ):
            response = self.call(DEFER_RULE, self.blocked_payload(offer, handout["id"]))

        self.assertEqual("hold", response["instruction"])
        self.assertEqual("inventory_unavailable", self.decision_row()["reason"])
        deferred = self.service().get_package_defer(handout["id"])
        self.assertEqual([offer["link_fingerprint"]], deferred["link_fingerprints"])

    def test_an_unreadable_inventory_disproves_a_readable_foreign_row(self):
        self.store(filecrypt_rows(5))
        offer, handout = self.handout_offer()
        stranger = self.stranger_for(handout["id"])
        rows = {
            table: dict(database.rows)
            for table, database in self.state.databases.items()
        }

        with mock.patch(
            "quasarr.api.sponsors_helper.enumerate_filecrypt_candidates",
            side_effect=RuntimeError("inventory unavailable"),
        ):
            response = self.call(DEFER_RULE, self.blocked_payload(offer, stranger))

        # The narrow row read proved the mismatch, so the blind taint the
        # inventory outage would otherwise install is never reached.
        self.assertEqual("stale", response["instruction"])
        self.assertEqual(
            rows,
            {
                table: dict(database.rows)
                for table, database in self.state.databases.items()
            },
        )


class ProvenOwnershipTests(CohortApiTestCase):
    """Ownership is proven, disproven, or unknown, and each answers differently."""

    def setUp(self):
        super().setUp()
        self.store(filecrypt_rows(5))
        self.renew_offer()

    def renew_offer(self):
        self.offer, handout = self.handout_offer()
        self.owner = handout["id"]
        self.stranger = next(
            package(index) for index in range(1, 6) if package(index) != self.owner
        )

    def all_rows(self):
        """Every row of every table, so a write anywhere at all is visible."""
        return {
            table: dict(database.rows)
            for table, database in self.state.databases.items()
        }

    def blind(self):
        return mock.patch(
            "quasarr.api.sponsors_helper.enumerate_filecrypt_candidates",
            side_effect=RuntimeError("inventory unavailable"),
        )

    def unreadable_owner_row(self):
        return mock.patch.object(
            self.state.databases["protected"],
            "retrieve",
            side_effect=RuntimeError("dbfile=/srv/secret/Quasarr.db"),
        )

    def assert_untouched(self, rule, payload, *, blind=False):
        before = self.all_rows()
        if blind:
            with self.blind():
                response = self.call(rule, payload)
        else:
            response = self.call(rule, payload)
        self.assertEqual(before, self.all_rows())
        return response

    def assert_stale_defer(self, package_id, *, blind=False):
        response = self.assert_untouched(
            DEFER_RULE, self.blocked_payload(self.offer, package_id), blind=blind
        )

        self.assertEqual("stale", response["instruction"])
        self.assertEqual("none", response["hold_type"])
        self.assertEqual(0, response["retry_after_epoch"])

    def assert_stale_access(self, package_id, *, access="clear", blind=False):
        response = self.assert_untouched(
            ACCESS_RULE,
            self.access_payload(self.offer, package_id, access=access),
            blind=blind,
        )

        self.assertIsInstance(response, HTTPResponse)
        self.assertEqual(409, response.status_code)
        body = json.loads(response.body)
        self.assertEqual("stale", body["instruction"])
        self.assertNotIn("cleared", body)

    def assert_blind_taint(self):
        packages = dict(self.state.databases["protected"].rows)

        with self.blind():
            response = self.call(
                DEFER_RULE, self.blocked_payload(self.offer, self.owner)
            )

        self.assertEqual("hold", response["instruction"])
        self.assertEqual("individual", response["state"])
        self.assertEqual("inventory_unavailable", self.decision_row()["reason"])
        self.assertEqual(packages, self.state.databases["protected"].rows)
        return response

    def test_a_proven_owner_still_records_the_report_it_names(self):
        response = self.call(DEFER_RULE, self.blocked_payload(self.offer, self.owner))

        self.assertEqual("hold", response["instruction"])
        self.assertEqual(
            [self.offer["link_fingerprint"]],
            self.service().get_package_defer(self.owner)["link_fingerprints"],
        )

    def test_a_readable_inventory_disproves_a_foreign_reporter(self):
        self.assert_stale_defer(self.stranger)

    def test_a_disproven_report_never_even_prunes_the_decision(self):
        """A report that proved nothing may not decide the row has expired."""
        self.clock.now = NOW + 121  # the unanswered lease of setUp has expired
        self.drive_blocked(5)
        self.assertEqual("cooldown", self.decision_row()["state"])
        self.clock.now = NOW + COOLDOWN_SECONDS + 200

        self.assert_stale_defer(self.stranger)

        self.assertEqual("cooldown", self.decision_row()["state"])

    def test_a_deleted_row_can_never_disprove_a_blind_reporter(self):
        self.state.databases["protected"].rows.pop(self.owner)

        self.assert_blind_taint()

    def test_a_malformed_row_can_never_disprove_a_blind_reporter(self):
        self.state.databases["protected"].update_store(self.owner, "{not json")

        self.assert_blind_taint()

    def test_an_ineligible_row_can_never_be_held_by_a_blind_reporter(self):
        """It still carries the link, so it is unknown - and unknown holds nothing."""
        row = json.loads(self.state.databases["protected"].rows[self.owner])
        row["disabled"] = True
        self.state.databases["protected"].update_store(self.owner, json.dumps(row))

        self.assert_blind_taint()

    def test_a_storage_error_is_unknown_and_never_reaches_the_helper(self):
        packages = dict(self.state.databases["protected"].rows)

        with (
            self.blind(),
            self.unreadable_owner_row(),
            mock.patch("quasarr.providers.crypter_cooldowns.warn") as warned,
        ):
            response = self.call(
                DEFER_RULE, self.blocked_payload(self.offer, self.owner)
            )

        self.assertEqual("hold", response["instruction"])
        self.assertEqual("inventory_unavailable", self.decision_row()["reason"])
        self.assertEqual(packages, self.state.databases["protected"].rows)
        logged = " ".join(str(call.args[0]) for call in warned.call_args_list)
        self.assertNotIn("/srv/secret/Quasarr.db", logged)
        self.assertNotIn("/srv/secret/Quasarr.db", json.dumps(response))
        self.assertIn(self.owner, logged)

    def test_a_foreign_reporter_can_never_clear_the_generation(self):
        self.drive_blocked(3)
        self.renew_offer()

        self.assert_stale_access(self.stranger)

        self.assertEqual("sweeping", self.decision_row()["state"])

    def test_an_unprovable_reporter_can_never_clear_the_generation(self):
        self.drive_blocked(3)
        self.renew_offer()
        before = self.all_rows()

        with self.blind(), self.unreadable_owner_row():
            response = self.call(
                ACCESS_RULE, self.access_payload(self.offer, self.owner)
            )

        # Health is evidence about a container, so a binding this could not
        # authenticate may not end the generation - and may not taint it either.
        self.assertEqual(409, response.status_code)
        self.assertEqual("stale", json.loads(response.body)["instruction"])
        self.assertEqual(before, self.all_rows())
        self.assertEqual("sweeping", self.decision_row()["state"])

    def test_an_unprovable_reporter_may_still_report_an_unknown_access(self):
        self.state.databases["protected"].rows.pop(self.owner)
        packages = dict(self.state.databases["protected"].rows)

        with self.blind():
            response = self.call(
                ACCESS_RULE,
                self.access_payload(self.offer, self.owner, access="unknown"),
            )

        self.assertEqual("unknown", response["accepted"])
        self.assertEqual("sweeping", response["state"])
        self.assertEqual(1, response["sweep_tested"])
        self.assertEqual(packages, self.state.databases["protected"].rows)

    def test_a_foreign_reporter_cannot_even_report_an_unknown_access(self):
        self.assert_stale_access(self.stranger, access="unknown")

    def test_a_disproven_report_leaves_its_offer_replayable_for_the_owner(self):
        self.assert_stale_defer(self.stranger)

        accepted = self.call(DEFER_RULE, self.blocked_payload(self.offer, self.owner))
        self.clock.now = NOW + 30
        replay = self.call(DEFER_RULE, self.blocked_payload(self.offer, self.owner))

        self.assertEqual("hold", accepted["instruction"])
        self.assertEqual(accepted, replay)
        self.assertEqual(
            [self.offer["offer_id"]],
            [entry["offer_id"] for entry in self.decision_row()["accepted_offers"]],
        )

    def test_an_accepted_result_is_never_replayable_by_a_stranger(self):
        self.call(DEFER_RULE, self.blocked_payload(self.offer, self.owner))

        self.assert_stale_defer(self.stranger)


class VersionOnePrecedenceRaceTests(CohortApiTestCase):
    """The version-one answer follows the row current inside its own transaction."""

    def legacy_report(self, package_id=None):
        return self.call(
            DEFER_RULE,
            {
                "package_id": package_id or package(1),
                "crypter": CRYPTER,
                "reason_code": REASON,
                "link_fingerprint": fingerprint_of(filecrypt_url(1)),
            },
        )

    def test_a_sweep_installed_before_the_transaction_wins_the_answer(self):
        self.store(filecrypt_rows(5))
        self.to_decrypt()
        sweeping = self.state.databases["crypter_cooldowns"].rows.pop(CRYPTER)
        packages = dict(self.state.databases["protected"].rows)

        def install_sweep():
            self.state.databases["crypter_cooldowns"].rows[CRYPTER] = sweeping

        self.state.databases["crypter_cooldowns"].before_mutation = install_sweep

        response = self.legacy_report()

        self.assertEqual("legacy_failure", response["instruction"])
        self.assertEqual("none", response["hold_type"])
        self.assertEqual(0, response["retry_after_epoch"])
        self.assertEqual(
            sweeping, self.state.databases["crypter_cooldowns"].rows[CRYPTER]
        )
        self.assertEqual(packages, self.state.databases["protected"].rows)

    def test_a_hold_written_before_the_transaction_is_read_inside_it(self):
        self.store(filecrypt_rows(5))
        first = self.legacy_report()
        self.assertEqual("hold", first["instruction"])
        held = json.dumps(self.package_row(package(1)))
        self.state.databases["protected"].rows[package(1)] = protected_blob(
            [[filecrypt_url(1), CRYPTER]]
        )

        def restore_hold():
            self.state.databases["protected"].rows[package(1)] = held

        # The hook runs at the start of the one transaction, so a route that
        # pre-read the package would still answer from the unheld row.
        self.state.databases["crypter_cooldowns"].before_mutation = restore_hold

        second = self.legacy_report()

        self.assertEqual("legacy_failure", second["instruction"])
        self.assertEqual("none", second["hold_type"])


class LegacyClearOrderingTests(CohortApiTestCase):
    """Health is proven first; every physical release is best effort after it."""

    def cohort_hold(self):
        self.store(filecrypt_rows(5))
        self.drive_blocked(5)
        target = next(
            package(index)
            for index in range(1, 6)
            if self.service().get_package_defer(package(index))
        )
        return target

    def clear(self, package_id):
        return self.call(
            ACCESS_RULE,
            {"package_id": package_id, "crypter": CRYPTER, "access": "clear"},
        )

    def test_a_failed_health_commit_releases_no_package_hold(self):
        target = self.cohort_hold()
        packages = dict(self.state.databases["protected"].rows)
        crypter_db = self.state.databases["crypter_cooldowns"]

        with mock.patch.object(
            crypter_db, "mutate_value", side_effect=RuntimeError("health commit failed")
        ):
            with self.assertRaises(HTTPError) as raised:
                self.clear(target)

        self.assertEqual(500, raised.exception.status_code)
        self.assertEqual(packages, self.state.databases["protected"].rows)
        self.assertEqual("cooldown", self.decision_row()["state"])

    def test_a_failed_package_cleanup_still_acknowledges_proven_health(self):
        target = self.cohort_hold()
        protected_db = self.state.databases["protected"]

        with mock.patch.object(
            protected_db, "mutate_value", side_effect=RuntimeError("cleanup failed")
        ):
            response = self.clear(target)

        self.assertEqual(
            {"success": True, "state": "available", "cleared": True}, response
        )
        self.assertEqual("healthy", self.decision_row()["state"])

    def test_a_newer_generation_row_survives_the_cleanup(self):
        target = self.cohort_hold()
        protected_db = self.state.databases["protected"]
        newer = {
            "crypter": CRYPTER,
            "reason_code": REASON,
            "since_epoch": NOW,
            "retry_after_epoch": NOW + SWEEP_WINDOW,
            "probe_requested": False,
            "observation_holds": 1,
            "schema_version": 2,
            "sweep_id": "e" * 32,
            "link_fingerprints": [fingerprint_of(filecrypt_url(1))],
        }

        def install_newer_generation():
            row = json.loads(protected_db.rows[target])
            row["deferred"] = newer
            protected_db.rows[target] = json.dumps(row)

        protected_db.before_mutation = install_newer_generation

        response = self.clear(target)

        self.assertIs(True, response["cleared"])
        self.assertEqual(newer, self.package_row(target)["deferred"])

    def test_a_legacy_hold_of_the_reporting_package_is_physically_released(self):
        self.store(filecrypt_rows(1))
        self.call(
            DEFER_RULE,
            {
                "package_id": package(1),
                "crypter": CRYPTER,
                "reason_code": REASON,
                "link_fingerprint": fingerprint_of(filecrypt_url(1)),
            },
        )
        self.assertIsNotNone(self.service().get_package_defer(package(1)))

        self.clear(package(1))

        self.assertNotIn("deferred", self.package_row(package(1)))
        self.assertEqual("healthy", self.decision_row()["state"])


class RealDatabaseCohortTests(unittest.TestCase):
    """The whole route path against the real SQLite storage layer."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.dbfile = os.path.join(self.tmpdir.name, "Quasarr.db")
        self.original_values = provider_shared_state.values
        self.original_lock = provider_shared_state.lock
        provider_shared_state.values = {"dbfile": self.dbfile}
        provider_shared_state.lock = None
        self.addCleanup(self.restore_shared_state)
        self.databases = {}
        self.clock = FakeClock(NOW)
        self.ids = SequentialIds()
        self.state = self.RealSharedState(self.databases, self.dbfile)
        patcher = mock.patch.object(
            CrypterCooldownService, "_new_identifier", lambda _self: self.ids()
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    class RealSharedState:
        def __init__(self, databases, dbfile):
            self._databases = databases
            self.values = {
                "dbfile": dbfile,
                "crypter_cooldown_hours": 24,
                "crypter_block_mode": "defer",
            }

        def get_db(self, table):
            if table not in self._databases:
                self._databases[table] = DataBase(table)
            return self._databases[table]

        def update(self, key, value):
            self.values[key] = value

    def restore_shared_state(self):
        for database in self.databases.values():
            database._conn.close()
        self.databases.clear()
        provider_shared_state.values = self.original_values
        provider_shared_state.lock = self.original_lock

    def call(self, rule, payload):
        route = route_for(rule)
        with (
            mock.patch("quasarr.api.sponsors_helper.shared_state", self.state),
            mock.patch("quasarr.api.sponsors_helper.request", mock.Mock(json=payload)),
            mock.patch(
                "quasarr.api.sponsors_helper.CrypterCooldownService",
                lambda state: CrypterCooldownService(state, clock=self.clock),
            ),
        ):
            return route.callback()

    def test_a_complete_cohort_cools_down_through_the_real_storage_layer(self):
        protected = self.state.get_db("protected")
        for index in range(1, 6):
            protected.update_store(
                package(index), protected_blob([[filecrypt_url(index), CRYPTER]])
            )

        response = None
        for _ in range(5):
            handout = self.call(
                DECRYPT_RULE,
                {
                    "supported_urls": ["filecrypt.invalid"],
                    "capabilities": CAPABILITIES,
                },
            )["to_decrypt"]
            offer = handout["crypter_offer"]
            urls = [entry[0] for entry in handout["url"]]
            self.assertEqual(1, len(urls))
            self.assertEqual(offer["link_fingerprint"], fingerprint_of(urls[0]))
            response = self.call(
                DEFER_RULE,
                {
                    "package_id": handout["id"],
                    "crypter": CRYPTER,
                    "reason_code": REASON,
                    "link_fingerprint": offer["link_fingerprint"],
                    "sweep_id": offer["sweep_id"],
                    "offer_id": offer["offer_id"],
                },
            )

        self.assertEqual("cooldown", response["instruction"])
        self.assertEqual(5, response["evidence_count"])
        service = CrypterCooldownService(self.state, clock=self.clock)
        self.assertEqual("cooldown", service.snapshot(CRYPTER)["state"])
        self.assertEqual(5, service.count_active_deferred_packages())

    def test_a_real_clear_releases_every_stored_hold(self):
        protected = self.state.get_db("protected")
        for index in range(1, 6):
            protected.update_store(
                package(index), protected_blob([[filecrypt_url(index), CRYPTER]])
            )
        for _ in range(3):
            handout = self.call(
                DECRYPT_RULE,
                {
                    "supported_urls": ["filecrypt.invalid"],
                    "capabilities": CAPABILITIES,
                },
            )["to_decrypt"]
            offer = handout["crypter_offer"]
            self.call(
                DEFER_RULE,
                {
                    "package_id": handout["id"],
                    "crypter": CRYPTER,
                    "reason_code": REASON,
                    "link_fingerprint": offer["link_fingerprint"],
                    "sweep_id": offer["sweep_id"],
                    "offer_id": offer["offer_id"],
                },
            )

        handout = self.call(
            DECRYPT_RULE,
            {"supported_urls": ["filecrypt.invalid"], "capabilities": CAPABILITIES},
        )["to_decrypt"]
        offer = handout["crypter_offer"]
        response = self.call(
            ACCESS_RULE,
            {
                "package_id": handout["id"],
                "crypter": CRYPTER,
                "access": "clear",
                "link_fingerprint": offer["link_fingerprint"],
                "sweep_id": offer["sweep_id"],
                "offer_id": offer["offer_id"],
            },
        )

        self.assertIs(True, response["cleared"])
        service = CrypterCooldownService(self.state, clock=self.clock)
        self.assertEqual(0, service.count_active_deferred_packages())
        for index in range(1, 6):
            self.assertIsNone(service.get_package_defer(package(index)))

    def test_a_sweep_that_opens_before_the_transaction_wins_the_legacy_answer(self):
        protected = self.state.get_db("protected")
        for index in range(1, 6):
            protected.update_store(
                package(index), protected_blob([[filecrypt_url(index), CRYPTER]])
            )
        self.call(
            DECRYPT_RULE,
            {"supported_urls": ["filecrypt.invalid"], "capabilities": CAPABILITIES},
        )
        cooldowns = self.state.get_db("crypter_cooldowns")
        sweeping = cooldowns.retrieve(CRYPTER)
        cooldowns.mutate_value(CRYPTER, lambda _current: None)
        self.assertIsNone(cooldowns.retrieve(CRYPTER))

        original = DataBase.mutate_values
        installed = []

        def install_sweep_then_commit(database, targets, mutator):
            if not installed:
                installed.append(True)
                competitor = DataBase("crypter_cooldowns")
                try:
                    competitor.update_store(CRYPTER, sweeping)
                finally:
                    competitor._conn.close()
            return original(database, targets, mutator)

        with mock.patch.object(DataBase, "mutate_values", install_sweep_then_commit):
            response = self.call(
                DEFER_RULE,
                {
                    "package_id": package(1),
                    "crypter": CRYPTER,
                    "reason_code": REASON,
                    "link_fingerprint": fingerprint_of(filecrypt_url(1)),
                },
            )

        self.assertEqual([True], installed)
        self.assertEqual("legacy_failure", response["instruction"])
        self.assertEqual("none", response["hold_type"])
        self.assertEqual(0, response["retry_after_epoch"])
        self.assertEqual(sweeping, cooldowns.retrieve(CRYPTER))
        self.assertNotIn("deferred", json.loads(protected.retrieve(package(1))))

    def stored_rows(self):
        return {
            table: dict(self.state.get_db(table).retrieve_all_titles() or ())
            for table in ("protected", "crypter_cooldowns", "crypter_events")
        }

    def real_handout(self):
        return self.call(
            DECRYPT_RULE,
            {"supported_urls": ["filecrypt.invalid"], "capabilities": CAPABILITIES},
        )["to_decrypt"]

    def real_cohort(self, blocked=0):
        protected = self.state.get_db("protected")
        for index in range(1, 6):
            protected.update_store(
                package(index), protected_blob([[filecrypt_url(index), CRYPTER]])
            )
        for _ in range(blocked):
            handout = self.real_handout()
            offer = handout["crypter_offer"]
            self.call(
                DEFER_RULE,
                {
                    "package_id": handout["id"],
                    "crypter": CRYPTER,
                    "reason_code": REASON,
                    "link_fingerprint": offer["link_fingerprint"],
                    "sweep_id": offer["sweep_id"],
                    "offer_id": offer["offer_id"],
                },
            )
        handout = self.real_handout()
        return handout, handout["crypter_offer"]

    def real_clear(self, offer, package_id):
        return self.call(
            ACCESS_RULE,
            {
                "package_id": package_id,
                "crypter": CRYPTER,
                "access": "clear",
                "link_fingerprint": offer["link_fingerprint"],
                "sweep_id": offer["sweep_id"],
                "offer_id": offer["offer_id"],
            },
        )

    def test_a_foreign_clear_changes_no_real_row(self):
        handout, offer = self.real_cohort(blocked=3)
        stranger = next(
            package(index) for index in range(1, 6) if package(index) != handout["id"]
        )
        before = self.stored_rows()

        response = self.real_clear(offer, stranger)

        self.assertEqual(409, response.status_code)
        self.assertEqual("stale", json.loads(response.body)["instruction"])
        self.assertEqual(before, self.stored_rows())
        service = CrypterCooldownService(self.state, clock=self.clock)
        self.assertEqual(3, service.count_active_deferred_packages())

    def test_an_unprovable_clear_changes_no_real_row(self):
        handout, offer = self.real_cohort(blocked=3)
        self.state.get_db("protected").delete(handout["id"])
        before = self.stored_rows()

        with mock.patch(
            "quasarr.api.sponsors_helper.enumerate_filecrypt_candidates",
            side_effect=RuntimeError("inventory unavailable"),
        ):
            response = self.real_clear(offer, handout["id"])

        self.assertEqual(409, response.status_code)
        self.assertEqual("stale", json.loads(response.body)["instruction"])
        self.assertEqual(before, self.stored_rows())
        self.assertEqual(
            "sweeping",
            json.loads(self.state.get_db("crypter_cooldowns").retrieve(CRYPTER))[
                "state"
            ],
        )


if __name__ == "__main__":
    unittest.main()
