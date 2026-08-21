# -*- coding: utf-8 -*-

"""Contracts for the Carbon Dashboard view.

Pins the pure ``build_dashboard_model`` builder (never touching
``shared_state.get_device()``/``get_packages()``), the rendered CAPTCHA
banner, the four-tile ``.cds-kpi-row`` status summary, the asynchronously
loaded queue tile scaffold (never populated server-side), the API access
tile, the all-time summary band, and the structural/privacy guards.
"""

import importlib
import io
import os
import threading
import unittest
from io import BytesIO
from unittest import mock


class _FakeProtectedDB:
    def __init__(self, titles):
        self._titles = titles

    def retrieve_all_titles(self):
        return self._titles


class _RaisingDevice:
    """Any attempt to treat this as a live JD device call must fail the test."""

    def __getattr__(self, name):
        raise AssertionError(
            f"build_dashboard_model must never touch shared_state.get_device() "
            f"or get_packages() (attempted .{name})"
        )


class _FakeSharedState:
    def __init__(self, **overrides):
        self.values = {
            "sites": [],
            "internal_address": "http://quasarr.invalid:8080",
            "database": lambda table: _FakeProtectedDB([]),
            "helper_active": False,
            "filecrypt_sweep_window_source": "default",
        }
        self.values.update(overrides)

    def get_device(self):
        raise AssertionError("build_dashboard_model must never call get_device()")

    def get_packages(self):
        raise AssertionError("build_dashboard_model must never call get_packages()")


class _FakeConfigSection:
    def __init__(self, data):
        self._data = data

    def get(self, key):
        return self._data.get(key)


_CONFIG_FIXTURES = {
    "JDownloader": {"device": "MyJD"},
    "API": {"key": "test-api-key"},
    "FlareSolverr": {"url": ""},
}


def _fake_config(section):
    return _FakeConfigSection(_CONFIG_FIXTURES.get(section, {}))


class _FakeDataBase:
    def __init__(self, table):
        self.table = table

    def retrieve(self, key):
        return None


def _fake_jd_status(shared_state):
    """Mirrors get_jdownloader_status()'s connected/device_name shape
    without its internal (real) Config("JDownloader") read.
    """
    device = shared_state.values.get("device")
    connected = device is not None and device is not False
    return {
        "connected": connected,
        "device_name": _CONFIG_FIXTURES["JDownloader"].get("device", ""),
        "status_text": "connected" if connected else "disconnected",
        "status_class": "success" if connected else "error",
    }


class CarbonDashboardModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = importlib.import_module("quasarr.api.carbon")

    def _build(self, shared_state=None, **stats_overrides):
        shared_state = shared_state or _FakeSharedState()
        stats = {
            "packages_downloaded": 12,
            "failed_downloads": 3,
            "total_captcha_decryptions": 9,
            "decryption_success_rate": 75.0,
        }
        stats.update(stats_overrides)

        with (
            mock.patch.object(self.mod, "Config", side_effect=_fake_config),
            mock.patch.object(self.mod, "DataBase", _FakeDataBase),
            mock.patch.object(
                self.mod, "get_jdownloader_status", side_effect=_fake_jd_status
            ),
            mock.patch.object(self.mod, "get_all_hostname_issues", return_value=set()),
            mock.patch.object(
                self.mod, "get_radarr_required_hostnames", return_value=[]
            ),
            mock.patch.object(
                self.mod, "get_sonarr_required_hostnames", return_value=[]
            ),
            mock.patch.object(
                self.mod, "get_login_required_hostnames", return_value=[]
            ),
            mock.patch.object(self.mod, "is_radarr_configured", return_value=False),
            mock.patch.object(self.mod, "is_sonarr_configured", return_value=False),
            mock.patch.object(self.mod, "show_logout_link", return_value=False),
            mock.patch.object(self.mod, "StatsHelper") as stats_helper_cls,
        ):
            stats_helper_cls.return_value.get_stats.return_value = stats
            return self.mod.build_dashboard_model(shared_state)

    def test_model_never_touches_device_or_packages(self):
        # _FakeSharedState raises on get_device()/get_packages(); building the
        # model must not trip either.
        self._build()

    def test_model_reads_cached_device_presence_only(self):
        shared_state = _FakeSharedState(device=object())
        model = self._build(shared_state=shared_state)
        self.assertTrue(model["jd_connected"])

    def test_model_disconnected_when_no_device_cached(self):
        model = self._build()
        self.assertFalse(model["jd_connected"])

    def test_model_carries_captcha_count_from_protected_db(self):
        shared_state = _FakeSharedState(
            database=lambda table: _FakeProtectedDB([("id-1", "t"), ("id-2", "t")])
        )
        model = self._build(shared_state=shared_state)
        self.assertEqual(model["captcha_count"], 2)

    def test_model_key_set(self):
        model = self._build()
        expected_keys = {
            "jd_connected",
            "jd_device_name",
            "hostnames_working",
            "hostnames_total",
            "hostnames_issue_line",
            "captcha_count",
            "helper_active",
            "flaresolverr_url",
            "flaresolverr_skipped",
            "flaresolverr_configured",
            "api_key",
            "internal_address",
            "stats",
            "show_user",
        }
        self.assertEqual(expected_keys, set(model.keys()))


class CarbonDashboardRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = importlib.import_module("quasarr.api.carbon")
        cls.templates = importlib.import_module("quasarr.providers.carbon_templates")

    def _render(self, **model_overrides):
        model = {
            "jd_connected": False,
            "jd_device_name": "MyJD",
            "hostnames_working": 3,
            "hostnames_total": 4,
            "hostnames_issue_line": "",
            "captcha_count": 0,
            "helper_active": False,
            "flaresolverr_url": "",
            "flaresolverr_skipped": False,
            "flaresolverr_configured": False,
            "api_key": "test-api-key-value",
            "internal_address": "http://quasarr.invalid:8080",
            "stats": {
                "packages_downloaded": 12,
                "failed_downloads": 3,
                "total_captcha_decryptions": 9,
                "decryption_success_rate": 75.0,
                "total_download_attempts": 15,
                "download_success_rate": 80.0,
            },
            "show_user": False,
        }
        model.update(model_overrides)
        with mock.patch.object(self.mod, "build_dashboard_model", return_value=model):
            html = self.mod.render_dashboard(object())
        return html, model

    def test_render_dashboard_exists(self):
        self.assertTrue(callable(self.mod.render_dashboard))

    def test_active_page_is_dashboard(self):
        html, _model = self._render()
        self.assertIn('href="/" aria-current="page"', html)
        self.assertIn("<title>Dashboard</title>", html)

    def test_four_status_tiles_in_kpi_row(self):
        html, _model = self._render()
        start = html.index('<div class="cds-kpi-row">')
        # The queue tile immediately follows the KPI row, so slicing up to
        # its id bounds the row without needing to balance nested </div>s.
        row_end = html.index('id="dashboard-queue-tile"', start)
        row_html = html[start:row_end]
        self.assertEqual(row_html.count("cds-tile--is-status"), 4)

    def test_status_tiles_use_dots_not_tags(self):
        html, _model = self._render()
        start = html.index('<div class="cds-kpi-row">')
        row_html = html[start : html.index('<div class="cds-grid--dashboard">', start)]
        self.assertEqual(row_html.count("cds-tile--is-status"), 4)
        self.assertNotIn("cds-tag", row_html)
        self.assertIn(
            'cds-status cds-status--error cds-status--strong">'
            '<span class="cds-status__dot" aria-hidden="true"></span>Disconnected',
            row_html,
        )
        for heading in ("JDownloader", "Hostnames", "FlareSolverr", "SponsorsHelper"):
            self.assertIn(f'<h2 class="cds-tile__heading">{heading}</h2>', row_html)

    def test_captcha_banner_is_single_line_with_ghost_link(self):
        html, _model = self._render(captcha_count=2)
        self.assertIn(
            "<strong>Action required.</strong> 2 links are waiting for a CAPTCHA solution.",
            html,
        )
        self.assertIn(
            '<a class="cds-btn cds-btn--ghost" href="/captcha">Solve CAPTCHAs →</a>',
            html,
        )
        self.assertNotIn(
            "cds-btn--primary", html[: html.index('<div class="cds-kpi-row">')]
        )

    def test_captcha_banner_singular_link_uses_singular_verb(self):
        html, _model = self._render(captcha_count=1)
        self.assertIn(
            "<strong>Action required.</strong> 1 link is waiting for a CAPTCHA solution.",
            html,
        )
        self.assertIn(
            '<a class="cds-btn cds-btn--ghost" href="/captcha">Solve CAPTCHA →</a>',
            html,
        )

    def test_captcha_banner_plural_links_use_plural_verb(self):
        html, _model = self._render(captcha_count=3)
        self.assertIn(
            "<strong>Action required.</strong> 3 links are waiting for a CAPTCHA solution.",
            html,
        )

    def test_captcha_banner_absent_when_count_zero(self):
        html, _model = self._render(captcha_count=0)
        self.assertNotIn("cds-notification--inline", html)
        self.assertNotIn("Action required", html)

    def test_dashboard_uses_two_column_grid(self):
        html, _model = self._render()
        self.assertIn(
            '<div class="cds-grid--dashboard"><section class="cds-tile" id="dashboard-queue-tile"',
            html,
        )
        self.assertIn(
            '<div class="cds-stack"><section class="cds-tile" id="dashboard-api-tile">',
            html,
        )
        self.assertIn(
            '<a class="cds-btn cds-btn--ghost" href="/statistics">Statistics →</a>',
            html,
        )
        self.assertNotIn("<table", html)

    def test_queue_tile_scaffold_present_and_empty(self):
        html, _model = self._render()
        self.assertIn('id="dashboard-queue-tile"', html)
        self.assertIn('id="dashboard-queue-content"', html)
        # No package rows are rendered server-side - the tile is filled by
        # carbon.js after first paint.
        self.assertIn("Loading queue", html)

    def test_api_access_tile_present(self):
        html, _model = self._render()
        self.assertIn('id="dashboard-api-url"', html)
        self.assertIn('id="dashboard-api-key"', html)
        self.assertIn("test-api-key-value", html)
        self.assertIn('data-action="copy"', html)
        self.assertIn('data-action="reveal"', html)

    def test_all_time_summary_present(self):
        html, _model = self._render()
        self.assertIn('<h2 class="cds-tile__heading">All time</h2>', html)
        self.assertIn('<span class="cds-kv__label">Download attempts</span>', html)
        self.assertIn('<span class="cds-kv__label">Download success rate</span>', html)
        self.assertIn('<span class="cds-kv__label">CAPTCHA decryptions</span>', html)
        self.assertIn("15", html)
        self.assertIn("80.0%", html)

    def test_structural_guards_pass(self):
        html, _model = self._render(captcha_count=1)
        self.templates._assert_structural_guards(html)

    def test_no_forbidden_identifiers_or_remote_resources(self):
        """The only http(s) text on the page is the user's own Quasarr API
        URL (matching Classic's dashboard field) - never a source/protected
        URL or an external remote resource reference.
        """
        html, model = self._render()
        without_own_url = html.replace(model["internal_address"], "")
        self.assertNotIn("http://", without_own_url)
        self.assertNotIn("https://", without_own_url)


class CarbonDashboardHeadRequestTests(unittest.TestCase):
    """Proves a HEAD / response never performs JD I/O: Bottle answers HEAD by
    running the same GET callback and discarding the body, so this drives the
    full route -> render_page -> render_dashboard -> build_dashboard_model
    chain through a real Bottle app, with a shared_state that raises on any
    live device/package access and every disk-backed dependency stubbed out.
    """

    def setUp(self):
        from quasarr.providers import shared_state as provider_shared_state

        self.addCleanup(
            provider_shared_state.set_state,
            provider_shared_state.values,
            provider_shared_state.lock,
        )

    def _build_main_app(self):
        import quasarr.api as api

        setup_names = (
            "add_auth_routes",
            "add_auth_hook",
            "setup_static_routes",
            "setup_arr_routes",
            "setup_captcha_routes",
            "setup_config",
            "setup_statistics",
            "setup_sponsors_helper_routes",
            "setup_packages_routes",
            "setup_ui_preference_routes",
            "audit_route_auth_modes",
        )
        captured = {}

        class CapturingServer:
            def __init__(self, app, **_kwargs):
                captured["app"] = app

            def serve_forever(self):
                return None

        shared_state_dict = {
            "port": 8080,
            "ui_preference": "carbon",
            "sites": [],
            "internal_address": "http://quasarr.invalid:8080",
            "helper_active": False,
            "filecrypt_sweep_window_source": "default",
            "database": lambda table: _FakeProtectedDB([]),
        }

        with mock.patch.multiple(
            api, Server=CapturingServer, **{name: mock.DEFAULT for name in setup_names}
        ):
            api.get_api(shared_state_dict, threading.Lock())
        return captured["app"]

    def _request(self, app, method, path):
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": "",
            "SERVER_NAME": "localhost",
            "SERVER_PORT": "8080",
            "SERVER_PROTOCOL": "HTTP/1.1",
            "HTTP_HOST": "localhost:8080",
            "wsgi.url_scheme": "http",
            "wsgi.input": BytesIO(b""),
            "wsgi.errors": io.StringIO(),
        }
        captured = {}

        def start_response(status, headers, _exc_info=None):
            captured["status"] = status
            captured["headers"] = list(headers)

        body = b"".join(app(environ, start_response))
        return captured.get("status", "500"), captured.get("headers", []), body

    def test_head_root_never_touches_jdownloader(self):
        app = self._build_main_app()
        carbon_mod = importlib.import_module("quasarr.api.carbon")

        with (
            mock.patch.dict(os.environ, {"QUASARR_UI": "carbon"}, clear=True),
            mock.patch(
                "quasarr.providers.page_dispatch.carbon_assets_available",
                return_value=True,
            ),
            mock.patch.object(carbon_mod, "Config", side_effect=_fake_config),
            mock.patch.object(carbon_mod, "DataBase", _FakeDataBase),
            mock.patch.object(
                carbon_mod,
                "get_jdownloader_status",
                return_value={
                    "connected": False,
                    "device_name": "",
                    "status_text": "Disconnected",
                    "status_class": "error",
                },
            ),
            mock.patch.object(
                carbon_mod, "get_all_hostname_issues", return_value=set()
            ),
            mock.patch.object(
                carbon_mod, "get_radarr_required_hostnames", return_value=[]
            ),
            mock.patch.object(
                carbon_mod, "get_sonarr_required_hostnames", return_value=[]
            ),
            mock.patch.object(
                carbon_mod, "get_login_required_hostnames", return_value=[]
            ),
            mock.patch.object(carbon_mod, "is_radarr_configured", return_value=False),
            mock.patch.object(carbon_mod, "is_sonarr_configured", return_value=False),
            mock.patch.object(carbon_mod, "show_logout_link", return_value=False),
            mock.patch.object(carbon_mod, "StatsHelper") as stats_helper_cls,
        ):
            stats_helper_cls.return_value.get_stats.return_value = {
                "packages_downloaded": 1,
                "failed_downloads": 0,
                "total_captcha_decryptions": 0,
                "decryption_success_rate": 0,
            }
            status, headers, body = self._request(app, "HEAD", "/")

        self.assertEqual(status.split()[0], "200")
        self.assertEqual(body, b"")
        # A Carbon-mode 200 always carries the CSP header render_page applies
        # only after a successful Carbon render, proving the Carbon renderer
        # (not the Classic fallback) actually produced this response.
        csp_values = [
            v for name, v in headers if name.lower() == "content-security-policy"
        ]
        self.assertEqual(len(csp_values), 1)


if __name__ == "__main__":
    unittest.main()
