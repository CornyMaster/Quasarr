# -*- coding: utf-8 -*-

import copy
import threading
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import mock

from bottle import Bottle

from quasarr.api import get_api
from quasarr.api.config import setup_config
from quasarr.api.sponsors_helper import setup_sponsors_helper_routes
from quasarr.constants import CRYPTER_BLOCK_SETTINGS_TABLE
from quasarr.providers.auth import audit_route_auth_modes
from quasarr.storage.setup import (
    get_crypter_block_settings_data,
    initialize_crypter_block_settings,
    save_crypter_block_settings,
)

PACKAGE_ID = "Quasarr_movies_" + "a" * 32
DEFER_PAYLOAD = {
    "package_id": PACKAGE_ID,
    "crypter": "filecrypt",
    "reason_code": "ip_block_suspected",
    "link_fingerprint": "b" * 64,
}


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

        settings = initialize_crypter_block_settings(shared_state)

        self.assertEqual({"mode": "defer", "cooldown_hours": 24}, settings)
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

        result = get_crypter_block_settings_data(shared_state)

        self.assertEqual(
            {"success": True, "settings": {"mode": "fail", "cooldown_hours": 72}},
            result,
        )
        self.assertEqual(
            [
                ("retrieve", "mode"),
                ("retrieve", "cooldown_hours"),
                ("update", "crypter_block_mode", "fail"),
                ("update", "crypter_cooldown_hours", 72),
            ],
            events,
        )

    def test_save_persists_valid_settings_for_a_fresh_shared_state(self):
        shared_state = FakeSharedState()

        with mock.patch(
            "quasarr.storage.setup.crypter_blocks.request",
            mock.Mock(json={"mode": "fail", "cooldown_hours": 48}),
        ):
            result = save_crypter_block_settings(shared_state)

        self.assertTrue(result["success"])
        self.assertEqual({"mode": "fail", "cooldown_hours": 48}, result["settings"])
        self.assertEqual({"mode": "fail", "cooldown_hours": "48"}, self.database.rows)

        restored_state = FakeSharedState()
        self.assertEqual(
            {"mode": "fail", "cooldown_hours": 48},
            initialize_crypter_block_settings(restored_state),
        )

    def test_invalid_mode_preserves_current_mode_and_saves_valid_hours(self):
        self.database.rows.update({"mode": "fail", "cooldown_hours": "48"})
        shared_state = FakeSharedState()

        with mock.patch(
            "quasarr.storage.setup.crypter_blocks.request",
            mock.Mock(json={"mode": "pause", "cooldown_hours": 72}),
        ):
            result = save_crypter_block_settings(shared_state)

        self.assertEqual({"mode": "fail", "cooldown_hours": 72}, result["settings"])
        self.assertEqual({"mode": "fail", "cooldown_hours": "72"}, self.database.rows)

    def test_non_integer_hours_preserve_current_hours_and_save_valid_mode(self):
        self.database.rows.update({"mode": "defer", "cooldown_hours": "48"})
        shared_state = FakeSharedState()

        with mock.patch(
            "quasarr.storage.setup.crypter_blocks.request",
            mock.Mock(json={"mode": "fail", "cooldown_hours": "72"}),
        ):
            result = save_crypter_block_settings(shared_state)

        self.assertEqual({"mode": "fail", "cooldown_hours": 48}, result["settings"])
        self.assertEqual({"mode": "fail", "cooldown_hours": "48"}, self.database.rows)

    def test_hours_below_24_preserve_current_hours(self):
        self.database.rows.update({"mode": "defer", "cooldown_hours": "96"})
        shared_state = FakeSharedState()

        with mock.patch(
            "quasarr.storage.setup.crypter_blocks.request",
            mock.Mock(json={"mode": "defer", "cooldown_hours": 23}),
        ):
            result = save_crypter_block_settings(shared_state)

        self.assertEqual({"mode": "defer", "cooldown_hours": 96}, result["settings"])
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


class CapturingServer:
    app = None

    def __init__(self, app, **_kwargs):
        type(self).app = app

    def serve_forever(self):
        return None


class DashboardControlsTests(unittest.TestCase):
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
        self.assertNotIn('type="password"', link_protection_html)
        self.assertNotIn("http://", link_protection_html)
        self.assertNotIn("https://", link_protection_html)
        self.assertIn("quasarrApiFetch('/api/crypter-block/settings'", html)
        self.assertIn("mode: modeSelect.value", html)
        self.assertIn("cooldown_hours: Number.parseInt", html)


if __name__ == "__main__":
    unittest.main()
