# -*- coding: utf-8 -*-

import copy
import json
import threading
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import mock

from bottle import Bottle

from quasarr.api import get_api
from quasarr.api.config import setup_config
from quasarr.api.sponsors_helper import setup_sponsors_helper_routes
from quasarr.api.sponsors_helper.cohort_protocol import (
    CRYPTER_DEFER_CAPABILITY,
    FILECRYPT_COHORT_CAPABILITY,
)
from quasarr.constants import CRYPTER_BLOCK_SETTINGS_TABLE
from quasarr.downloads.packages import get_packages
from quasarr.providers import shared_state as provider_shared_state
from quasarr.providers.auth import audit_route_auth_modes
from quasarr.providers.crypter_cooldowns import CrypterCooldownService
from quasarr.providers.filecrypt_lifecycle import (
    FILECRYPT_SWEEP_KEY,
    FILECRYPT_SWEEP_MEMBERS_TABLE,
    FILECRYPT_SWEEP_STATE_TABLE,
    decode_sweep_header,
)
from quasarr.providers.filecrypt_lifecycle_service import FilecryptLifecycleService
from quasarr.storage.setup import (
    get_crypter_block_settings_data,
    initialize_crypter_block_settings,
    save_crypter_block_settings,
)

PACKAGE_ID = "Quasarr_movies_" + "a" * 32
ALTERNATIVE_PACKAGE_ID = "Quasarr_movies_" + "b" * 32
EVIDENCE_PACKAGE_IDS = (
    "Quasarr_movies_" + "c" * 32,
    "Quasarr_movies_" + "d" * 32,
)
# Sorts ahead of every canonical ID below, so selection reaches it first.
MALFORMED_PACKAGE_ID = "Quasarr_Movies_" + "e" * 32
CUSTOM_CATEGORY_PACKAGE_ID = "Quasarr_movies4k_" + "f" * 32
REASON = "ip_block_suspected"
NOW = 1_700_000_000
COOLED_LINK = ["https://filecrypt.invalid/container/1", "filecrypt"]
CLEAR_LINK = ["https://tolink.invalid/container/2", "tolink"]
HELPER_URLS = ["filecrypt.", "tolink."]
DEFER_PAYLOAD = {
    "package_id": PACKAGE_ID,
    "crypter": "filecrypt",
    "reason_code": REASON,
    "link_fingerprint": "b" * 64,
}
_UNSET = object()


def protected_blob(title, links):
    return json.dumps({"title": title, "links": links, "password": "", "size_mb": 1024})


class MemoryTable:
    def __init__(self, tables=None):
        self.rows = {}
        self.tables = {} if tables is None else tables
        self.retrieve_count = 0

    def _peer(self, table):
        if table not in self.tables:
            self.tables[table] = MemoryTable(self.tables)
        return self.tables[table]

    def retrieve(self, key):
        self.retrieve_count += 1
        return self.rows.get(key)

    def retrieve_all_titles(self):
        items = [[key, value] for key, value in sorted(self.rows.items())]
        return items if items else None

    def update_store(self, key, value):
        self.rows[key] = value
        return True

    def mutate_value(self, key, mutator):
        self.retrieve_count += 1
        value = mutator(self.rows.get(key))
        if value is None:
            self.rows.pop(key, None)
        else:
            self.rows[key] = value
        return value

    def mutate_values(self, targets, mutator):
        """One transaction over several tables, like the sqlite primitive."""
        self.retrieve_count += 1
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


class BlockModeSharedState:
    def __init__(self, mode=_UNSET):
        self.values = {"crypter_cooldown_hours": 24}
        if mode is not _UNSET:
            self.values["crypter_block_mode"] = mode
        self.databases = {}
        for table in ("protected", "failed", "crypter_cooldowns"):
            self.databases[table] = MemoryTable(self.databases)

    def get_db(self, table):
        if table not in self.databases:
            self.databases[table] = MemoryTable(self.databases)
        return self.databases[table]

    def get_device(self):
        return mock.Mock()

    def update(self, key, value):
        self.values[key] = value


class FakeCache:
    linkgrabber_packages = []
    linkgrabber_links = []
    downloader_packages = []
    downloader_links = []
    is_collecting = False

    @staticmethod
    def get_stats():
        return {}


class MemorySettingsDatabase:
    def __init__(self, rows=None, events=None):
        self.rows = dict(rows or {})
        self.events = events if events is not None else []

    def retrieve(self, key):
        self.events.append(("retrieve", key))
        return self.rows.get(key)

    def update_store(self, key, value):
        self.events.append(("update_store", key, value))
        self.rows[key] = value

    def delete(self, key):
        self.events.append(("delete", key))
        self.rows.pop(key, None)


class FakeSharedState:
    def __init__(self, values=None, events=None):
        self.values = dict(values or {})
        self.events = events if events is not None else []

    def update(self, key, value):
        self.events.append(("update", key, value))
        self.values[key] = value


class CrypterBlockSettingTests(unittest.TestCase):
    def setUp(self):
        self.database = MemorySettingsDatabase()
        self.database_type = mock.patch(
            "quasarr.storage.setup.crypter_blocks.DataBase",
            return_value=self.database,
        )
        self.database_factory = self.database_type.start()

    def tearDown(self):
        self.database_type.stop()

    def test_empty_table_initializes_defer_mode_and_24_hour_cooldown(self):
        shared_state = FakeSharedState()

        with mock.patch.dict("os.environ", {"FILECRYPT_SWEEP_WINDOW_MINUTES": ""}):
            settings = initialize_crypter_block_settings(shared_state)

        self.assertEqual(
            {
                "mode": "defer",
                "cooldown_hours": 24,
                "filecrypt_sweep_window_minutes": 15,
                "filecrypt_sweep_window_override": None,
                "filecrypt_sweep_window_source": "default",
            },
            settings,
        )
        self.assertEqual("defer", shared_state.values["crypter_block_mode"])
        self.assertEqual(24, shared_state.values["crypter_cooldown_hours"])
        self.database_factory.assert_called_with(CRYPTER_BLOCK_SETTINGS_TABLE)

    def test_get_refreshes_persisted_settings_before_updating_cache(self):
        events = []
        self.database.events = events
        self.database.rows.update({"mode": "fail", "cooldown_hours": "72"})
        shared_state = FakeSharedState(
            {
                "crypter_block_mode": "defer",
                "crypter_cooldown_hours": 24,
            },
            events=events,
        )

        with mock.patch.dict("os.environ", {"FILECRYPT_SWEEP_WINDOW_MINUTES": ""}):
            result = get_crypter_block_settings_data(shared_state)

        self.assertEqual(
            {
                "success": True,
                "settings": {
                    "mode": "fail",
                    "cooldown_hours": 72,
                    "filecrypt_sweep_window_minutes": 15,
                    "filecrypt_sweep_window_override": None,
                    "filecrypt_sweep_window_source": "default",
                },
            },
            result,
        )
        self.assertEqual(
            [
                ("retrieve", "mode"),
                ("retrieve", "cooldown_hours"),
                ("retrieve", "filecrypt_sweep_window_minutes"),
                ("update", "crypter_block_mode", "fail"),
                ("update", "crypter_cooldown_hours", 72),
                ("update", "filecrypt_sweep_window_minutes", 15),
                ("update", "filecrypt_sweep_window_override", None),
                ("update", "filecrypt_sweep_window_source", "default"),
            ],
            events,
        )

    def test_save_persists_valid_settings_for_a_fresh_shared_state(self):
        shared_state = FakeSharedState()

        with (
            mock.patch(
                "quasarr.storage.setup.crypter_blocks.request",
                mock.Mock(json={"mode": "fail", "cooldown_hours": 48}),
            ),
            mock.patch.dict("os.environ", {"FILECRYPT_SWEEP_WINDOW_MINUTES": ""}),
        ):
            result = save_crypter_block_settings(shared_state)

        self.assertTrue(result["success"])
        self.assertEqual(
            {
                "mode": "fail",
                "cooldown_hours": 48,
                "filecrypt_sweep_window_minutes": 15,
                "filecrypt_sweep_window_override": None,
                "filecrypt_sweep_window_source": "default",
            },
            result["settings"],
        )
        self.assertEqual({"mode": "fail", "cooldown_hours": "48"}, self.database.rows)

        restored_state = FakeSharedState()
        self.assertEqual(
            {
                "mode": "fail",
                "cooldown_hours": 48,
                "filecrypt_sweep_window_minutes": 15,
                "filecrypt_sweep_window_override": None,
                "filecrypt_sweep_window_source": "default",
            },
            initialize_crypter_block_settings(restored_state),
        )

    def test_invalid_mode_preserves_current_mode_and_saves_valid_hours(self):
        self.database.rows.update({"mode": "fail", "cooldown_hours": "48"})
        shared_state = FakeSharedState()

        with (
            mock.patch(
                "quasarr.storage.setup.crypter_blocks.request",
                mock.Mock(json={"mode": "pause", "cooldown_hours": 72}),
            ),
            mock.patch.dict("os.environ", {"FILECRYPT_SWEEP_WINDOW_MINUTES": ""}),
        ):
            result = save_crypter_block_settings(shared_state)

        self.assertEqual(
            {
                "mode": "fail",
                "cooldown_hours": 72,
                "filecrypt_sweep_window_minutes": 15,
                "filecrypt_sweep_window_override": None,
                "filecrypt_sweep_window_source": "default",
            },
            result["settings"],
        )
        self.assertEqual({"mode": "fail", "cooldown_hours": "72"}, self.database.rows)

    def test_non_integer_hours_preserve_current_hours_and_save_valid_mode(self):
        self.database.rows.update({"mode": "defer", "cooldown_hours": "48"})
        shared_state = FakeSharedState()

        with (
            mock.patch(
                "quasarr.storage.setup.crypter_blocks.request",
                mock.Mock(json={"mode": "fail", "cooldown_hours": "72"}),
            ),
            mock.patch.dict("os.environ", {"FILECRYPT_SWEEP_WINDOW_MINUTES": ""}),
        ):
            result = save_crypter_block_settings(shared_state)

        self.assertEqual(
            {
                "mode": "fail",
                "cooldown_hours": 48,
                "filecrypt_sweep_window_minutes": 15,
                "filecrypt_sweep_window_override": None,
                "filecrypt_sweep_window_source": "default",
            },
            result["settings"],
        )
        self.assertEqual({"mode": "fail", "cooldown_hours": "48"}, self.database.rows)

    def test_hours_below_24_preserve_current_hours(self):
        self.database.rows.update({"mode": "defer", "cooldown_hours": "96"})
        shared_state = FakeSharedState()

        with (
            mock.patch(
                "quasarr.storage.setup.crypter_blocks.request",
                mock.Mock(json={"mode": "defer", "cooldown_hours": 23}),
            ),
            mock.patch.dict("os.environ", {"FILECRYPT_SWEEP_WINDOW_MINUTES": ""}),
        ):
            result = save_crypter_block_settings(shared_state)

        self.assertEqual(
            {
                "mode": "defer",
                "cooldown_hours": 96,
                "filecrypt_sweep_window_minutes": 15,
                "filecrypt_sweep_window_override": None,
                "filecrypt_sweep_window_source": "default",
            },
            result["settings"],
        )
        self.assertEqual({"mode": "defer", "cooldown_hours": "96"}, self.database.rows)

    def test_malformed_json_preserves_database_and_cached_settings(self):
        for payload in (None, [], "invalid"):
            with self.subTest(payload=payload):
                self.database.rows = {"mode": "fail", "cooldown_hours": "48"}
                self.database.events.clear()
                shared_state = FakeSharedState(
                    {
                        "crypter_block_mode": "fail",
                        "crypter_cooldown_hours": 48,
                    }
                )
                before_rows = copy.deepcopy(self.database.rows)
                before_values = copy.deepcopy(shared_state.values)

                with mock.patch(
                    "quasarr.storage.setup.crypter_blocks.request",
                    mock.Mock(json=payload),
                ):
                    result = save_crypter_block_settings(shared_state)

                self.assertEqual(
                    {"success": False, "message": "Invalid JSON payload"},
                    result,
                )
                self.assertEqual(before_rows, self.database.rows)
                self.assertEqual(before_values, shared_state.values)
                self.assertEqual([], self.database.events)

    def test_sweep_window_precedence_matrix(self):
        cases = [
            dict(
                label="default",
                stored={},
                env="",
                effective=15,
                override=None,
                source="default",
            ),
            dict(
                label="valid_env",
                stored={},
                env="30",
                effective=30,
                override=None,
                source="environment",
            ),
            dict(
                label="stored_over_env",
                stored={"filecrypt_sweep_window_minutes": "60"},
                env="30",
                effective=60,
                override=60,
                source="stored",
            ),
            dict(
                label="invalid_env",
                stored={},
                env="abc",
                effective=15,
                override=None,
                source="default",
            ),
            dict(
                label="invalid_stored",
                stored={"filecrypt_sweep_window_minutes": "xyz"},
                env="",
                effective=15,
                override=None,
                source="default",
            ),
        ]
        for case in cases:
            with self.subTest(label=case["label"]):
                self.database.rows = {
                    "mode": "defer",
                    "cooldown_hours": "24",
                    **case["stored"],
                }
                shared_state = FakeSharedState()
                with (
                    mock.patch.dict(
                        "os.environ",
                        {"FILECRYPT_SWEEP_WINDOW_MINUTES": case["env"]},
                    ),
                    mock.patch("quasarr.storage.setup.crypter_blocks._log") as mock_log,
                ):
                    settings = initialize_crypter_block_settings(shared_state)
                self.assertEqual(
                    case["effective"], settings["filecrypt_sweep_window_minutes"]
                )
                self.assertEqual(
                    case["override"], settings["filecrypt_sweep_window_override"]
                )
                self.assertEqual(
                    case["source"], settings["filecrypt_sweep_window_source"]
                )
                self.assertEqual(
                    case["effective"],
                    shared_state.values["filecrypt_sweep_window_minutes"],
                )
                if case["label"] == "invalid_env":
                    mock_log.warning.assert_called_once()
                    self.assertNotIn("abc", mock_log.warning.call_args[0][0])
                else:
                    mock_log.warning.assert_not_called()

    def test_save_clear_and_invalid_sweep_window_override(self):
        cases = [
            dict(
                label="valid_int",
                initial={"mode": "defer", "cooldown_hours": "24"},
                payload={
                    "mode": "defer",
                    "cooldown_hours": 24,
                    "filecrypt_sweep_window_minutes": 30,
                },
                env="",
                expected_row="30",
                expected_effective=30,
                expected_override=30,
                expected_source="stored",
            ),
            dict(
                label="null_clear_to_env",
                initial={
                    "mode": "defer",
                    "cooldown_hours": "24",
                    "filecrypt_sweep_window_minutes": "60",
                },
                payload={
                    "mode": "defer",
                    "cooldown_hours": 24,
                    "filecrypt_sweep_window_minutes": None,
                },
                env="45",
                expected_row=None,
                expected_effective=45,
                expected_override=None,
                expected_source="environment",
            ),
            dict(
                label="null_clear_to_default",
                initial={
                    "mode": "defer",
                    "cooldown_hours": "24",
                    "filecrypt_sweep_window_minutes": "60",
                },
                payload={
                    "mode": "defer",
                    "cooldown_hours": 24,
                    "filecrypt_sweep_window_minutes": None,
                },
                env="",
                expected_row=None,
                expected_effective=15,
                expected_override=None,
                expected_source="default",
            ),
            dict(
                label="invalid_preserve",
                initial={
                    "mode": "defer",
                    "cooldown_hours": "24",
                    "filecrypt_sweep_window_minutes": "60",
                },
                payload={
                    "mode": "defer",
                    "cooldown_hours": 24,
                    "filecrypt_sweep_window_minutes": "30",
                },
                env="",
                expected_row="60",
                expected_effective=60,
                expected_override=60,
                expected_source="stored",
            ),
        ]
        for case in cases:
            with self.subTest(label=case["label"]):
                events = []
                self.database.rows = dict(case["initial"])
                self.database.events = events
                shared_state = FakeSharedState(events=events)
                with (
                    mock.patch(
                        "quasarr.storage.setup.crypter_blocks.request",
                        mock.Mock(json=case["payload"]),
                    ),
                    mock.patch.dict(
                        "os.environ",
                        {"FILECRYPT_SWEEP_WINDOW_MINUTES": case["env"]},
                    ),
                ):
                    result = save_crypter_block_settings(shared_state)
                self.assertTrue(result["success"])
                if case["expected_row"] is None:
                    self.assertNotIn(
                        "filecrypt_sweep_window_minutes", self.database.rows
                    )
                else:
                    self.assertEqual(
                        case["expected_row"],
                        self.database.rows.get("filecrypt_sweep_window_minutes"),
                    )
                s = result["settings"]
                self.assertEqual(
                    case["expected_effective"], s["filecrypt_sweep_window_minutes"]
                )
                self.assertEqual(
                    case["expected_override"], s["filecrypt_sweep_window_override"]
                )
                self.assertEqual(
                    case["expected_source"], s["filecrypt_sweep_window_source"]
                )
                # Cache updates come after all DB operations
                update_idxs = [i for i, e in enumerate(events) if e[0] == "update"]
                db_idxs = [
                    i
                    for i, e in enumerate(events)
                    if e[0] in ("retrieve", "update_store", "delete")
                ]
                if update_idxs and db_idxs:
                    self.assertGreater(update_idxs[0], db_idxs[-1])

    def test_active_generation_keeps_frozen_sweep_deadline(self):
        state = BlockModeSharedState()
        state.values["filecrypt_sweep_window_minutes"] = 7
        ids_seq = [0]

        def seq_id():
            ids_seq[0] += 1
            return f"{ids_seq[0]:032x}"

        service = FilecryptLifecycleService(
            state, clock=lambda: NOW, identifier_factory=seq_id
        )
        rows = [
            [
                PACKAGE_ID,
                protected_blob(
                    "T1", [["https://filecrypt.invalid/link/1", "filecrypt"]]
                ),
            ],
            [
                ALTERNATIVE_PACKAGE_ID,
                protected_blob(
                    "T2", [["https://filecrypt.invalid/link/2", "filecrypt"]]
                ),
            ],
        ]

        offer = service.prepare_offer(rows)
        self.assertIsNotNone(offer)
        self.assertEqual("sweep", offer["mode"])

        header = decode_sweep_header(
            state.get_db(FILECRYPT_SWEEP_STATE_TABLE).retrieve(FILECRYPT_SWEEP_KEY)
        )
        self.assertEqual(NOW + 7 * 60, header["deadline_epoch"])
        self.assertEqual(7 * 60, header["window_seconds"])

        # Changing the cached setting does not reopen or extend the active generation
        state.values["filecrypt_sweep_window_minutes"] = 30
        service.prepare_offer(rows)

        header_after = decode_sweep_header(
            state.get_db(FILECRYPT_SWEEP_STATE_TABLE).retrieve(FILECRYPT_SWEEP_KEY)
        )
        self.assertEqual(header["deadline_epoch"], header_after["deadline_epoch"])
        self.assertEqual(header["window_seconds"], header_after["window_seconds"])
        members = (
            state.get_db(FILECRYPT_SWEEP_MEMBERS_TABLE).retrieve_all_titles() or []
        )
        self.assertEqual(header["total"], len(members))


class CrypterBlockRouteTests(unittest.TestCase):
    def test_settings_routes_are_get_post_only_and_api_key_authenticated(self):
        app = Bottle()
        setup_config(app, FakeSharedState())

        methods = {
            route.method
            for route in app.routes
            if route.rule == "/api/crypter-block/settings"
        }

        self.assertEqual({"GET", "POST"}, methods)
        audit_route_auth_modes(
            app,
            api_key_prefixes=("/api",),
            public_whitelist=(),
        )

    def test_fail_mode_returns_exact_legacy_response_without_state_access(self):
        app = Bottle()
        setup_sponsors_helper_routes(app)
        route = next(
            route for route in app.routes if route.rule == "/sponsors_helper/api/defer/"
        )
        shared_state = FakeSharedState({"crypter_block_mode": "fail"})
        shared_state.get_db = mock.Mock(
            side_effect=AssertionError("fail mode must not access cooldown state")
        )

        with (
            mock.patch("quasarr.api.sponsors_helper.shared_state", shared_state),
            mock.patch(
                "quasarr.api.sponsors_helper.request",
                mock.Mock(json=DEFER_PAYLOAD),
            ),
        ):
            result = route.callback()

        self.assertEqual(
            {
                "success": True,
                "instruction": "legacy_failure",
                "state": "available",
                "hold_type": "none",
                "evidence_count": 0,
                "retry_after_epoch": 0,
            },
            result,
        )
        shared_state.get_db.assert_not_called()


class CohortBlockModeTests(unittest.TestCase):
    """`fail` must stay a pure bypass for the cohort path as well: no offer,
    no decision row, and no cooldown read at all."""

    COHORT_CAPABILITIES = [CRYPTER_DEFER_CAPABILITY, FILECRYPT_COHORT_CAPABILITY]

    def setUp(self):
        self.clock = lambda: NOW
        self.state = BlockModeSharedState(mode="fail")
        protected = self.state.databases["protected"]
        for index, package_id in enumerate((PACKAGE_ID, ALTERNATIVE_PACKAGE_ID)):
            protected.update_store(
                package_id,
                protected_blob(
                    f"Cohort.Member.{index}",
                    [[f"https://filecrypt.invalid/container/{index}", "filecrypt"]],
                ),
            )

    def call(self, rule, payload):
        app = Bottle()
        setup_sponsors_helper_routes(app)
        route = next(route for route in app.routes if route.rule == rule)
        built = []

        def build_service(shared_state):
            built.append(shared_state)
            return CrypterCooldownService(shared_state, clock=self.clock)

        with (
            mock.patch("quasarr.api.sponsors_helper.shared_state", self.state),
            mock.patch("quasarr.api.sponsors_helper.request", mock.Mock(json=payload)),
            mock.patch(
                "quasarr.api.sponsors_helper.CrypterCooldownService", build_service
            ),
        ):
            return route.callback(), built

    def test_the_cohort_path_only_opens_a_sweep_in_defer_mode(self):
        payload = {
            "supported_urls": list(HELPER_URLS),
            "capabilities": self.COHORT_CAPABILITIES,
        }

        bypassed, built = self.call("/sponsors_helper/api/to_decrypt/", payload)

        self.assertNotIn("crypter_offer", bypassed["to_decrypt"])
        self.assertIn("terminal_operation_id", bypassed["to_decrypt"])
        self.assertEqual([], built)
        self.assertEqual({}, self.state.databases["crypter_cooldowns"].rows)

        self.state.values["crypter_block_mode"] = "defer"
        offered, _ = self.call("/sponsors_helper/api/to_decrypt/", payload)

        self.assertEqual(
            FILECRYPT_COHORT_CAPABILITY,
            offered["to_decrypt"]["crypter_offer"]["capability"],
        )
        self.assertEqual("sweep", offered["to_decrypt"]["crypter_offer"]["mode"])
        self.assertIn("filecrypt", self.state.databases["crypter_cooldowns"].rows)

    def test_a_cohort_defer_report_in_fail_mode_reads_no_state(self):
        self.state.get_db = mock.Mock(
            side_effect=AssertionError("fail mode must not access cooldown state")
        )

        response, built = self.call(
            "/sponsors_helper/api/defer/",
            {
                "package_id": PACKAGE_ID,
                "crypter": "filecrypt",
                "reason_code": REASON,
                "link_fingerprint": "b" * 64,
                "sweep_id": "a" * 32,
                "offer_id": "c" * 32,
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
        self.assertEqual([], built)
        self.state.get_db.assert_not_called()


class CapturingServer:
    app = None

    def __init__(self, app, **_kwargs):
        type(self).app = app

    def serve_forever(self):
        return None


class DashboardControlsTests(unittest.TestCase):
    def setUp(self):
        # get_api() installs its dict as the process-global shared state, and
        # `crypter_block_mode` now steers selection and queue projection, so a
        # leaked "fail" here would silently rewrite later tests.
        previous_values = provider_shared_state.values
        previous_lock = provider_shared_state.lock
        self.addCleanup(provider_shared_state.set_state, previous_values, previous_lock)

    def _render_dashboard(self):
        shared_values = {
            "port": 8080,
            "sites": [],
            "internal_address": "http://quasarr.invalid:8080",
            "external_address": "http://quasarr.invalid:8080",
            "helper_active": False,
            "notification_settings": {},
            "timeout_slow_mode": {},
            "filecrypt_enabled": True,
            "crypter_block_mode": "fail",
            "crypter_cooldown_hours": 72,
            "filecrypt_sweep_window_minutes": 15,
            "filecrypt_sweep_window_override": None,
        }
        protected_db = mock.Mock()
        protected_db.retrieve_all_titles.return_value = None

        config_values = {
            "API": {"key": "synthetic-api-key"},
            "JDownloader": {},
            "Hostnames": {},
            "FlareSolverr": {},
            "Radarr": {},
            "Sonarr": {},
        }

        def config_factory(section):
            return SimpleNamespace(
                get=lambda key: config_values.get(section, {}).get(key)
            )

        empty_database = SimpleNamespace(retrieve=lambda _key: None)
        route_setup_names = (
            "setup_arr_routes",
            "setup_captcha_routes",
            "setup_config",
            "setup_statistics",
            "setup_sponsors_helper_routes",
            "setup_packages_routes",
        )

        with ExitStack() as stack:
            for setup_name in route_setup_names:
                stack.enter_context(mock.patch(f"quasarr.api.{setup_name}"))
            stack.enter_context(mock.patch("quasarr.api.Server", CapturingServer))
            stack.enter_context(
                mock.patch("quasarr.api.Config", side_effect=config_factory)
            )
            stack.enter_context(
                mock.patch("quasarr.api.DataBase", return_value=empty_database)
            )
            stack.enter_context(
                mock.patch(
                    "quasarr.api.get_jdownloader_status",
                    return_value={
                        "status_class": "success",
                        "status_text": "JDownloader connected",
                    },
                )
            )
            stack.enter_context(
                mock.patch("quasarr.api.get_all_hostname_issues", return_value={})
            )
            stack.enter_context(
                mock.patch("quasarr.api.get_login_required_hostnames", return_value=[])
            )
            stack.enter_context(
                mock.patch("quasarr.api.get_radarr_required_hostnames", return_value=[])
            )
            stack.enter_context(
                mock.patch("quasarr.api.get_sonarr_required_hostnames", return_value=[])
            )
            stack.enter_context(
                mock.patch("quasarr.api.is_radarr_configured", return_value=False)
            )
            stack.enter_context(
                mock.patch("quasarr.api.is_sonarr_configured", return_value=False)
            )
            stack.enter_context(
                mock.patch("quasarr.api.show_logout_link", return_value=False)
            )
            stack.enter_context(
                mock.patch("quasarr.api.shared_state.get_db", return_value=protected_db)
            )

            get_api(shared_values, threading.Lock())
            index_route = next(
                route for route in CapturingServer.app.routes if route.rule == "/"
            )
            return index_route.callback()

    def test_link_protection_renders_block_controls_and_api_save(self):
        html = self._render_dashboard()
        section_start = html.index('<details id="filecryptDetails">')
        section_end = html.index("</details>", section_start)
        link_protection_html = html[section_start:section_end]

        self.assertIn("Linkcrypter-wide access blocks", link_protection_html)
        self.assertIn('<select id="crypter-block-mode">', link_protection_html)
        self.assertIn('value="fail" selected', link_protection_html)
        self.assertIn(
            '<input type="number" id="crypter-cooldown-hours" min="24" step="1" value="72">',
            link_protection_html,
        )
        # The mode is the operator's escape hatch, so the section must say what
        # each mode does and that switching back restores the recorded blocks.
        self.assertIn("Fail restores the legacy behavior", link_protection_html)
        self.assertIn("kept but ignored until you switch back", link_protection_html)
        self.assertNotIn('type="password"', link_protection_html)
        self.assertNotIn("http://", link_protection_html)
        self.assertNotIn("https://", link_protection_html)
        self.assertIn("quasarrApiFetch('/api/crypter-block/settings'", html)
        self.assertIn("mode: modeSelect.value", html)
        self.assertIn("cooldown_hours: Number.parseInt", html)
        # Sweep window input and checkbox
        self.assertIn(
            '<input type="number" id="filecrypt-sweep-window-minutes" min="1" max="1440" step="1" value="15" disabled>',
            link_protection_html,
        )
        self.assertIn(
            'id="filecrypt-sweep-window-default" checked', link_protection_html
        )
        # POST payload carries null when checkbox checked, int otherwise
        self.assertIn("sweepWindowDefaultCheckbox.checked ? null", html)
        self.assertIn("filecrypt_sweep_window_minutes:", html)
        # Response restoration includes override field to drive checkbox/disabled state
        self.assertIn("filecrypt_sweep_window_override", html)


class CrypterBlockModeBehaviorTests(unittest.TestCase):
    """`fail` is the operator's escape hatch, so it must restore the exact
    pre-cooldown behavior instead of only short-circuiting `/defer/`. Every
    case below runs against real persisted cooldown and package defer metadata
    and asserts that metadata survives the switch, because a confirmed cooldown
    otherwise outlives the setting change by at least 24 hours."""

    def setUp(self):
        self.clock = lambda: NOW
        self.state = BlockModeSharedState(mode="defer")
        protected = self.state.databases["protected"]
        protected.update_store(
            PACKAGE_ID, protected_blob("Cooled.Package", [COOLED_LINK])
        )
        protected.update_store(
            ALTERNATIVE_PACKAGE_ID,
            protected_blob("Alternative.Package", [CLEAR_LINK]),
        )
        service = CrypterCooldownService(self.state, clock=self.clock)
        # Three distinct observations are the confirmed-cooldown threshold, so
        # the crypter itself - not just this package - is held.
        for index, package_id in enumerate((PACKAGE_ID, *EVIDENCE_PACKAGE_IDS)):
            decision = service.observe("filecrypt", package_id, f"{index}" * 64, REASON)
        self.assertEqual("cooldown", decision["state"])
        service.defer_package(
            PACKAGE_ID,
            "filecrypt",
            REASON,
            decision["package_retry_after_epoch"],
            0,
        )
        self.persisted = self._persisted_rows()

    def _persisted_rows(self):
        return {
            table: copy.deepcopy(database.rows)
            for table, database in self.state.databases.items()
        }

    def _cooldown_reads(self):
        return self.state.databases["crypter_cooldowns"].retrieve_count

    def call_to_decrypt(self, payload):
        """Returns the response plus every shared_state the hot path built a
        cooldown service for; an empty list proves no cooldown read happened."""
        app = Bottle()
        setup_sponsors_helper_routes(app)
        route = next(
            route
            for route in app.routes
            if route.rule == "/sponsors_helper/api/to_decrypt/"
        )
        built = []

        def build_service(shared_state):
            built.append(shared_state)
            return CrypterCooldownService(shared_state, clock=self.clock)

        with (
            mock.patch("quasarr.api.sponsors_helper.shared_state", self.state),
            mock.patch("quasarr.api.sponsors_helper.request", mock.Mock(json=payload)),
            mock.patch(
                "quasarr.api.sponsors_helper.CrypterCooldownService", build_service
            ),
        ):
            return route.callback(), built

    def call_get_packages(self):
        built = []

        def build_service(shared_state):
            built.append(shared_state)
            return CrypterCooldownService(shared_state, clock=self.clock)

        with (
            mock.patch(
                "quasarr.downloads.packages.JDPackageCache", return_value=FakeCache()
            ),
            mock.patch(
                "quasarr.downloads.packages.get_download_category_from_package_id",
                return_value="movies",
            ),
            mock.patch(
                "quasarr.downloads.packages.CrypterCooldownService", build_service
            ),
        ):
            downloads = get_packages(self.state, auto_start=False)
        queue = {item["nzo_id"]: item for item in downloads["queue"]}
        return queue, built

    def capable_payload(self, **extra):
        payload = {
            "supported_urls": list(HELPER_URLS),
            "capabilities": [CRYPTER_DEFER_CAPABILITY],
        }
        payload.update(extra)
        return payload

    def test_mode_switch_flips_capable_selection_in_both_directions(self):
        payload = self.capable_payload()

        held, _ = self.call_to_decrypt(payload)
        self.assertEqual("Alternative.Package", held["to_decrypt"]["name"])

        self.state.values["crypter_block_mode"] = "fail"
        reads_before = self._cooldown_reads()
        legacy, built = self.call_to_decrypt(payload)

        self.assertEqual("Cooled.Package", legacy["to_decrypt"]["name"])
        self.assertEqual([COOLED_LINK], legacy["to_decrypt"]["url"])
        # The cached mode alone decides; the hot path never reads cooldown state.
        self.assertEqual([], built)
        self.assertEqual(reads_before, self._cooldown_reads())

        self.state.values["crypter_block_mode"] = "defer"
        restored, _ = self.call_to_decrypt(payload)

        self.assertEqual("Alternative.Package", restored["to_decrypt"]["name"])
        self.assertEqual(self.persisted, self._persisted_rows())

    def test_fail_mode_offers_every_alternative_link_of_one_package(self):
        self.state.databases["protected"].rows.pop(ALTERNATIVE_PACKAGE_ID)
        self.state.databases["protected"].update_store(
            PACKAGE_ID, protected_blob("Cooled.Package", [COOLED_LINK, CLEAR_LINK])
        )
        payload = self.capable_payload()

        held, _ = self.call_to_decrypt(payload)
        # Defer mode drops only the cooled link of the very same package.
        self.assertEqual([CLEAR_LINK], held["to_decrypt"]["url"])

        self.state.values["crypter_block_mode"] = "fail"
        legacy, _ = self.call_to_decrypt(payload)

        self.assertEqual([COOLED_LINK, CLEAR_LINK], legacy["to_decrypt"]["url"])

    def test_fail_mode_still_honors_capable_exclusions(self):
        self.state.values["crypter_block_mode"] = "fail"

        excluded, _ = self.call_to_decrypt(
            self.capable_payload(excluded_package_ids=[PACKAGE_ID])
        )

        # Exclusions are in-flight handout state, not a cooldown hold, so the
        # legacy bypass must not resurrect duplicate handouts.
        self.assertEqual("Alternative.Package", excluded["to_decrypt"]["name"])

    def test_fail_mode_capable_selection_still_enforces_the_package_contract(self):
        self.state.databases["protected"].update_store(
            MALFORMED_PACKAGE_ID, protected_blob("Malformed.Identifier", [CLEAR_LINK])
        )
        self.state.values["crypter_block_mode"] = "fail"
        reads_before = self._cooldown_reads()

        # A non-canonical ID cannot be normalized into an exclusion, so handing
        # it out would starve every later package permanently.
        selected, built = self.call_to_decrypt(
            self.capable_payload(excluded_package_ids=[MALFORMED_PACKAGE_ID])
        )

        self.assertEqual("Cooled.Package", selected["to_decrypt"]["name"])
        self.assertEqual(PACKAGE_ID, selected["to_decrypt"]["id"])
        self.assertEqual([], built)
        self.assertEqual(reads_before, self._cooldown_reads())

    def test_fail_mode_capable_exclusions_honor_custom_category_ids(self):
        self.state.databases["protected"].update_store(
            CUSTOM_CATEGORY_PACKAGE_ID,
            protected_blob("Custom.Category.Package", [CLEAR_LINK]),
        )
        self.state.values["crypter_block_mode"] = "fail"

        selected, _ = self.call_to_decrypt(self.capable_payload())
        self.assertEqual("Custom.Category.Package", selected["to_decrypt"]["name"])

        excluded, built = self.call_to_decrypt(
            self.capable_payload(excluded_package_ids=[CUSTOM_CATEGORY_PACKAGE_ID])
        )

        self.assertEqual("Cooled.Package", excluded["to_decrypt"]["name"])
        self.assertEqual([], built)

    def test_fail_mode_legacy_helper_keeps_selecting_malformed_ids(self):
        self.state.databases["protected"].update_store(
            MALFORMED_PACKAGE_ID, protected_blob("Malformed.Identifier", [CLEAR_LINK])
        )
        self.state.values["crypter_block_mode"] = "fail"

        selected, built = self.call_to_decrypt({"supported_urls": list(HELPER_URLS)})

        # Without the capability the helper cannot exclude anything, so the
        # legacy selector keeps its exact pre-cooldown behavior.
        self.assertEqual("Malformed.Identifier", selected["to_decrypt"]["name"])
        self.assertEqual([], built)

    def test_defer_mode_capable_selection_still_enforces_the_package_contract(self):
        self.state.databases["protected"].update_store(
            MALFORMED_PACKAGE_ID, protected_blob("Malformed.Identifier", [CLEAR_LINK])
        )

        selected, built = self.call_to_decrypt(self.capable_payload())

        self.assertEqual("Alternative.Package", selected["to_decrypt"]["name"])
        self.assertEqual([self.state], built)

    def test_legacy_helper_request_is_identical_in_both_modes(self):
        payload = {"supported_urls": list(HELPER_URLS)}

        deferred_mode, _ = self.call_to_decrypt(payload)
        self.state.values["crypter_block_mode"] = "fail"
        fail_mode, built = self.call_to_decrypt(payload)

        self.assertEqual(deferred_mode, fail_mode)
        self.assertEqual("Cooled.Package", fail_mode["to_decrypt"]["name"])
        self.assertEqual([], built)

    def test_mode_switch_flips_the_queue_projection_in_both_directions(self):
        held, _ = self.call_get_packages()
        self.assertEqual(
            "[Waiting for linkcrypter retry] Cooled.Package",
            held[PACKAGE_ID]["filename"],
        )
        self.assertTrue(held[PACKAGE_ID]["deferred"]["active"])

        self.state.values["crypter_block_mode"] = "fail"
        reads_before = self._cooldown_reads()
        legacy, built = self.call_get_packages()

        item = legacy[PACKAGE_ID]
        self.assertEqual("protected", item["type"])
        self.assertEqual("[CAPTCHA not solved!] Cooled.Package", item["filename"])
        self.assertNotIn("deferred", item)
        self.assertEqual([], built)
        self.assertEqual(reads_before, self._cooldown_reads())

        self.state.values["crypter_block_mode"] = "defer"
        restored, _ = self.call_get_packages()

        self.assertEqual(
            "[Waiting for linkcrypter retry] Cooled.Package",
            restored[PACKAGE_ID]["filename"],
        )
        self.assertEqual(self.persisted, self._persisted_rows())

    def test_unknown_and_missing_modes_fall_back_to_defer(self):
        for mode in (_UNSET, None, "", "FAIL", "pause", 0):
            with self.subTest(mode=mode):
                if mode is _UNSET:
                    self.state.values.pop("crypter_block_mode", None)
                else:
                    self.state.values["crypter_block_mode"] = mode

                selected, _ = self.call_to_decrypt(self.capable_payload())
                queue, _ = self.call_get_packages()

                self.assertEqual("Alternative.Package", selected["to_decrypt"]["name"])
                self.assertTrue(queue[PACKAGE_ID]["deferred"]["active"])


if __name__ == "__main__":
    unittest.main()
