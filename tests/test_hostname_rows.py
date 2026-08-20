import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from bottle import Bottle

from quasarr.constants import SEARCH_CAT_BOOKS, SEARCH_CAT_MOVIES, SEARCH_CAT_SHOWS
from quasarr.providers.auth import _AUTH_MODE_API_KEY, _AUTH_MODE_ATTR
from quasarr.storage.setup.hostnames import build_hostname_rows, get_hostnames_data

# ---------------------------------------------------------------------------
# Synthetic, deterministic fixture shared by every test in this file.
#
# Site labels ("GA".."SJ") are synthetic two-letter source identifiers, never
# real Quasarr source hostnames. "DJ"/"SJ" are used deliberately (matching the
# JUNKIES shared-credentials source pair the implementation hardcodes) but the
# hostname VALUES attached to them are synthetic ".invalid" domains, per
# tests/AGENTS.md.
# ---------------------------------------------------------------------------

SITES = ["GA", "GB", "GC", "GD", "GE", "DJ", "SJ"]

HOSTNAMES_DATA = {
    "gb": "gb-fixture.invalid",
    "gc": "gc-fixture.invalid",
    "gd": "gd-fixture.invalid",
    "ge": "ge-fixture.invalid",
    "dj": "dj-fixture.invalid",
    "sj": "sj-fixture.invalid",
}

SECRET_GC_PASSWORD = "gc-pass-secret"
SECRET_JUNKIES_PASSWORD = "junkies-pass-secret"

CONFIG_DATA = {
    "Hostnames": HOSTNAMES_DATA,
    "Settings": {"hostnames_url": "https://hostnames.invalid/list.ini"},
    "GC": {"user": "gc-user", "password": SECRET_GC_PASSWORD},
    "JUNKIES": {"user": "junkies-user", "password": SECRET_JUNKIES_PASSWORD},
}

SKIP_LOGIN_DATA = {"gc": "true"}
SKIP_FLARESOLVERR_DATA = {"skipped": "true"}

HOSTNAME_ISSUES = {
    "ge": {
        "operation": "search",
        "error": "Synthetic error text",
        "timestamp": "2026-01-01T00:00:00Z",
    }
}

SOURCE_METADATA = {
    "gb": {
        "language": "en",
        "categories": [SEARCH_CAT_MOVIES, SEARCH_CAT_SHOWS],
        "invite_only": True,
        "requires_login": False,
        "requires_account": False,
        "requires_flaresolverr": False,
    },
    "gc": {
        "language": "de",
        "categories": [],
        "invite_only": False,
        "requires_login": True,
        "requires_account": False,
        "requires_flaresolverr": False,
    },
    "gd": {
        "language": "fr",
        "categories": [SEARCH_CAT_BOOKS],
        "invite_only": False,
        "requires_login": False,
        "requires_account": True,
        "requires_flaresolverr": True,
    },
    "ge": {
        "language": "en",
        "categories": [],
        "invite_only": False,
        "requires_login": False,
        "requires_account": False,
        "requires_flaresolverr": False,
    },
    "dj": {
        "language": "de",
        "categories": [SEARCH_CAT_SHOWS],
        "invite_only": False,
        "requires_login": True,
        "requires_account": False,
        "requires_flaresolverr": False,
    },
    "sj": {
        "language": "de",
        "categories": [SEARCH_CAT_SHOWS],
        "invite_only": False,
        "requires_login": True,
        "requires_account": False,
        "requires_flaresolverr": False,
    },
}

LOGIN_REQUIRED = ["gc", "dj", "sj"]
RADARR_REQUIRED = ["gb"]
SONARR_REQUIRED = ["gb"]

MESSAGE = "<p>Synthetic setup message.</p>"

# Captured from the pre-refactor Classic hostname_form_html() (base
# commit 50bdd12) against the exact fixture above, via a throwaway capture
# script run before any extraction edits were made. These are the "golden"
# digests that must still match once hostname_form_html() becomes a thin
# consumer of build_hostname_rows().
GOLDEN_SHA256_SKIP_MANAGEMENT_TRUE = (
    "152fb710523408e60e62a57176ec16907ac3822dfb1a39800c71efebcad015a9"
)
GOLDEN_SHA256_SKIP_MANAGEMENT_FALSE = (
    "bae8efaf33fb9bb7baddcccc417dcc34ad3b99b1f4b83a4b84738a4a030c8329"
)


class _FakeConfigSection:
    def __init__(self, data):
        self._data = data

    def get(self, key):
        return self._data.get(key)


def _config_factory(config_data):
    def factory(section):
        return _FakeConfigSection(config_data.get(section, {}))

    return factory


class _FakeDataBase:
    def __init__(self, table_data):
        self._data = table_data

    def retrieve(self, key):
        return self._data.get(key)


def _database_factory(skip_login_data, skip_flaresolverr_data):
    def factory(table):
        if table == "skip_login":
            return _FakeDataBase(skip_login_data)
        if table == "skip_flaresolverr":
            return _FakeDataBase(skip_flaresolverr_data)
        return _FakeDataBase({})

    return factory


def _build_shared_state(sites):
    return SimpleNamespace(values={"sites": list(sites)})


def _patched_environment(
    *,
    config_data=CONFIG_DATA,
    skip_login_data=SKIP_LOGIN_DATA,
    skip_flaresolverr_data=SKIP_FLARESOLVERR_DATA,
    hostname_issues=HOSTNAME_ISSUES,
    source_metadata=SOURCE_METADATA,
    login_required=LOGIN_REQUIRED,
    radarr_required=RADARR_REQUIRED,
    sonarr_required=SONARR_REQUIRED,
    radarr_ok=True,
    sonarr_ok=True,
):
    """Return an ExitStack-friendly list of mock.patch context managers that
    make quasarr.storage.setup.hostnames deterministic and fully synthetic."""
    return [
        mock.patch(
            "quasarr.storage.setup.hostnames.Config",
            side_effect=_config_factory(config_data),
        ),
        mock.patch(
            "quasarr.storage.setup.hostnames.DataBase",
            side_effect=_database_factory(skip_login_data, skip_flaresolverr_data),
        ),
        mock.patch(
            "quasarr.storage.setup.hostnames.get_all_hostname_issues",
            return_value=hostname_issues,
        ),
        mock.patch(
            "quasarr.storage.setup.hostnames.get_source_metadata",
            return_value=source_metadata,
        ),
        mock.patch(
            "quasarr.storage.setup.hostnames.get_login_required_hostnames",
            return_value=login_required,
        ),
        mock.patch(
            "quasarr.storage.setup.hostnames.get_radarr_required_hostnames",
            return_value=radarr_required,
        ),
        mock.patch(
            "quasarr.storage.setup.hostnames.get_sonarr_required_hostnames",
            return_value=sonarr_required,
        ),
        mock.patch(
            "quasarr.storage.setup.radarr.is_radarr_configured",
            return_value=radarr_ok,
        ),
        mock.patch(
            "quasarr.storage.setup.sonarr.is_sonarr_configured",
            return_value=sonarr_ok,
        ),
    ]


class _PatchedEnvironmentTestCase(unittest.TestCase):
    def _enter_environment(self, **kwargs):
        for patcher in _patched_environment(**kwargs):
            self.addCleanup(patcher.stop)
            patcher.start()


EXPECTED_ROW_KEYS = {
    "id",
    "label",
    "hostname",
    "status",
    "status_emoji",
    "status_title",
    "details",
    "timestamp",
    "operation",
    "missing_arr_client",
    "supports_login",
    "credential_section",
    "skip_login",
    "language",
    "categories",
    "invite_only",
    "requires_login",
    "requires_account",
    "requires_flaresolverr",
}


class HostnameRowBuilderTests(_PatchedEnvironmentTestCase):
    def setUp(self):
        self._enter_environment()
        self.shared_state = _build_shared_state(SITES)
        self.rows = build_hostname_rows(self.shared_state)
        self.by_id = {row["id"]: row for row in self.rows}

    def test_preserves_source_order(self):
        self.assertEqual(
            [row["id"] for row in self.rows],
            ["ga", "gb", "gc", "gd", "ge", "dj", "sj"],
        )

    def test_row_shape_is_exact_key_set(self):
        for row in self.rows:
            with self.subTest(id=row["id"]):
                self.assertEqual(set(row), EXPECTED_ROW_KEYS)

    def test_unset_hostname_status(self):
        row = self.by_id["ga"]
        self.assertEqual(row["hostname"], "")
        self.assertEqual(row["status"], "unset")
        self.assertEqual(row["status_emoji"], "⚫️")
        self.assertEqual(row["status_title"], "Hostname not configured")
        self.assertEqual(row["details"], "This hostname is not configured.")
        self.assertEqual(row["timestamp"], "")
        self.assertEqual(row["operation"], "")
        self.assertIsNone(row["missing_arr_client"])
        self.assertFalse(row["supports_login"])
        self.assertIsNone(row["credential_section"])
        self.assertFalse(row["skip_login"])
        self.assertIsNone(row["language"])
        self.assertEqual(row["categories"], [])

    def test_ok_status_with_capability_flags(self):
        row = self.by_id["gb"]
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["status_emoji"], "🟢")
        self.assertEqual(row["status_title"], "Working normally")
        self.assertIsNone(row["missing_arr_client"])
        self.assertFalse(row["supports_login"])
        self.assertIsNone(row["credential_section"])
        self.assertEqual(row["language"], "en")
        self.assertEqual(row["categories"], [SEARCH_CAT_MOVIES, SEARCH_CAT_SHOWS])
        self.assertTrue(row["invite_only"])

    def test_skipped_login_status(self):
        row = self.by_id["gc"]
        self.assertEqual(row["hostname"], "gc-fixture.invalid")
        self.assertEqual(row["status"], "skipped")
        self.assertEqual(row["status_emoji"], "🟡")
        self.assertEqual(row["status_title"], "Login was skipped")
        self.assertEqual(row["details"], "Login was skipped for this site.")
        self.assertTrue(row["supports_login"])
        self.assertEqual(row["credential_section"], "GC")
        self.assertTrue(row["skip_login"])

    def test_error_status_from_hostname_issue(self):
        row = self.by_id["ge"]
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["status_emoji"], "🔴")
        self.assertEqual(row["operation"], "search")
        self.assertEqual(row["details"], "Synthetic error text")
        self.assertEqual(row["timestamp"], "2026-01-01T00:00:00Z")
        self.assertEqual(row["status_title"], "Error in search")

    def test_account_and_flaresolverr_flags_without_login(self):
        row = self.by_id["gd"]
        self.assertFalse(row["supports_login"])
        self.assertIsNone(row["credential_section"])
        self.assertTrue(row["requires_account"])
        self.assertTrue(row["requires_flaresolverr"])
        self.assertFalse(row["requires_login"])

    def test_dj_and_sj_share_junkies_credential_section(self):
        dj = self.by_id["dj"]
        sj = self.by_id["sj"]
        self.assertTrue(dj["supports_login"])
        self.assertTrue(sj["supports_login"])
        self.assertEqual(dj["credential_section"], "JUNKIES")
        self.assertEqual(sj["credential_section"], "JUNKIES")

    def test_rows_never_carry_credential_values(self):
        for row in self.rows:
            self.assertNotIn("user", row)
            self.assertNotIn("password", row)
            serialized = json.dumps(row)
            self.assertNotIn(SECRET_GC_PASSWORD, serialized)
            self.assertNotIn(SECRET_JUNKIES_PASSWORD, serialized)
            self.assertNotIn("gc-user", serialized)
            self.assertNotIn("junkies-user", serialized)


class MissingArrClientVariantTests(_PatchedEnvironmentTestCase):
    """Pins that build_hostname_rows keeps *arr requirement resolution intact:
    single-client requirements report the missing client, dual-category
    hostnames accept either configured client."""

    SITES = ["MA", "MB", "MC"]
    HOSTNAMES = {
        "ma": "ma-fixture.invalid",
        "mb": "mb-fixture.invalid",
        "mc": "mc-fixture.invalid",
    }
    RADARR_REQUIRED = ["ma", "mc"]
    SONARR_REQUIRED = ["mb", "mc"]

    def _rows_for(self, radarr_ok, sonarr_ok):
        self._enter_environment(
            config_data={"Hostnames": self.HOSTNAMES},
            skip_login_data={},
            hostname_issues={},
            source_metadata={},
            login_required=[],
            radarr_required=self.RADARR_REQUIRED,
            sonarr_required=self.SONARR_REQUIRED,
            radarr_ok=radarr_ok,
            sonarr_ok=sonarr_ok,
        )
        shared_state = _build_shared_state(self.SITES)
        return {row["id"]: row for row in build_hostname_rows(shared_state)}

    def test_only_sonarr_configured(self):
        rows = self._rows_for(radarr_ok=False, sonarr_ok=True)
        self.assertEqual(rows["ma"]["missing_arr_client"], "Radarr")
        self.assertIsNone(rows["mb"]["missing_arr_client"])
        self.assertIsNone(rows["mc"]["missing_arr_client"])

    def test_neither_configured(self):
        rows = self._rows_for(radarr_ok=False, sonarr_ok=False)
        self.assertEqual(rows["ma"]["missing_arr_client"], "Radarr")
        self.assertEqual(rows["mb"]["missing_arr_client"], "Sonarr")
        self.assertEqual(rows["mc"]["missing_arr_client"], "Radarr or Sonarr")
        self.assertEqual(rows["mc"]["status"], "error")
        self.assertEqual(rows["mc"]["status_title"], "Radarr or Sonarr not configured")


class ClassicGoldenOutputTests(_PatchedEnvironmentTestCase):
    """Proves hostname_form_html() output is byte-for-byte unchanged after the
    build_hostname_rows() extraction, by comparing against SHA-256 digests
    captured from the pre-refactor implementation (see module docstring)."""

    def setUp(self):
        self._enter_environment()
        self.shared_state = _build_shared_state(SITES)

    def _render(self, show_skip_management):
        from quasarr.storage.setup.hostnames import hostname_form_html

        return hostname_form_html(self.shared_state, MESSAGE, show_skip_management)

    def test_golden_html_with_skip_management(self):
        html = self._render(True)
        digest = hashlib.sha256(html.encode("utf-8")).hexdigest()
        self.assertEqual(digest, GOLDEN_SHA256_SKIP_MANAGEMENT_TRUE)

    def test_golden_html_without_skip_management(self):
        html = self._render(False)
        digest = hashlib.sha256(html.encode("utf-8")).hexdigest()
        self.assertEqual(digest, GOLDEN_SHA256_SKIP_MANAGEMENT_FALSE)

    def test_positional_skip_indicator_is_kept(self):
        html = self._render(True)
        gc_entry = html.index('id="gc"')
        skip_marker = html.index("skip-indicator-gc")
        gd_entry = html.index('id="gd"')
        self.assertTrue(gc_entry < skip_marker < gd_entry)

    def test_skip_indicator_absent_when_management_disabled(self):
        html = self._render(False)
        self.assertNotIn("skip-indicator-gc", html)


class HostnameApiProjectionTests(_PatchedEnvironmentTestCase):
    """Pins the secret-free GET /api/hostnames projection and its auth mode."""

    def setUp(self):
        self._enter_environment()
        self.shared_state = _build_shared_state(SITES)

    def test_get_hostnames_data_is_secret_free(self):
        data = get_hostnames_data(self.shared_state)
        self.assertIn("hostnames", data)
        rows = data["hostnames"]
        self.assertEqual(len(rows), len(SITES))

        serialized = json.dumps(data)
        self.assertNotIn(SECRET_GC_PASSWORD, serialized)
        self.assertNotIn(SECRET_JUNKIES_PASSWORD, serialized)
        self.assertNotIn("gc-user", serialized)
        self.assertNotIn("junkies-user", serialized)
        for row in rows:
            self.assertNotIn("user", row)
            self.assertNotIn("password", row)

        # Configuration values ARE allowed to appear - the authenticated
        # hostname editor owns these values.
        self.assertIn("gb-fixture.invalid", serialized)

    def test_get_hostnames_data_excludes_status_emoji(self):
        """Carbon markup has no emoji. `status_emoji` stays on
        build_hostname_rows() itself - hostname_form_html() (Classic) still
        renders it directly from that shared row dict, byte-for-byte
        unchanged - but the JSON projection this endpoint answers is Carbon
        carbon.js's only consumer, and Carbon no longer displays it.
        """
        data = get_hostnames_data(self.shared_state)
        for row in data["hostnames"]:
            self.assertNotIn("status_emoji", row)
        # Classic's in-process consumer is unaffected by the projection cut.
        for row in build_hostname_rows(self.shared_state):
            self.assertIn("status_emoji", row)

    def test_get_hostnames_data_matches_build_hostname_rows_minus_status_emoji(self):
        data = get_hostnames_data(self.shared_state)
        expected = [
            {key: value for key, value in row.items() if key != "status_emoji"}
            for row in build_hostname_rows(self.shared_state)
        ]
        self.assertEqual(data["hostnames"], expected)

    def test_route_is_registered_with_api_key_auth(self):
        from quasarr.api.config import setup_config

        app = Bottle()
        setup_config(app, self.shared_state)

        matches = [
            route
            for route in app.routes
            if route.rule == "/api/hostnames" and route.method == "GET"
        ]
        self.assertEqual(len(matches), 1)
        route = matches[0]
        self.assertEqual(
            getattr(route.callback, _AUTH_MODE_ATTR, None), _AUTH_MODE_API_KEY
        )

    def test_route_passes_auth_audit(self):
        from quasarr.api.config import setup_config
        from quasarr.providers.auth import audit_route_auth_modes

        app = Bottle()
        setup_config(app, self.shared_state)

        # Must not raise.
        audit_route_auth_modes(
            app,
            api_key_prefixes=("/api", "/download/", "/sponsors_helper/api/"),
            public_whitelist=(".user.js",),
        )

    def test_existing_post_route_is_unchanged(self):
        from quasarr.api.config import setup_config

        app = Bottle()
        setup_config(app, self.shared_state)

        matches = [
            route
            for route in app.routes
            if route.rule == "/api/hostnames" and route.method == "POST"
        ]
        self.assertEqual(len(matches), 1)
        self.assertEqual(
            getattr(matches[0].callback, _AUTH_MODE_ATTR, None), _AUTH_MODE_API_KEY
        )


if __name__ == "__main__":
    unittest.main()
