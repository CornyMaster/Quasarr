# -*- coding: utf-8 -*-

import json
import threading
import unittest
from unittest import mock

from bottle import Bottle, HTTPError

from quasarr.api.sponsors_helper import (
    normalize_helper_supported_urls,
    select_helper_package,
    setup_sponsors_helper_routes,
)
from quasarr.providers.auth import audit_route_auth_modes
from quasarr.providers.crypter_candidates import (
    enumerate_filecrypt_candidates,
    link_fingerprint,
)
from quasarr.providers.crypter_cooldowns import (
    CrypterCooldownService,
    CrypterProjection,
)

PACKAGE_A = "Quasarr_movies_00000000000000000000000000000000"
PACKAGE_B = "Quasarr_movies_11111111111111111111111111111111"
PACKAGE_C = "Quasarr_movies_22222222222222222222222222222222"
PACKAGE_D = "Quasarr_movies_33333333333333333333333333333333"
# Custom download categories may contain digits (add_download_category allows
# ^[a-z0-9]+$), so package IDs built from them are canonical Quasarr IDs.
CUSTOM_CATEGORY_PACKAGE = "Quasarr_movies4k_44444444444444444444444444444444"
NONCONFORMING_PACKAGE = "Quasarr_movies_00000000000000000000000000000001"
NOW = 1_700_000_000


def protected_package(package_id, title, links, **extra):
    data = {
        "title": title,
        "links": links,
        "password": "",
    }
    data.update(extra)
    return package_id, json.dumps(data)


def package_defer(crypter="filecrypt", active=True, probe_requested=False):
    return {
        "crypter": crypter,
        "reason_code": "ip_block_suspected",
        "since_epoch": NOW - 60,
        "retry_after_epoch": NOW + 900 if active else NOW - 1,
        "probe_requested": probe_requested,
        "observation_holds": 1,
    }


class FakeCooldownService:
    def __init__(
        self,
        cooling_crypters=(),
        package_defers=None,
        failed_probe_consumptions=(),
    ):
        self.cooling_crypters = set(cooling_crypters)
        self.package_defers = package_defers or {}
        self.failed_probe_consumptions = set(failed_probe_consumptions)
        self.probe_consumptions = []
        self.projections = []
        self.projected_decisions = []

    def snapshot(self, crypter):
        cooling = crypter in self.cooling_crypters
        return {
            "state": "cooldown" if cooling else "available",
            "reason_code": "ip_block_suspected" if cooling else None,
            "first_seen_epoch": NOW - 120 if cooling else 0,
            "last_seen_epoch": NOW - 60 if cooling else 0,
            "retry_after_epoch": NOW + 86_400 if cooling else 0,
            "observations": [],
            "evidence_count": 3 if cooling else 0,
        }

    def is_cooling(self, crypter):
        return crypter in self.cooling_crypters

    def crypter_projection(self, crypter):
        self.projections.append(crypter)
        return CrypterProjection(self.snapshot(crypter), None)

    def get_package_defer(self, package_id):
        return self.package_defers.get(package_id)

    def project_package_defer(self, deferred, snapshot, decision_snapshot=None):
        self.projected_decisions.append(decision_snapshot)
        crypter_retry_after = (
            snapshot["retry_after_epoch"] if snapshot["state"] == "cooldown" else 0
        )
        retry_after_epoch = max(deferred["retry_after_epoch"], crypter_retry_after)
        if crypter_retry_after > NOW:
            hold_type = "crypter_cooldown"
        elif retry_after_epoch > NOW:
            hold_type = "provisional"
        else:
            hold_type = "none"
        projected = dict(deferred)
        projected.update(
            {
                "retry_after_epoch": retry_after_epoch,
                "state": snapshot["state"],
                "evidence_count": snapshot["evidence_count"],
                "hold_type": hold_type,
                "active": hold_type != "none",
            }
        )
        return projected

    def consume_probe(self, package_id, crypter):
        self.probe_consumptions.append((package_id, crypter))
        if package_id in self.failed_probe_consumptions:
            return False
        deferred = self.package_defers.get(package_id)
        if (
            not deferred
            or deferred.get("crypter") != crypter
            or not deferred.get("probe_requested")
        ):
            return False
        deferred["probe_requested"] = False
        return True


class AtomicDatabase:
    def __init__(self, rows=None, tables=None):
        self.rows = dict(rows or {})
        self.tables = {} if tables is None else tables
        self.lock = threading.Lock()
        self.before_mutation = None

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
            if value is not None and not isinstance(value, str):
                raise TypeError("mutator must return str or None")
            if value is None:
                self.rows.pop(key, None)
            else:
                self.rows[key] = value
            return value

    def mutate_values(self, targets, mutator):
        """One transaction over several tables, like the sqlite primitive."""
        with self.lock:
            self._interleave()
            databases = [self._peer(table) for table, _key in targets]
            values = mutator(
                tuple(
                    database.rows.get(key)
                    for database, (_table, key) in zip(databases, targets, strict=True)
                )
            )
            for value in values:
                if value is not None and not isinstance(value, str):
                    raise TypeError("mutator must return str or None")
            for database, (_table, key), value in zip(
                databases, targets, values, strict=True
            ):
                if value is None:
                    database.rows.pop(key, None)
                else:
                    database.rows[key] = value
            return tuple(values)


class AtomicSharedState:
    def __init__(self, protected_rows):
        self.databases = {}
        self.databases["protected"] = AtomicDatabase(
            protected_rows, tables=self.databases
        )
        for table in ("crypter_cooldowns", "statistics"):
            self.databases[table] = AtomicDatabase(tables=self.databases)
        self.values = {"database": self.get_db}

    def get_db(self, table):
        if table not in self.databases:
            self.databases[table] = AtomicDatabase(tables=self.databases)
        return self.databases[table]

    def update(self, key, value):
        self.values[key] = value


class SponsorsHelperApiTests(unittest.TestCase):
    def deferred_state(self, links=None):
        if links is None:
            links = [["https://filecrypt.invalid/container/1", "filecrypt"]]
        state = AtomicSharedState(
            dict([protected_package(PACKAGE_A, "Deferred.Package", links)])
        )
        service = CrypterCooldownService(state, clock=lambda: NOW)
        decision = service.observe(
            "filecrypt",
            PACKAGE_A,
            "a" * 64,
            "ip_block_suspected",
        )
        service.defer_package(
            PACKAGE_A,
            "filecrypt",
            "ip_block_suspected",
            decision["package_retry_after_epoch"],
            observation_holds=1,
        )
        return state, service

    def call_json_route(self, rule, state, payload, cooldown_service):
        app = Bottle()
        setup_sponsors_helper_routes(app)
        route = next(route for route in app.routes if route.rule == rule)

        with (
            mock.patch("quasarr.api.sponsors_helper.shared_state", state),
            mock.patch(
                "quasarr.api.sponsors_helper.request",
                mock.Mock(json=payload),
            ),
            mock.patch(
                "quasarr.api.sponsors_helper.CrypterCooldownService",
                return_value=cooldown_service,
            ),
        ):
            return route.callback()

    def call_to_decrypt(self, protected_packages, payload, cooldown_service=None):
        app = Bottle()
        setup_sponsors_helper_routes(app)
        route = next(
            route
            for route in app.routes
            if route.rule == "/sponsors_helper/api/to_decrypt/"
        )
        protected_db = mock.Mock()
        protected_db.retrieve_all_titles.return_value = protected_packages

        with (
            # Cooldown-aware selection only applies in the default block mode,
            # so pin it instead of inheriting process-global shared state.
            mock.patch.dict(
                "quasarr.api.sponsors_helper.shared_state.values",
                {"crypter_block_mode": "defer"},
            ),
            mock.patch("quasarr.api.sponsors_helper.shared_state.update"),
            mock.patch(
                "quasarr.api.sponsors_helper.shared_state.get_db",
                return_value=protected_db,
            ),
            mock.patch(
                "quasarr.api.sponsors_helper.request",
                mock.Mock(json=payload),
            ),
            mock.patch(
                "quasarr.api.sponsors_helper.CrypterCooldownService",
                return_value=cooldown_service or FakeCooldownService(),
            ) as cooldown_type,
        ):
            return route.callback(), cooldown_type

    def test_to_decrypt_route_accepts_only_post(self):
        app = Bottle()
        setup_sponsors_helper_routes(app)

        methods = {
            route.method
            for route in app.routes
            if route.rule == "/sponsors_helper/api/to_decrypt/"
        }

        self.assertEqual({"POST"}, methods)

    def test_crypter_report_routes_are_post_only_and_api_key_authenticated(self):
        app = Bottle()
        setup_sponsors_helper_routes(app)

        methods_by_rule = {
            rule: {route.method for route in app.routes if route.rule == rule}
            for rule in (
                "/sponsors_helper/api/defer/",
                "/sponsors_helper/api/crypter-access/",
            )
        }

        self.assertEqual(
            {
                "/sponsors_helper/api/defer/": {"POST"},
                "/sponsors_helper/api/crypter-access/": {"POST"},
            },
            methods_by_rule,
        )
        audit_route_auth_modes(
            app,
            api_key_prefixes=("/sponsors_helper/api/",),
            public_whitelist=(),
        )

    def test_defer_first_observation_returns_exact_provisional_hold(self):
        state = AtomicSharedState(
            dict(
                [
                    protected_package(
                        PACKAGE_A,
                        "First.Observation",
                        [["https://filecrypt.invalid/container/1", "filecrypt"]],
                    )
                ]
            )
        )
        service = CrypterCooldownService(state, clock=lambda: NOW)

        with (
            mock.patch("quasarr.api.sponsors_helper.fail") as fail,
            mock.patch("quasarr.api.sponsors_helper.StatsHelper") as stats,
            mock.patch(
                "quasarr.api.sponsors_helper.update_release_notification"
            ) as notify,
        ):
            result = self.call_json_route(
                "/sponsors_helper/api/defer/",
                state,
                {
                    "package_id": PACKAGE_A,
                    "crypter": "filecrypt",
                    "reason_code": "ip_block_suspected",
                    "link_fingerprint": "a" * 64,
                },
                service,
            )

        self.assertEqual(
            {
                "success": True,
                "instruction": "hold",
                "state": "observing",
                "evidence_count": 1,
                "retry_after_epoch": NOW + 900,
                "hold_type": "provisional",
            },
            result,
        )
        self.assertIn(PACKAGE_A, state.databases["protected"].rows)
        deferred = service.get_package_defer(PACKAGE_A)
        self.assertEqual(1, deferred.get("observation_holds") if deferred else None)
        fail.assert_not_called()
        # The transition is counted by the transaction that recorded it, so the
        # route never depends on statistics storage at all.
        self.assertEqual([], stats.mock_calls)
        self.assertEqual(
            {"observations": 1, "cooldowns": 0, "probes": 0},
            json.loads(state.databases["crypter_events"].rows["pending"]),
        )
        notify.assert_not_called()

    def test_defer_third_distinct_observation_returns_exact_cooldown(self):
        packages = (
            (PACKAGE_A, "a"),
            (PACKAGE_B, "b"),
            (PACKAGE_C, "c"),
        )
        state = AtomicSharedState(
            dict(
                protected_package(
                    package_id,
                    f"Observation.{fingerprint}",
                    [
                        [
                            f"https://filecrypt.invalid/container/{fingerprint}",
                            "filecrypt",
                        ]
                    ],
                )
                for package_id, fingerprint in packages
            )
        )
        service = CrypterCooldownService(state, clock=lambda: NOW)

        responses = []
        for package_id, fingerprint in packages:
            responses.append(
                self.call_json_route(
                    "/sponsors_helper/api/defer/",
                    state,
                    {
                        "package_id": package_id,
                        "crypter": "filecrypt",
                        "reason_code": "ip_block_suspected",
                        "link_fingerprint": fingerprint * 64,
                    },
                    service,
                )
            )
        result = responses[-1]

        self.assertEqual(
            {
                "success": True,
                "instruction": "cooldown",
                "state": "cooldown",
                "evidence_count": 3,
                "retry_after_epoch": NOW + 86_400,
                "hold_type": "crypter_cooldown",
            },
            result,
        )
        deferred = service.get_package_defer(PACKAGE_C)
        self.assertEqual(0, deferred.get("observation_holds") if deferred else None)
        self.assertEqual(
            set(state.databases["protected"].rows), {PACKAGE_A, PACKAGE_B, PACKAGE_C}
        )

    def test_defer_second_isolated_report_returns_exact_legacy_failure(self):
        current_time = [NOW]
        state = AtomicSharedState(
            dict(
                [
                    protected_package(
                        PACKAGE_A,
                        "Repeated.Isolated",
                        [["https://filecrypt.invalid/container/1", "filecrypt"]],
                    )
                ]
            )
        )
        service = CrypterCooldownService(state, clock=lambda: current_time[0])
        payload = {
            "package_id": PACKAGE_A,
            "crypter": "filecrypt",
            "reason_code": "ip_block_suspected",
            "link_fingerprint": "a" * 64,
        }

        first = self.call_json_route(
            "/sponsors_helper/api/defer/", state, payload, service
        )
        current_time[0] += 901
        payload["link_fingerprint"] = "b" * 64
        second = self.call_json_route(
            "/sponsors_helper/api/defer/", state, payload, service
        )

        self.assertEqual("hold", first["instruction"])
        self.assertEqual(
            {
                "success": True,
                "instruction": "legacy_failure",
                "state": "observing",
                "evidence_count": 1,
                "retry_after_epoch": 0,
                "hold_type": "none",
            },
            second,
        )
        self.assertIn(PACKAGE_A, state.databases["protected"].rows)
        deferred = service.get_package_defer(PACKAGE_A)
        self.assertEqual(1, deferred.get("observation_holds") if deferred else None)
        self.assertEqual(
            NOW + 900, deferred.get("retry_after_epoch") if deferred else None
        )

    def test_defer_rejects_incomplete_payload_before_persistence_access(self):
        app = Bottle()
        setup_sponsors_helper_routes(app)
        route = next(
            route for route in app.routes if route.rule == "/sponsors_helper/api/defer/"
        )
        payloads = (
            None,
            [],
            {},
            {
                "package_id": PACKAGE_A,
                "crypter": "filecrypt",
                "reason_code": "ip_block_suspected",
            },
        )

        for payload in payloads:
            with self.subTest(payload=payload):
                with (
                    mock.patch(
                        "quasarr.api.sponsors_helper.request",
                        mock.Mock(json=payload),
                    ),
                    mock.patch(
                        "quasarr.api.sponsors_helper.CrypterCooldownService"
                    ) as cooldown_type,
                    self.assertRaises(HTTPError) as context,
                ):
                    route.callback()

                self.assertEqual(400, context.exception.status_code)
                cooldown_type.assert_not_called()

    def test_defer_rejects_malformed_fields_without_writes_or_failed_side_effects(self):
        valid_payload = {
            "package_id": PACKAGE_A,
            "crypter": "filecrypt",
            "reason_code": "ip_block_suspected",
            "link_fingerprint": "a" * 64,
        }
        malformed_fields = {
            "package_id": (None, "not-a-package-id"),
            "crypter": (None, "rapidgator"),
            "reason_code": (None, "unknown"),
            "link_fingerprint": (None, "A" * 64, "a" * 63),
        }

        for field, values in malformed_fields.items():
            for value in values:
                state = AtomicSharedState(
                    dict(
                        [
                            protected_package(
                                PACKAGE_A,
                                "Retained.Malformed.Request",
                                [
                                    [
                                        "https://filecrypt.invalid/container/1",
                                        "filecrypt",
                                    ]
                                ],
                            )
                        ]
                    )
                )
                service = CrypterCooldownService(state, clock=lambda: NOW)
                payload = dict(valid_payload)
                payload[field] = value

                with (
                    self.subTest(field=field, value=value),
                    mock.patch("quasarr.api.sponsors_helper.fail") as fail,
                    mock.patch("quasarr.api.sponsors_helper.StatsHelper") as stats,
                    mock.patch(
                        "quasarr.api.sponsors_helper.update_release_notification"
                    ) as notify,
                    self.assertRaises(HTTPError) as context,
                ):
                    self.call_json_route(
                        "/sponsors_helper/api/defer/",
                        state,
                        payload,
                        service,
                    )

                self.assertEqual(400, context.exception.status_code)
                self.assertIn(PACKAGE_A, state.databases["protected"].rows)
                self.assertEqual({}, state.databases["crypter_cooldowns"].rows)
                fail.assert_not_called()
                stats.assert_not_called()
                notify.assert_not_called()

    def test_defer_missing_package_keeps_evidence_without_failed_side_effects(self):
        state = AtomicSharedState({})
        service = CrypterCooldownService(state, clock=lambda: NOW)

        with (
            mock.patch("quasarr.api.sponsors_helper.fail") as fail,
            mock.patch("quasarr.api.sponsors_helper.StatsHelper") as stats,
            mock.patch(
                "quasarr.api.sponsors_helper.update_release_notification"
            ) as notify,
            self.assertRaises(HTTPError) as context,
        ):
            self.call_json_route(
                "/sponsors_helper/api/defer/",
                state,
                {
                    "package_id": PACKAGE_A,
                    "crypter": "filecrypt",
                    "reason_code": "ip_block_suspected",
                    "link_fingerprint": "a" * 64,
                },
                service,
            )

        self.assertEqual(404, context.exception.status_code)
        self.assertEqual("observing", service.snapshot("filecrypt")["state"])
        fail.assert_not_called()
        # The evidence survives the rejected package, so its observation stays
        # counted in the durable ledger while no statistic is touched here.
        self.assertEqual([], stats.mock_calls)
        self.assertEqual(
            {"observations": 1, "cooldowns": 0, "probes": 0},
            json.loads(state.databases["crypter_events"].rows["pending"]),
        )
        notify.assert_not_called()

    def test_defer_database_error_retains_package_without_failed_side_effects(self):
        package = protected_package(
            PACKAGE_A,
            "Retained.Database.Error",
            [["https://filecrypt.invalid/container/1", "filecrypt"]],
        )
        state = AtomicSharedState(dict([package]))
        original = state.databases["protected"].rows[PACKAGE_A]
        service = mock.Mock()
        service.get_package_defer.return_value = None
        service.crypter_decision.return_value = None
        service.observe.return_value = {
            "state": "observing",
            "evidence_count": 1,
            "package_retry_after_epoch": NOW + 900,
            "recorded": True,
            "cooldown_started": False,
        }
        service.defer_package.side_effect = RuntimeError("database unavailable")

        with (
            mock.patch("quasarr.api.sponsors_helper.fail") as fail,
            mock.patch("quasarr.api.sponsors_helper.StatsHelper") as stats,
            mock.patch(
                "quasarr.api.sponsors_helper.update_release_notification"
            ) as notify,
            self.assertRaises(HTTPError) as context,
        ):
            self.call_json_route(
                "/sponsors_helper/api/defer/",
                state,
                {
                    "package_id": PACKAGE_A,
                    "crypter": "filecrypt",
                    "reason_code": "ip_block_suspected",
                    "link_fingerprint": "a" * 64,
                },
                service,
            )

        self.assertEqual(500, context.exception.status_code)
        self.assertEqual(original, state.databases["protected"].rows[PACKAGE_A])
        fail.assert_not_called()
        self.assertEqual([], stats.mock_calls)
        notify.assert_not_called()

    def test_crypter_access_clear_returns_exact_response_and_clears_all_state(self):
        state, service = self.deferred_state()

        result = self.call_json_route(
            "/sponsors_helper/api/crypter-access/",
            state,
            {
                "package_id": PACKAGE_A,
                "crypter": "filecrypt",
                "access": "clear",
            },
            service,
        )

        self.assertEqual(
            {"success": True, "state": "available", "cleared": True},
            result,
        )
        self.assertEqual("available", service.snapshot("filecrypt")["state"])
        self.assertIsNone(service.get_package_defer(PACKAGE_A))
        retained = json.loads(state.databases["protected"].rows[PACKAGE_A])
        self.assertEqual("Deferred.Package", retained["title"])
        self.assertNotIn("deferred", retained)

    def test_crypter_access_package_clear_failure_keeps_all_state_held(self):
        state, service = self.deferred_state()

        def fail_package_clear():
            raise RuntimeError("database unavailable")

        state.databases["protected"].before_mutation = fail_package_clear

        with self.assertRaises(HTTPError) as context:
            self.call_json_route(
                "/sponsors_helper/api/crypter-access/",
                state,
                {
                    "package_id": PACKAGE_A,
                    "crypter": "filecrypt",
                    "access": "clear",
                },
                service,
            )

        self.assertEqual(500, context.exception.status_code)
        self.assertEqual("observing", service.snapshot("filecrypt")["state"])
        self.assertIsNotNone(service.get_package_defer(PACKAGE_A))

    def test_crypter_access_rejects_unknown_and_unsupported_access_without_clearing(
        self,
    ):
        for access in (None, "unknown", "blocked", "CLEAR"):
            state, service = self.deferred_state()

            with (
                self.subTest(access=access),
                self.assertRaises(HTTPError) as context,
            ):
                self.call_json_route(
                    "/sponsors_helper/api/crypter-access/",
                    state,
                    {
                        "package_id": PACKAGE_A,
                        "crypter": "filecrypt",
                        "access": access,
                    },
                    service,
                )

            self.assertEqual(400, context.exception.status_code)
            self.assertEqual("observing", service.snapshot("filecrypt")["state"])
            self.assertIsNotNone(service.get_package_defer(PACKAGE_A))

    def test_crypter_access_rejects_unprotected_key_and_mismatched_package_link(self):
        cases = (
            (
                "rapidgator",
                [["https://filecrypt.invalid/container/1", "filecrypt"]],
            ),
            (
                "filecrypt",
                [["https://tolink.invalid/container/1", "tolink"]],
            ),
        )

        for crypter, links in cases:
            state, service = self.deferred_state(links=links)

            with (
                self.subTest(crypter=crypter, links=links),
                self.assertRaises(HTTPError) as context,
            ):
                self.call_json_route(
                    "/sponsors_helper/api/crypter-access/",
                    state,
                    {
                        "package_id": PACKAGE_A,
                        "crypter": crypter,
                        "access": "clear",
                    },
                    service,
                )

            self.assertEqual(400, context.exception.status_code)
            self.assertEqual("observing", service.snapshot("filecrypt")["state"])
            self.assertIsNotNone(service.get_package_defer(PACKAGE_A))

    def test_crypter_access_clear_survives_later_download_failure(self):
        state, service = self.deferred_state()
        self.call_json_route(
            "/sponsors_helper/api/crypter-access/",
            state,
            {
                "package_id": PACKAGE_A,
                "crypter": "filecrypt",
                "access": "clear",
            },
            service,
        )
        state.values["helper_active"] = True
        app = Bottle()
        setup_sponsors_helper_routes(app)
        route = next(
            route
            for route in app.routes
            if route.rule == "/sponsors_helper/api/download/"
        )

        with (
            mock.patch("quasarr.api.sponsors_helper.shared_state", state),
            mock.patch(
                "quasarr.api.sponsors_helper.request",
                mock.Mock(
                    json={
                        "name": "Deferred.Package",
                        "package_id": PACKAGE_A,
                        "urls": ["https://host.invalid/file"],
                        "password": "",
                        "notification": {"solvers": []},
                    }
                ),
            ),
            mock.patch(
                "quasarr.api.sponsors_helper.submit_final_download_urls",
                return_value={"success": False},
            ),
            mock.patch("quasarr.api.sponsors_helper.StatsHelper"),
            self.assertRaises(HTTPError) as context,
        ):
            route.callback()

        self.assertEqual(500, context.exception.status_code)
        self.assertEqual("available", service.snapshot("filecrypt")["state"])
        self.assertIsNone(service.get_package_defer(PACKAGE_A))

    def test_download_crypter_field_cannot_clear_but_crypter_access_does(self):
        state, service = self.deferred_state()
        state.values["helper_active"] = True
        app = Bottle()
        setup_sponsors_helper_routes(app)
        route = next(
            route
            for route in app.routes
            if route.rule == "/sponsors_helper/api/download/"
        )
        statistics = mock.Mock()
        payload = {
            "name": "Successful.Package",
            "package_id": PACKAGE_B,
            "urls": ["https://host.invalid/file"],
            "password": "",
            "notification": {"solvers": []},
            "crypter": "filecrypt",
        }

        with (
            mock.patch("quasarr.api.sponsors_helper.shared_state", state),
            mock.patch(
                "quasarr.api.sponsors_helper.request",
                mock.Mock(json=payload),
            ),
            mock.patch(
                "quasarr.api.sponsors_helper.submit_final_download_urls",
                return_value={
                    "success": True,
                    "links": ["https://host.invalid/file"],
                },
            ),
            mock.patch(
                "quasarr.api.sponsors_helper.StatsHelper",
                return_value=statistics,
            ),
        ):
            result = route.callback()
            payload.pop("crypter")
            legacy_result = route.callback()

        self.assertEqual(
            "Downloaded 1 download links for Successful.Package",
            result,
        )
        self.assertEqual(result, legacy_result)
        self.assertEqual("observing", service.snapshot("filecrypt")["state"])
        self.assertIsNotNone(service.get_package_defer(PACKAGE_A))

        access_result = self.call_json_route(
            "/sponsors_helper/api/crypter-access/",
            state,
            {
                "package_id": PACKAGE_A,
                "crypter": "filecrypt",
                "access": "clear",
            },
            service,
        )

        self.assertEqual(
            {"success": True, "state": "available", "cleared": True},
            access_result,
        )
        self.assertEqual("available", service.snapshot("filecrypt")["state"])
        self.assertIsNone(service.get_package_defer(PACKAGE_A))

    def test_to_decrypt_route_preserves_invalid_payload_status(self):
        app = Bottle()
        setup_sponsors_helper_routes(app)
        route = next(
            route
            for route in app.routes
            if route.rule == "/sponsors_helper/api/to_decrypt/"
        )
        protected_db = mock.Mock()
        protected_db.retrieve_all_titles.return_value = [("pkg-1", "{}")]

        with (
            mock.patch("quasarr.api.sponsors_helper.shared_state.update"),
            mock.patch(
                "quasarr.api.sponsors_helper.shared_state.get_db",
                return_value=protected_db,
            ),
            mock.patch(
                "quasarr.api.sponsors_helper.request",
                mock.Mock(json={}),
            ),
            self.assertRaises(HTTPError) as context,
        ):
            route.callback()

        self.assertEqual(400, context.exception.status_code)

    def test_normalize_helper_supported_urls_deduplicates_and_lowercases(self):
        self.assertEqual(
            ["container.", "alpha.", "beta."],
            normalize_helper_supported_urls(
                [" Container. ", "ALPHA.", "", None, "beta.", "container."]
            ),
        )

    def test_select_helper_package_moves_supported_url_to_front(self):
        protected_packages = [
            (
                "pkg-1",
                json.dumps(
                    {
                        "title": "Example.Release",
                        "links": [
                            ["https://unsupported.invalid/path", "other"],
                            ["https://container.invalid/Container/abc", "container"],
                        ],
                        "password": "",
                    }
                ),
            )
        ]

        package_id, data, prioritized_links = select_helper_package(
            protected_packages,
            ["container."],
        )

        self.assertEqual("pkg-1", package_id)
        self.assertEqual("Example.Release", data["title"])
        self.assertEqual(
            "https://container.invalid/Container/abc",
            prioritized_links[0][0],
        )

    def test_select_helper_package_skips_unsupported_packages_until_match(self):
        protected_packages = [
            (
                "pkg-1",
                json.dumps(
                    {
                        "title": "Unsupported.First",
                        "links": [["https://unknown.invalid/path", "other"]],
                        "password": "",
                    }
                ),
            ),
            (
                "pkg-2",
                json.dumps(
                    {
                        "title": "Supported.Second",
                        "links": [["https://alpha.invalid/f/abc", "alpha"]],
                        "password": "",
                    }
                ),
            ),
        ]

        package_id, data, prioritized_links = select_helper_package(
            protected_packages,
            ["container.", "alpha."],
        )

        self.assertEqual("pkg-2", package_id)
        self.assertEqual("Supported.Second", data["title"])
        self.assertEqual("https://alpha.invalid/f/abc", prioritized_links[0][0])

    def test_select_helper_package_accepts_advertised_mirror(self):
        protected_packages = [
            (
                "pkg-1",
                json.dumps(
                    {
                        "title": "Example.Release",
                        "links": [["https://source.invalid/release", "he"]],
                        "password": "",
                    }
                ),
            )
        ]

        package_id, _, links = select_helper_package(
            protected_packages, ["container."], ["he"]
        )

        self.assertEqual("pkg-1", package_id)
        self.assertEqual("he", links[0][1])

    def test_select_helper_package_returns_none_when_nothing_matches(self):
        protected_packages = [
            (
                "pkg-1",
                json.dumps(
                    {
                        "title": "Unsupported.Only",
                        "links": [["https://unknown.invalid/path", "other"]],
                        "password": "",
                    }
                ),
            )
        ]

        self.assertIsNone(select_helper_package(protected_packages, ["container."]))

    def test_select_helper_package_orders_links_by_mirror_whitelist(self):
        protected_packages = [
            (
                "Quasarr_movies_hash",
                json.dumps(
                    {
                        "title": "Example.Release",
                        "links": [
                            ["https://a.invalid/1", "ddownload"],
                            ["https://b.invalid/2", "rapidgator"],
                            ["https://c.invalid/3", "turbobit"],
                        ],
                        "password": "",
                    }
                ),
            )
        ]

        with (
            mock.patch(
                "quasarr.api.sponsors_helper.get_download_category_from_package_id",
                return_value="movies",
            ),
            mock.patch(
                "quasarr.api.sponsors_helper.get_download_category_mirrors",
                return_value=["turbobit", "rapidgator"],
            ),
        ):
            _, _, prioritized_links = select_helper_package(protected_packages, [])

        # Whitelist order is the ranking; unlisted mirrors keep their order last.
        self.assertEqual(
            ["turbobit", "rapidgator", "ddownload"],
            [link[1] for link in prioritized_links],
        )

    def test_select_helper_package_falls_back_to_rapidgator_first(self):
        protected_packages = [
            (
                "Quasarr_movies_hash",
                json.dumps(
                    {
                        "title": "Example.Release",
                        "links": [
                            ["https://a.invalid/1", "ddownload"],
                            ["https://b.invalid/2", "rapidgator"],
                        ],
                        "password": "",
                    }
                ),
            )
        ]

        with (
            mock.patch(
                "quasarr.api.sponsors_helper.get_download_category_from_package_id",
                return_value="movies",
            ),
            mock.patch(
                "quasarr.api.sponsors_helper.get_download_category_mirrors",
                return_value=[],
            ),
        ):
            _, _, prioritized_links = select_helper_package(protected_packages, [])

        # No whitelist configured: legacy rapidgator-first default is preserved.
        self.assertEqual(
            ["rapidgator", "ddownload"],
            [link[1] for link in prioritized_links],
        )

    def test_select_helper_package_skips_cooled_link_with_other_crypter_available(self):
        service = FakeCooldownService(cooling_crypters={"filecrypt"})
        protected_packages = [
            protected_package(
                PACKAGE_A,
                "Alternate.Crypter",
                [
                    ["https://filecrypt.invalid/container/1", "filecrypt"],
                    ["https://tolink.invalid/container/2", "tolink"],
                ],
            )
        ]

        package_id, _, links = select_helper_package(
            protected_packages,
            ["filecrypt.", "tolink."],
            cooldown_service=service,
        )

        self.assertEqual(PACKAGE_A, package_id)
        self.assertEqual(
            [["https://tolink.invalid/container/2", "tolink"]],
            links,
        )

    def test_select_helper_package_uses_later_package_when_only_link_is_cooled(self):
        service = FakeCooldownService(cooling_crypters={"filecrypt"})
        protected_packages = [
            protected_package(
                PACKAGE_A,
                "Cooled.First",
                [["https://filecrypt.invalid/container/1", "filecrypt"]],
            ),
            protected_package(
                PACKAGE_B,
                "Unrelated.Second",
                [["https://tolink.invalid/container/2", "tolink"]],
            ),
        ]

        package_id, data, _ = select_helper_package(
            protected_packages,
            ["filecrypt.", "tolink."],
            cooldown_service=service,
        )

        self.assertEqual(PACKAGE_B, package_id)
        self.assertEqual("Unrelated.Second", data["title"])

    def test_unresolved_he_like_link_remains_eligible(self):
        protected_packages = [
            protected_package(
                PACKAGE_A,
                "Unresolved.HE.Link",
                [["https://source.invalid/release", "he"]],
            )
        ]

        package_id, _, links = select_helper_package(
            protected_packages,
            ["container."],
            supported_mirrors=["he"],
            cooldown_service=FakeCooldownService(cooling_crypters={"filecrypt"}),
        )

        self.assertEqual(PACKAGE_A, package_id)
        self.assertEqual(
            [["https://source.invalid/release", "he"]],
            links,
        )

    def test_provisional_hold_skips_only_matching_package_and_crypter(self):
        service = FakeCooldownService(package_defers={PACKAGE_A: package_defer()})
        protected_packages = [
            protected_package(
                PACKAGE_A,
                "Held.First",
                [
                    ["https://filecrypt.invalid/container/1", "filecrypt"],
                    ["https://tolink.invalid/container/1", "tolink"],
                ],
            ),
            protected_package(
                PACKAGE_B,
                "Same.Crypter.Second",
                [["https://filecrypt.invalid/container/2", "filecrypt"]],
            ),
        ]

        package_id, _, links = select_helper_package(
            protected_packages,
            ["filecrypt.", "tolink."],
            cooldown_service=service,
        )
        later_package_id, _, _ = select_helper_package(
            protected_packages,
            ["filecrypt."],
            cooldown_service=service,
        )

        self.assertEqual(PACKAGE_A, package_id)
        self.assertEqual(
            [["https://tolink.invalid/container/1", "tolink"]],
            links,
        )
        self.assertEqual(PACKAGE_B, later_package_id)

    def test_probe_allows_one_handoff_and_is_consumed_before_return(self):
        service = FakeCooldownService(
            cooling_crypters={"filecrypt"},
            package_defers={PACKAGE_A: package_defer(probe_requested=True)},
        )
        protected_packages = [
            protected_package(
                PACKAGE_A,
                "Probe.First",
                [["https://filecrypt.invalid/container/1", "filecrypt"]],
            ),
            protected_package(
                PACKAGE_B,
                "Fallback.Second",
                [["https://tolink.invalid/container/2", "tolink"]],
            ),
        ]

        first_package, _, _ = select_helper_package(
            protected_packages,
            ["filecrypt.", "tolink."],
            cooldown_service=service,
        )
        second_package, _, _ = select_helper_package(
            protected_packages,
            ["filecrypt.", "tolink."],
            cooldown_service=service,
        )

        self.assertEqual(PACKAGE_A, first_package)
        self.assertEqual(PACKAGE_B, second_package)
        self.assertEqual([(PACKAGE_A, "filecrypt")], service.probe_consumptions)

    def test_queued_probe_is_not_consumed_after_hold_clears(self):
        service = FakeCooldownService(
            package_defers={
                PACKAGE_A: package_defer(active=False, probe_requested=True)
            }
        )
        protected_packages = [
            protected_package(
                PACKAGE_A,
                "Recovered.Hold",
                [["https://filecrypt.invalid/container/1", "filecrypt"]],
            )
        ]

        package_id, _, links = select_helper_package(
            protected_packages,
            ["filecrypt."],
            cooldown_service=service,
        )

        self.assertEqual(PACKAGE_A, package_id)
        self.assertEqual(
            [["https://filecrypt.invalid/container/1", "filecrypt"]],
            links,
        )
        self.assertEqual([], service.probe_consumptions)

    def test_failed_probe_consumption_drops_only_probe_dependent_links(self):
        service = FakeCooldownService(
            cooling_crypters={"filecrypt"},
            package_defers={PACKAGE_A: package_defer(probe_requested=True)},
            failed_probe_consumptions={PACKAGE_A},
        )
        protected_packages = [
            protected_package(
                PACKAGE_A,
                "Lost.Probe",
                [
                    ["https://filecrypt.invalid/container/1", "filecrypt"],
                    ["https://tolink.invalid/container/1", "tolink"],
                ],
            ),
            protected_package(
                PACKAGE_B,
                "Fallback.Second",
                [["https://tolink.invalid/container/2", "tolink"]],
            ),
        ]

        package_id, _, links = select_helper_package(
            protected_packages,
            ["filecrypt.", "tolink."],
            cooldown_service=service,
        )

        self.assertEqual(PACKAGE_A, package_id)
        self.assertEqual(
            [["https://tolink.invalid/container/1", "tolink"]],
            links,
        )
        self.assertEqual([(PACKAGE_A, "filecrypt")], service.probe_consumptions)

    def test_real_cooldown_selection_survives_probe_race_and_malformed_rows(self):
        state = AtomicSharedState(
            {
                "0-invalid-package": json.dumps(
                    {
                        "title": "Invalid.Identifier",
                        "links": [
                            ["https://tolink.invalid/container/invalid", "tolink"]
                        ],
                        "password": "",
                    }
                ),
                PACKAGE_A: "{malformed-json",
                NONCONFORMING_PACKAGE: json.dumps(
                    {
                        "links": [
                            ["https://tolink.invalid/container/no-title", "tolink"]
                        ],
                        "password": "",
                    }
                ),
                PACKAGE_B: json.dumps(
                    {
                        "title": "Probe.Race.Alternate",
                        "links": [
                            ["https://filecrypt.invalid/container/1", "filecrypt"],
                            ["https://tolink.invalid/container/1", "tolink"],
                        ],
                        "password": "",
                    }
                ),
            }
        )
        service = CrypterCooldownService(state)
        for package_id, fingerprint_character in (
            (PACKAGE_B, "a"),
            (PACKAGE_C, "b"),
            (PACKAGE_D, "c"),
        ):
            decision = service.observe(
                "filecrypt",
                package_id,
                fingerprint_character * 64,
                "ip_block_suspected",
            )
        service.defer_package(
            PACKAGE_B,
            "filecrypt",
            "ip_block_suspected",
            decision["package_retry_after_epoch"],
            observation_holds=0,
        )
        service.request_probe([PACKAGE_B])
        protected_database = state.databases["protected"]
        race_observed = []

        def consume_probe_elsewhere():
            package = json.loads(protected_database.rows[PACKAGE_B])
            package["deferred"]["probe_requested"] = False
            protected_database.rows[PACKAGE_B] = json.dumps(package)
            race_observed.append(PACKAGE_B)

        protected_database.before_mutation = consume_probe_elsewhere
        app = Bottle()
        setup_sponsors_helper_routes(app)
        route = next(
            route
            for route in app.routes
            if route.rule == "/sponsors_helper/api/to_decrypt/"
        )

        with (
            mock.patch("quasarr.api.sponsors_helper.shared_state", state),
            mock.patch(
                "quasarr.api.sponsors_helper.request",
                mock.Mock(
                    json={
                        "supported_urls": ["filecrypt.", "tolink."],
                        "capabilities": ["crypter_defer_v1"],
                    }
                ),
            ),
        ):
            result = route.callback()

        self.assertEqual([PACKAGE_B], race_observed)
        self.assertEqual("Probe.Race.Alternate", result["to_decrypt"]["name"])
        self.assertEqual(
            [["https://tolink.invalid/container/1", "tolink"]],
            result["to_decrypt"]["url"],
        )

    def test_custom_category_package_is_selectable_during_unrelated_cooldown(self):
        service = FakeCooldownService(cooling_crypters={"filecrypt"})
        protected_packages = [
            protected_package(
                CUSTOM_CATEGORY_PACKAGE,
                "Custom.Category.First",
                [["https://tolink.invalid/container/1", "tolink"]],
            ),
            protected_package(
                PACKAGE_B,
                "Fallback.Second",
                [["https://tolink.invalid/container/2", "tolink"]],
            ),
        ]

        package_id, data, links = select_helper_package(
            protected_packages,
            ["tolink."],
            cooldown_service=service,
        )

        self.assertEqual(CUSTOM_CATEGORY_PACKAGE, package_id)
        self.assertEqual("Custom.Category.First", data["title"])
        self.assertEqual([["https://tolink.invalid/container/1", "tolink"]], links)

    def test_exclusions_honor_custom_category_package_ids(self):
        protected_packages = [
            protected_package(
                CUSTOM_CATEGORY_PACKAGE,
                "Excluded.Custom.Category",
                [["https://tolink.invalid/container/1", "tolink"]],
            ),
            protected_package(
                PACKAGE_B,
                "Eligible.Second",
                [["https://tolink.invalid/container/2", "tolink"]],
            ),
        ]

        package_id, data, _ = select_helper_package(
            protected_packages,
            ["tolink."],
            cooldown_service=FakeCooldownService(),
            excluded_package_ids=[CUSTOM_CATEGORY_PACKAGE],
        )

        self.assertEqual(PACKAGE_B, package_id)
        self.assertEqual("Eligible.Second", data["title"])
        # Control: the same package is selected once the exclusion is dropped,
        # so PACKAGE_B above proves exclusion rather than ID rejection.
        self.assertEqual(
            CUSTOM_CATEGORY_PACKAGE,
            select_helper_package(
                protected_packages,
                ["tolink."],
                cooldown_service=FakeCooldownService(),
            )[0],
        )

    def test_selection_still_skips_ids_outside_the_package_id_contract(self):
        uppercase_category = "Quasarr_Movies_" + "0" * 32
        protected_packages = [
            protected_package(
                uppercase_category,
                "Uppercase.Category",
                [["https://tolink.invalid/container/1", "tolink"]],
            ),
            protected_package(
                "Quasarr_movies-4k_" + "0" * 32,
                "Punctuated.Category",
                [["https://tolink.invalid/container/2", "tolink"]],
            ),
            protected_package(
                PACKAGE_B,
                "Conforming.Third",
                [["https://tolink.invalid/container/3", "tolink"]],
            ),
        ]

        package_id, _, _ = select_helper_package(
            protected_packages,
            ["tolink."],
            cooldown_service=FakeCooldownService(),
            excluded_package_ids=[uppercase_category],
            enforce_package_contract=True,
        )

        self.assertEqual(PACKAGE_B, package_id)

    def test_package_contract_applies_without_a_cooldown_service(self):
        protected_packages = [
            protected_package(
                "Quasarr_Movies_" + "0" * 32,
                "Uppercase.Category",
                [["https://tolink.invalid/container/1", "tolink"]],
            ),
            protected_package(
                PACKAGE_B,
                "Conforming.Second",
                [["https://tolink.invalid/container/2", "tolink"]],
            ),
        ]

        # The contract follows the helper's capability, not cooldown filtering:
        # a capable helper can only exclude IDs the exclusion list accepts.
        package_id, data, _ = select_helper_package(
            protected_packages,
            ["tolink."],
            enforce_package_contract=True,
        )

        self.assertEqual(PACKAGE_B, package_id)
        self.assertEqual("Conforming.Second", data["title"])

    def test_exclusions_ignore_invalid_duplicates_and_cap_valid_ids_at_100(self):
        package_ids = [f"Quasarr_movies_{index:032x}" for index in range(101)]
        protected_packages = [
            protected_package(
                package_id,
                f"Package.{index}",
                [[f"https://tolink.invalid/container/{index}", "tolink"]],
            )
            for index, package_id in enumerate(package_ids)
        ]
        excluded_package_ids = [
            None,
            "not-a-package-id",
            package_ids[0],
            package_ids[0],
            *package_ids[1:100],
            42,
            package_ids[100],
        ]

        package_id, _, _ = select_helper_package(
            protected_packages,
            ["tolink."],
            cooldown_service=FakeCooldownService(),
            excluded_package_ids=excluded_package_ids,
        )

        self.assertEqual(package_ids[100], package_id)

    def test_disabled_packages_stay_excluded_with_cooldown_filtering(self):
        protected_packages = [
            protected_package(
                PACKAGE_A,
                "Disabled.First",
                [["https://tolink.invalid/container/1", "tolink"]],
                disabled=True,
            ),
            protected_package(
                PACKAGE_B,
                "Enabled.Second",
                [["https://tolink.invalid/container/2", "tolink"]],
            ),
        ]

        package_id, data, _ = select_helper_package(
            protected_packages,
            ["tolink."],
            cooldown_service=FakeCooldownService(),
        )

        self.assertEqual(PACKAGE_B, package_id)
        self.assertEqual("Enabled.Second", data["title"])

    def test_legacy_request_ignores_cooldown_and_exclusions(self):
        service = FakeCooldownService(cooling_crypters={"filecrypt"})
        protected_packages = [
            protected_package(
                PACKAGE_A,
                "Legacy.First",
                [["https://filecrypt.invalid/container/1", "filecrypt"]],
            ),
            protected_package(
                PACKAGE_B,
                "Alternative.Second",
                [["https://tolink.invalid/container/2", "tolink"]],
            ),
        ]

        result, cooldown_type = self.call_to_decrypt(
            protected_packages,
            {
                "supported_urls": ["filecrypt.", "tolink."],
                "excluded_package_ids": [PACKAGE_A],
            },
            cooldown_service=service,
        )

        self.assertEqual("Legacy.First", result["to_decrypt"]["name"])
        self.assertEqual(
            [["https://filecrypt.invalid/container/1", "filecrypt"]],
            result["to_decrypt"]["url"],
        )
        cooldown_type.assert_not_called()

    def test_capable_request_filters_cooled_package_and_selects_alternative(self):
        service = FakeCooldownService(cooling_crypters={"filecrypt"})
        protected_packages = [
            protected_package(
                PACKAGE_A,
                "Cooled.First",
                [["https://filecrypt.invalid/container/1", "filecrypt"]],
            ),
            protected_package(
                PACKAGE_B,
                "Alternative.Second",
                [["https://tolink.invalid/container/2", "tolink"]],
            ),
        ]

        result, cooldown_type = self.call_to_decrypt(
            protected_packages,
            {
                "supported_urls": ["filecrypt.", "tolink."],
                "capabilities": ["crypter_defer_v1"],
            },
            cooldown_service=service,
        )

        self.assertEqual("Alternative.Second", result["to_decrypt"]["name"])
        cooldown_type.assert_called_once()

    def test_capable_request_applies_excluded_package_ids(self):
        protected_packages = [
            protected_package(
                PACKAGE_A,
                "Excluded.First",
                [["https://tolink.invalid/container/1", "tolink"]],
            ),
            protected_package(
                PACKAGE_B,
                "Eligible.Second",
                [["https://tolink.invalid/container/2", "tolink"]],
            ),
        ]

        result, _ = self.call_to_decrypt(
            protected_packages,
            {
                "supported_urls": ["tolink."],
                "capabilities": ["crypter_defer_v1"],
                "excluded_package_ids": [PACKAGE_A],
            },
        )

        self.assertEqual("Eligible.Second", result["to_decrypt"]["name"])

    def test_capable_request_returns_custom_category_package_during_cooldown(self):
        service = FakeCooldownService(cooling_crypters={"filecrypt"})
        protected_packages = [
            protected_package(
                PACKAGE_A,
                "Cooled.First",
                [["https://filecrypt.invalid/container/1", "filecrypt"]],
            ),
            protected_package(
                CUSTOM_CATEGORY_PACKAGE,
                "Custom.Category.Second",
                [["https://tolink.invalid/container/2", "tolink"]],
            ),
        ]

        result, cooldown_type = self.call_to_decrypt(
            protected_packages,
            {
                "supported_urls": ["filecrypt.", "tolink."],
                "capabilities": ["crypter_defer_v1"],
            },
            cooldown_service=service,
        )

        self.assertEqual("Custom.Category.Second", result["to_decrypt"]["name"])
        self.assertEqual(CUSTOM_CATEGORY_PACKAGE, result["to_decrypt"]["id"])
        cooldown_type.assert_called_once()

    def test_legacy_request_retains_exact_response_shape(self):
        protected_packages = [
            protected_package(
                PACKAGE_A,
                "Legacy.Shape",
                [["https://filecrypt.invalid/container/1", "filecrypt"]],
            )
        ]

        result, _ = self.call_to_decrypt(
            protected_packages,
            {"supported_urls": ["filecrypt."]},
        )

        self.assertEqual(
            {
                "to_decrypt": {
                    "name": "Legacy.Shape",
                    "id": PACKAGE_A,
                    "url": [["https://filecrypt.invalid/container/1", "filecrypt"]],
                    "mirror": "filecrypt",
                    "password": "",
                    "max_attempts": 3,
                }
            },
            result,
        )


class CoherentDeferProjectionTests(unittest.TestCase):
    """Capable selection must read the live decision, not a stored timestamp."""

    FILECRYPT_URLS = tuple(
        f"https://filecrypt.invalid/container/{index}" for index in range(1, 6)
    )

    def setUp(self):
        rows = {
            f"Quasarr_movies_{index:032x}": json.dumps(
                {
                    "title": f"Cohort.Member.{index}",
                    "password": "",
                    "links": [[self.FILECRYPT_URLS[index - 1], "filecrypt"]],
                }
            )
            for index in range(1, 6)
        }
        self.state = AtomicSharedState(rows)
        self.state.values["crypter_block_mode"] = "defer"
        self.service = CrypterCooldownService(self.state, clock=lambda: NOW)
        self.identities = iter(f"{index:032x}" for index in range(1, 500))
        patcher = mock.patch.object(
            CrypterCooldownService,
            "_new_identifier",
            lambda _self: next(self.identities),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.rows = [[key, value] for key, value in sorted(rows.items())]

    def hold_one_member(self):
        inventory = enumerate_filecrypt_candidates(self.rows)
        offer = self.service.prepare_offer("filecrypt", inventory)
        owner = next(
            candidate.occurrences[0].package_id
            for candidate in inventory.candidates
            if candidate.fingerprint == offer["link_fingerprint"]
        )
        self.service.record_cohort_blocked(
            "filecrypt",
            owner,
            offer["link_fingerprint"],
            offer["sweep_id"],
            offer["offer_id"],
            "ip_block_suspected",
            inventory,
        )
        return owner, offer["link_fingerprint"]

    def test_a_generation_hold_suppresses_its_link_at_the_helper_boundary(self):
        owner, held = self.hold_one_member()
        others = [row[0] for row in self.rows if row[0] != owner]

        selected = select_helper_package(
            self.rows,
            ["filecrypt."],
            cooldown_service=self.service,
            excluded_package_ids=others,
            enforce_package_contract=True,
        )

        self.assertIsNone(selected)
        self.assertNotEqual([], self.rows)
        self.assertEqual(
            [held],
            [
                link_fingerprint("filecrypt", link[0])
                for link in json.loads(self.state.databases["protected"].rows[owner])[
                    "links"
                ]
            ],
        )

    def test_the_held_package_is_skipped_only_for_the_tested_fingerprint(self):
        owner, _held = self.hold_one_member()
        alternative = ["https://tolink.invalid/alternative", "tolink"]
        package = json.loads(self.state.databases["protected"].rows[owner])
        package["links"].append(alternative)
        self.state.databases["protected"].rows[owner] = json.dumps(package)
        rows = [
            [key, value]
            for key, value in sorted(self.state.databases["protected"].rows.items())
        ]
        others = [row[0] for row in rows if row[0] != owner]

        selected = select_helper_package(
            rows,
            ["filecrypt.", "tolink."],
            cooldown_service=self.service,
            excluded_package_ids=others,
            enforce_package_contract=True,
        )

        self.assertIsNotNone(selected)
        package_id, _data, links = selected
        self.assertEqual(owner, package_id)
        self.assertEqual([alternative], links)


if __name__ == "__main__":
    unittest.main()
