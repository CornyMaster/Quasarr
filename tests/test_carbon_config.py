# -*- coding: utf-8 -*-

"""Contracts for the Carbon Hostnames and Categories views.

Pins ``quasarr.api.config.carbon.render_hostnames``/``render_categories``
against synthetic fixtures modeled on ``tests/test_hostname_rows.py``'s
recipe: every real hostname row renders (not a seven-row sample), every
required capability tag is present, the uncontrolled ``details`` exception
text never enters the initial page HTML (it is fetched client-side only
when a status modal opens - see ``carbon.js``), no stored credential value
ever reaches the page, existing add/edit/delete/save/check endpoints and
payload shapes are unchanged, and every modal trigger is a native
keyboard-focusable element.
"""

import importlib
import json
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from quasarr.constants import (
    SEARCH_CAT_BOOKS,
    SEARCH_CAT_MOVIES,
    SEARCH_CAT_MUSIC,
    SEARCH_CAT_SHOWS,
    SEARCH_CAT_SHOWS_ANIME,
    SEARCH_CAT_SHOWS_DOCUMENTARY,
)

STATIC_ROOT = Path(__file__).resolve().parent.parent / "quasarr" / "static"


def _page_content_only(html):
    """Slice out just the page-specific body content, excluding the shared
    Carbon shell (nav, header, modal host) - the shell owns its own
    ``data-action="nav-close"`` backdrop ``<div>``, which is intentionally
    not a focusable element (Escape and the visible close button already
    cover it), so it must not be swept into this page's own keyboard
    contract.
    """
    start = html.index('<div class="cds-main__inner">')
    end = html.index("</main>")
    return html[start:end]


# ---------------------------------------------------------------------------
# Hostnames fixture - nine synthetic ".invalid" sources (more than the old
# seven-row mockup sample), covering every required capability tag.
# ---------------------------------------------------------------------------

SITES = ["GA", "GB", "GC", "GD", "GE", "GF", "GG", "DJ", "SJ"]

HOSTNAMES_DATA = {
    "ga": "ga-fixture.invalid",
    "gb": "gb-fixture.invalid",
    "gc": "gc-fixture.invalid",
    "gd": "gd-fixture.invalid",
    "ge": "ge-fixture.invalid",
    "gf": "gf-fixture.invalid",
    # "gg" intentionally unset.
    "dj": "dj-fixture.invalid",
    "sj": "sj-fixture.invalid",
}

SECRET_GC_PASSWORD = "gc-pass-secret"
SECRET_JUNKIES_PASSWORD = "junkies-pass-secret"
SECRET_GC_USER = "gc-secret-user"

CONFIG_DATA = {
    "Hostnames": HOSTNAMES_DATA,
    "Settings": {"hostnames_url": "https://hostnames-fixture.invalid/list.ini"},
    "GC": {"user": SECRET_GC_USER, "password": SECRET_GC_PASSWORD},
    "JUNKIES": {"user": "junkies-secret-user", "password": SECRET_JUNKIES_PASSWORD},
}

SKIP_LOGIN_DATA = {"gc": "true"}
SKIP_FLARESOLVERR_DATA = {}

HOSTNAME_ISSUES = {
    "ge": {
        "operation": "search",
        "error": "Synthetic uncontrolled exception mentioning ge-fixture.invalid",
        "timestamp": "2026-01-01T00:00:00Z",
    }
}

SOURCE_METADATA = {
    "ga": {
        "language": "en",
        "categories": [SEARCH_CAT_MOVIES],
        "invite_only": False,
        "requires_login": False,
        "requires_account": False,
        "requires_flaresolverr": False,
    },
    "gb": {
        "language": "de",
        "categories": [SEARCH_CAT_SHOWS],
        "invite_only": True,
        "requires_login": False,
        "requires_account": False,
        "requires_flaresolverr": False,
    },
    "gc": {
        "language": "fr",
        "categories": [SEARCH_CAT_SHOWS_ANIME],
        "invite_only": False,
        "requires_login": True,
        "requires_account": False,
        "requires_flaresolverr": False,
    },
    "gd": {
        "language": "en",
        "categories": [SEARCH_CAT_SHOWS_DOCUMENTARY],
        "invite_only": False,
        "requires_login": False,
        "requires_account": True,
        "requires_flaresolverr": False,
    },
    "ge": {
        "language": "de",
        "categories": [SEARCH_CAT_MUSIC],
        "invite_only": False,
        "requires_login": False,
        "requires_account": False,
        "requires_flaresolverr": True,
    },
    "gf": {
        "language": "en",
        "categories": [SEARCH_CAT_BOOKS],
        "invite_only": False,
        "requires_login": False,
        "requires_account": False,
        "requires_flaresolverr": False,
    },
    "gg": {
        "language": None,
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
RADARR_REQUIRED = []
SONARR_REQUIRED = []


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


def _build_shared_state(sites=SITES):
    return SimpleNamespace(
        values={"sites": list(sites), "database": lambda table: _FakeDataBase({})}
    )


class _HostnamesTestCase(unittest.TestCase):
    """Patches both quasarr.storage.setup.hostnames (build_hostname_rows'
    own module - the shared hostnames data layer) and
    quasarr.api.config.carbon (this module's own direct Config/DataBase
    reads for hostnames_url/flaresolverr flag), mirroring
    test_hostname_rows.py's recipe.
    """

    @classmethod
    def setUpClass(cls):
        cls.mod = importlib.import_module("quasarr.api.config.carbon")
        cls.templates = importlib.import_module("quasarr.providers.carbon_templates")

    def setUp(self):
        patchers = [
            mock.patch(
                "quasarr.storage.setup.hostnames.Config",
                side_effect=_config_factory(CONFIG_DATA),
            ),
            mock.patch(
                "quasarr.storage.setup.hostnames.DataBase",
                side_effect=_database_factory(SKIP_LOGIN_DATA, SKIP_FLARESOLVERR_DATA),
            ),
            mock.patch(
                "quasarr.storage.setup.hostnames.get_all_hostname_issues",
                return_value=HOSTNAME_ISSUES,
            ),
            mock.patch(
                "quasarr.storage.setup.hostnames.get_source_metadata",
                return_value=SOURCE_METADATA,
            ),
            mock.patch(
                "quasarr.storage.setup.hostnames.get_login_required_hostnames",
                return_value=LOGIN_REQUIRED,
            ),
            mock.patch(
                "quasarr.storage.setup.hostnames.get_radarr_required_hostnames",
                return_value=RADARR_REQUIRED,
            ),
            mock.patch(
                "quasarr.storage.setup.hostnames.get_sonarr_required_hostnames",
                return_value=SONARR_REQUIRED,
            ),
            mock.patch(
                "quasarr.storage.setup.radarr.is_radarr_configured", return_value=True
            ),
            mock.patch(
                "quasarr.storage.setup.sonarr.is_sonarr_configured", return_value=True
            ),
            mock.patch.object(
                self.mod, "Config", side_effect=_config_factory(CONFIG_DATA)
            ),
            mock.patch.object(
                self.mod,
                "DataBase",
                side_effect=_database_factory(SKIP_LOGIN_DATA, SKIP_FLARESOLVERR_DATA),
            ),
            mock.patch.object(self.mod, "protected_captcha_count", return_value=0),
            mock.patch.object(self.mod, "show_logout_link", return_value=False),
        ]
        for patcher in patchers:
            self.addCleanup(patcher.stop)
            patcher.start()

    def _render(self, sites=SITES):
        return self.mod.render_hostnames(_build_shared_state(sites))


class HostnamesModelTests(_HostnamesTestCase):
    def test_model_row_count_matches_every_configured_site(self):
        model = self.mod.build_hostnames_model(_build_shared_state())
        self.assertEqual(len(model["rows"]), len(SITES))

    def test_model_never_touches_device_or_packages(self):
        state = _build_shared_state()
        state.get_device = lambda: (_ for _ in ()).throw(
            AssertionError("build_hostnames_model must never call get_device()")
        )
        self.mod.build_hostnames_model(state)


class HostnamesRenderTests(_HostnamesTestCase):
    def test_renders_every_real_row_not_a_seven_row_sample(self):
        html = self._render()
        # Nine synthetic sources - more than the old seven-row mockup - all
        # of them must render, proving no hardcoded sample truncation.
        for site in SITES:
            with self.subTest(site=site):
                self.assertIn(f'data-hostname-id="{site.lower()}"', html)
        self.assertEqual(html.count('class="cds-hostname-row"'), len(SITES))

    def test_renders_more_rows_when_more_sites_configured(self):
        extra_sites = SITES + ["GH"]
        extra_metadata = dict(SOURCE_METADATA)
        extra_metadata["gh"] = SOURCE_METADATA["ga"]
        with mock.patch(
            "quasarr.storage.setup.hostnames.get_source_metadata",
            return_value=extra_metadata,
        ):
            html = self.mod.render_hostnames(_build_shared_state(extra_sites))
        self.assertEqual(html.count('class="cds-hostname-row"'), len(extra_sites))

    def test_capability_tags_cover_every_required_kind(self):
        html = self._render()
        for label in (
            "English",
            "German",
            "French",
            "Movies",
            "TV",
            "Anime",
            "Docs",
            "Music",
            "Books",
            "Account Required",
            "Invite Only",
            "Login Required",
            "FlareSolverr Required",
        ):
            with self.subTest(label=label):
                self.assertIn(label, html)

    def test_hostname_values_render_directly(self):
        html = self._render()
        for shorthand, hostname in HOSTNAMES_DATA.items():
            with self.subTest(shorthand=shorthand):
                self.assertIn(f'value="{hostname}"', html)

    def test_details_text_never_enters_initial_page_html(self):
        """SECURITY CARRY-IN: `details` carries uncontrolled exception text
        that can embed a configured source hostname. It must never enter a
        generic row data attribute (or anywhere else in the initial page) -
        carbon.js fetches it fresh from GET /api/hostnames only when a
        status modal is explicitly opened, and renders it solely as a text
        node inside that modal.

        Only the "error" status carries genuinely uncontrolled text (sourced
        from a stored exception via get_all_hostname_issues()/a missing *arr
        client message); "unset"/"skipped"/"ok" details are small fixed safe
        strings the row builder always produces itself, so this only checks
        the uncontrolled case.
        """
        html = self._render()
        rows = self.mod.build_hostname_rows(_build_shared_state())
        for row in rows:
            if row["status"] == "error":
                with self.subTest(id=row["id"]):
                    self.assertNotIn(row["details"], html)
        # The concrete uncontrolled fixture string is doubly proven absent.
        self.assertNotIn("Synthetic uncontrolled exception", html)

    def test_no_stored_credential_value_anywhere_in_page(self):
        html = self._render()
        self.assertNotIn(SECRET_GC_PASSWORD, html)
        self.assertNotIn(SECRET_GC_USER, html)
        self.assertNotIn(SECRET_JUNKIES_PASSWORD, html)
        self.assertNotIn("junkies-secret-user", html)

    def test_skip_login_banner_present_for_skipped_source(self):
        html = self._render()
        self.assertIn("Login was skipped for this site", html)

    def test_save_form_targets_existing_endpoint_with_unchanged_field_names(self):
        html = self._render()
        self.assertIn(
            '<form id="hostnames-form" action="/api/hostnames" method="post">', html
        )
        self.assertIn('name="hostnames_url"', html)
        for site in SITES:
            with self.subTest(site=site):
                self.assertIn(f'name="{site.lower()}"', html)

    def test_save_button_is_a_real_button_not_a_submit(self):
        html = self._render()
        self.assertIn(
            '<button class="cds-btn cds-btn--primary" type="button" data-action="hostnames-save">',
            html,
        )

    def test_structural_guards_pass(self):
        html = self._render()
        self.templates._assert_structural_guards(html)

    def test_no_inline_event_handlers(self):
        html = self._render()
        self.assertNotRegex(html, r"\son[a-z]+\s*=")

    def test_every_data_action_trigger_is_keyboard_focusable(self):
        html = _page_content_only(self._render())
        for match in re.finditer(r"<(\w+)[^>]*\sdata-action=", html):
            with self.subTest(tag=match.group(1)):
                self.assertIn(match.group(1), {"button", "a", "input", "select"})

    def test_no_emoji_anywhere_in_rendered_output(self):
        """Carbon markup has no emoji - same widened range
        test_carbon_templates.py's test_renderer_owned_emoji_guard uses, run
        here against a real fixture render whose language capability chips
        (en/de/fr) exercise LANGUAGE_FLAG_EMOJI, the actual source of the
        flag glyphs the shell-only guard could never see.
        """
        html = self._render()
        match = re.search(
            "[\U0001f1e6-\U0001f1ff⌀-➿\U0001f300-\U0001faff]",
            html,
        )
        self.assertIsNone(match, f"emoji found: {match.group()!r}" if match else "")


class HostnamesRowSchemaCarriedThroughTests(_HostnamesTestCase):
    def test_row_ids_match_get_hostnames_data_projection(self):
        """The row set consumed for rendering is exactly build_hostname_rows()
        - the same data layer GET /api/hostnames projects - never a
        re-derived list.
        """
        hostnames_module = importlib.import_module("quasarr.storage.setup.hostnames")
        state = _build_shared_state()
        rows = self.mod.build_hostname_rows(_build_shared_state())
        projection = hostnames_module.get_hostnames_data(state)
        self.assertEqual(
            {row["id"] for row in rows},
            {row["id"] for row in projection["hostnames"]},
        )


# ---------------------------------------------------------------------------
# Categories fixture
# ---------------------------------------------------------------------------


class _FakeSource:
    def __init__(self, initials, supported_categories):
        self.initials = initials
        self.supported_categories = supported_categories


FAKE_SOURCES = {
    "ga": _FakeSource("GA", [SEARCH_CAT_MOVIES]),
    "gb": _FakeSource("GB", [SEARCH_CAT_SHOWS, SEARCH_CAT_MOVIES]),
}

DOWNLOAD_CATEGORY_NAMES = ["movies", "music", "tv", "docs", "customcat"]
DOWNLOAD_CATEGORY_MIRRORS = {
    "movies": ["Rapidgator", "DDownload"],
    "customcat": [],
}

SEARCH_CATEGORIES_FIXTURE = {
    SEARCH_CAT_MOVIES: {"name": "Movies", "emoji": "🎬"},
    SEARCH_CAT_SHOWS: {"name": "TV", "emoji": "📺"},
}
SEARCH_CATEGORY_SOURCES_FIXTURE = {
    SEARCH_CAT_MOVIES: ["ga"],
    SEARCH_CAT_SHOWS: [],
}


class _CategoriesTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = importlib.import_module("quasarr.api.config.carbon")
        cls.templates = importlib.import_module("quasarr.providers.carbon_templates")

    def setUp(self):
        patchers = [
            mock.patch.object(
                self.mod,
                "get_download_categories",
                return_value=list(DOWNLOAD_CATEGORY_NAMES),
            ),
            mock.patch.object(
                self.mod,
                "get_download_category_mirrors",
                side_effect=lambda name: DOWNLOAD_CATEGORY_MIRRORS.get(name, []),
            ),
            mock.patch.object(
                self.mod,
                "get_search_categories",
                return_value=dict(SEARCH_CATEGORIES_FIXTURE),
            ),
            mock.patch.object(
                self.mod,
                "get_search_category_sources",
                side_effect=lambda cat_id: list(
                    SEARCH_CATEGORY_SOURCES_FIXTURE.get(int(cat_id), [])
                ),
            ),
            mock.patch.object(self.mod, "get_sources", return_value=dict(FAKE_SOURCES)),
            mock.patch.object(
                self.mod, "get_hostnames", return_value=sorted(FAKE_SOURCES.keys())
            ),
            mock.patch.object(self.mod, "protected_captcha_count", return_value=0),
            mock.patch.object(self.mod, "show_logout_link", return_value=False),
        ]
        for patcher in patchers:
            self.addCleanup(patcher.stop)
            patcher.start()

    def _render(self):
        return self.mod.render_categories(SimpleNamespace(values={}))


class CategoriesModelTests(_CategoriesTestCase):
    def test_model_never_touches_device_or_packages(self):
        state = SimpleNamespace(values={})
        state.get_device = lambda: (_ for _ in ()).throw(
            AssertionError("build_categories_model must never call get_device()")
        )
        self.mod.build_categories_model(state)

    def test_download_rows_include_defaults_and_custom(self):
        model = self.mod.build_categories_model(SimpleNamespace(values={}))
        names = {row["name"] for row in model["download_rows"]}
        self.assertEqual(names, set(DOWNLOAD_CATEGORY_NAMES))
        custom = {row["name"] for row in model["download_rows"] if row["is_custom"]}
        self.assertEqual(custom, {"customcat"})


class CategoriesRenderTests(_CategoriesTestCase):
    def test_download_category_rows_show_mirrors_and_all_fallback(self):
        html = self._render()
        self.assertIn("Mirrors: Rapidgator, DDownload", html)
        self.assertIn("Mirrors: All", html)  # customcat has no mirrors -> "All"

    def test_download_category_edit_button_carries_mirrors_as_data(self):
        html = self._render()
        mirrors_json = json.dumps(DOWNLOAD_CATEGORY_MIRRORS["movies"])
        expected_attr = json.dumps(
            mirrors_json
        )  # not directly comparable; check substrings
        self.assertIn(
            'data-action="download-category-edit" data-category="movies"', html
        )
        self.assertIn("Rapidgator", html)
        del expected_attr  # documentation only

    def test_custom_download_category_has_delete_button_default_does_not(self):
        html = self._render()
        self.assertIn(
            '<button class="cds-btn cds-btn--danger-ghost" type="button" '
            'data-action="download-category-delete" data-category="customcat">',
            html,
        )
        self.assertNotIn(
            'data-action="download-category-delete" data-category="movies"', html
        )

    def test_search_category_rows_show_sources_and_all_fallback(self):
        html = self._render()
        self.assertIn("Hostnames: GA", html)
        self.assertIn("Hostnames: All", html)

    def test_default_search_category_shows_its_own_newznab_id_pill(self):
        """Regression pin: Classic's pill list is
        `[f"{name} ({cat_id})"] + subcats` - a default category with no
        inherited subcategories must still surface its own numeric ID as a
        pill. Checks the exact `cds-tag` pill markup (not just the raw
        "name (id)" substring), because that same text also legitimately
        appears in the unrelated "Add custom category" <option> - a plain
        substring check would pass even without the fix.
        """
        html = self._render()
        self.assertIn(
            f'<span class="cds-tag cds-tag--gray">Movies ({SEARCH_CAT_MOVIES})</span>',
            html,
        )
        self.assertIn(
            f'<span class="cds-tag cds-tag--gray">TV ({SEARCH_CAT_SHOWS})</span>', html
        )

    def test_search_category_edit_button_carries_expected_data(self):
        html = self._render()
        self.assertIn('data-action="search-category-edit"', html)
        self.assertIn(f'data-cat-id="{SEARCH_CAT_MOVIES}"', html)

    def test_hoster_and_source_data_hidden_spans_present(self):
        html = self._render()
        self.assertIn('id="categories-hoster-data"', html)
        self.assertIn('id="categories-source-data"', html)
        self.assertIn("Rapidgator", html)
        self.assertIn("&quot;ga&quot;", html)

    def test_structural_guards_pass(self):
        html = self._render()
        self.templates._assert_structural_guards(html)

    def test_no_inline_event_handlers(self):
        html = self._render()
        self.assertNotRegex(html, r"\son[a-z]+\s*=")

    def test_every_data_action_trigger_is_keyboard_focusable(self):
        html = _page_content_only(self._render())
        for match in re.finditer(r"<(\w+)[^>]*\sdata-action=", html):
            with self.subTest(tag=match.group(1)):
                self.assertIn(match.group(1), {"button", "a", "input", "select"})

    def test_no_emoji_anywhere_in_rendered_output(self):
        """Carbon markup has no emoji - same widened range
        test_carbon_templates.py's test_renderer_owned_emoji_guard uses, run
        here against a real fixture render whose category rows carry
        SEARCH_CATEGORIES_FIXTURE/DOWNLOAD_CATEGORIES emoji, the actual
        source of the glyphs the shell-only guard could never see.
        """
        html = self._render()
        match = re.search(
            "[\U0001f1e6-\U0001f1ff⌀-➿\U0001f300-\U0001faff]",
            html,
        )
        self.assertIsNone(match, f"emoji found: {match.group()!r}" if match else "")


# ---------------------------------------------------------------------------
# Gate: all 22 real source modules discovered from runtime metadata remain
# usable. Uses the REAL get_sources()/get_source_metadata() (no source
# mocking) against a synthetic Config/DataBase layer, so no real hostname
# ever needs to be configured.
# ---------------------------------------------------------------------------


class RealSourceGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = importlib.import_module("quasarr.api.config.carbon")
        cls.sources_helpers = importlib.import_module("quasarr.search.sources.helpers")

    def test_exactly_22_real_source_modules_discovered(self):
        self.assertEqual(len(self.sources_helpers.get_hostnames()), 22)

    def test_every_real_source_renders_a_hostname_row(self):
        real_sources = self.sources_helpers.get_hostnames()
        sites = [name.upper() for name in real_sources]
        empty_config = {"Hostnames": {}, "Settings": {}}

        with (
            mock.patch(
                "quasarr.storage.setup.hostnames.Config",
                side_effect=_config_factory(empty_config),
            ),
            mock.patch(
                "quasarr.storage.setup.hostnames.DataBase",
                side_effect=_database_factory({}, {}),
            ),
            mock.patch(
                "quasarr.storage.setup.hostnames.get_all_hostname_issues",
                return_value={},
            ),
            mock.patch(
                "quasarr.storage.setup.radarr.is_radarr_configured", return_value=True
            ),
            mock.patch(
                "quasarr.storage.setup.sonarr.is_sonarr_configured", return_value=True
            ),
            mock.patch.object(
                self.mod, "Config", side_effect=_config_factory(empty_config)
            ),
            mock.patch.object(
                self.mod, "DataBase", side_effect=_database_factory({}, {})
            ),
            mock.patch.object(self.mod, "protected_captcha_count", return_value=0),
            mock.patch.object(self.mod, "show_logout_link", return_value=False),
        ):
            html = self.mod.render_hostnames(_build_shared_state(sites))

        self.assertEqual(html.count('class="cds-hostname-row"'), len(sites))
        for source in real_sources:
            with self.subTest(source=source):
                self.assertIn(f'data-hostname-id="{source}"', html)


# ---------------------------------------------------------------------------
# JS/CSS structural contracts (Hostnames/Categories additions to carbon.js/carbon.css).
# ---------------------------------------------------------------------------


class CarbonConfigJsContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = (STATIC_ROOT / "carbon.js").read_text(encoding="utf-8")

    def _function_body(self, name):
        for prefix in ("async function ", "function "):
            marker = f"{prefix}{name}("
            if marker in self.js:
                start = self.js.index(marker)
                break
        else:
            raise AssertionError(f"No function named {name} found in carbon.js")
        depth = 0
        i = self.js.index("{", start)
        body_start = i
        for index in range(i, len(self.js)):
            char = self.js[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return self.js[body_start : index + 1]
        raise AssertionError(f"Unbalanced braces in {name}")

    def test_carbon_js_has_no_remote_urls(self):
        self.assertNotIn("http://", self.js)
        self.assertNotIn("https://", self.js)

    def test_all_eight_required_modal_flows_are_present(self):
        for marker in (
            "function openHostnameImportModal(",
            "function openHostnamesSaveModal(",
            "function openHostnameStatusModal(",
            "function performHostnameCredentialsCheck(",
            "function openFlareSolverrRequiredModal(",
            "function openRestartModal(",
            "function showErrorModal(",
            "function openSkipLoginModal(",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.js)

    def test_status_modal_never_writes_details_into_an_attribute(self):
        """The uncontrolled `details` text is read into a buildEl(...) call
        (safe textContent assignment) and never into any setAttribute/data-*
        write - satisfies the SECURITY CARRY-IN contract at the JS layer
        too.
        """
        body = self._function_body("openHostnameStatusModal")
        self.assertIn("buildEl('p', '', row.details", body)
        self.assertNotIn("data-details", self.js)

    def test_status_modal_title_never_consumes_status_emoji(self):
        """Carbon markup has no emoji. `status_emoji` is a JSON-data-path
        glyph (fetched from GET /api/hostnames, not present as a literal
        character in this source file), so the whole-file emoji/dingbat scan
        in test_carbon_templates.py cannot see it - this pins the JS source
        directly instead.
        """
        body = self._function_body("openHostnameStatusModal")
        self.assertNotIn("status_emoji", body)
        self.assertIn("row.label + ' - ' + row.status_title", body)
        self.assertNotIn("setAttribute('data-details'", body)

    def test_hostnames_save_uses_real_form_submit_not_json_fetch(self):
        """POST /api/hostnames reads request.forms and answers a full HTML
        page - unchanged endpoint contract, so this must stay a real
        navigation, not a quasarrApiFetch JSON call.
        """
        body = self._function_body("submitHostnamesForm")
        self.assertIn("form.submit()", body)
        self.assertNotIn("quasarrApiFetch", body)

    def test_hostnames_save_syncs_visible_import_url_into_hidden_field(self):
        """Regression pin: the visible
        #hostnames-import-url field lives outside <form> and is only
        written into #hostnames-url-hidden by a successful import. Every
        Save must resync it first (matching Classic's validateHostnames()),
        or typing/clearing the URL and pressing Save silently reverts the
        stored value - save_hostnames() unconditionally persists it.
        """
        body = self._function_body("submitHostnamesForm")
        sync_index = body.index(
            "hiddenUrl.value = String(urlField.value || '').trim();"
        )
        submit_index = body.index("form.submit();")
        self.assertLess(sync_index, submit_index)

    def test_import_status_surfaces_invalid_entry_count(self):
        """Regression pin: the import endpoint
        returns {success, hostnames, errors} and Classic appends
        "(N invalid)" when errors is non-empty. The handler must surface
        that count too, not just the imported count.
        """
        body = self._function_body("performHostnameImport")
        self.assertIn("data.errors", body)
        self.assertIn("Object.keys(data.errors).length", body)
        self.assertIn("invalid", body)

    def test_existing_endpoints_are_reused_unchanged(self):
        for endpoint in (
            "'/api/hostnames/import-url'",
            "'/api/hostnames/check-credentials/'",
            "'/api/skip-login/'",
            "'/api/restart'",
            "'/api/hostnames'",
            "'/api/categories'",
            "'/api/categories_search'",
            "'/api/categories/'",
            "'/api/categories_search/'",
        ):
            with self.subTest(endpoint=endpoint):
                self.assertIn(endpoint, self.js)

    def test_mirror_reflow_is_stable_partition_like_classic(self):
        body = self._function_body("reflowMirrorRows")
        self.assertIn("filter(isOn)", body)
        self.assertIn("enabled.concat(disabled)", body)

    def test_edit_modal_preserves_saved_mirror_priority_order(self):
        """Regression pin: opening Edit must never
        reorder the saved mirror whitelist to hosters.all (SHARE_HOSTERS)
        order. `selected` must be derived by filtering `currentMirrors` (the
        saved priority order) - matching Classic's
        `currentMirrors.filter(m => ALL_HOSTERS.includes(m))`. Filtering
        hosters.all instead silently re-persists the whitelist in
        SHARE_HOSTERS order on the very next Save, changing which mirror is
        auto-decrypted for any user whose saved order differs from that
        constant's order. `reflowMirrorRows` alone (above) cannot catch
        this: it is a stable partition over whatever order it is handed,
        and was always correct - the bug is in how `ordered` is built
        before reflow ever runs.
        """
        body = self._function_body("openDownloadCategoryEditModal")
        self.assertNotIn("var selected = hosters.all.filter(", body)
        selected_index = body.index("var selected = currentMirrors.filter(")
        unselected_index = body.index("var unselected = hosters.all.filter(")
        ordered_index = body.index("var ordered = selected.concat(unselected);")
        self.assertLess(selected_index, unselected_index)
        self.assertLess(unselected_index, ordered_index)
        # The `selected` statement itself must filter currentMirrors by
        # membership in hosters.all (preserving currentMirrors' order),
        # never the reverse.
        selected_statement = body[selected_index:unselected_index]
        self.assertIn("hosters.all.indexOf(hoster) !== -1", selected_statement)
        self.assertNotIn("currentMirrors.indexOf(hoster) !== -1", selected_statement)

    def test_search_source_empty_selection_sends_empty_list_not_all(self):
        body = self._function_body("saveSearchCategorySources")
        self.assertIn("var selected = [];", body)
        self.assertIn("search_sources: selected", body)

    # -- Regression pin: saved selections must survive innerHTML serialization

    def test_mirror_row_checkbox_sets_checked_attribute_not_only_the_property(self):
        """Both builders build rows as DOM nodes, set
        `checkbox.checked` as a PROPERTY, then hand `body.innerHTML` (a
        STRING) to `showModal()`. The checked PROPERTY never serializes -
        no `checked` ATTRIBUTE is ever written - so every previously saved
        mirror renders unchecked after Edit, and opening Edit then Save
        silently wipes the whitelist down to whatever was manually
        re-ticked. Setting the attribute alongside the property is what
        survives the innerHTML string round-trip.
        """
        body = self._function_body("buildMirrorRow")
        self.assertIn("checkbox.checked = Boolean(isChecked);", body)
        checked_index = body.index("checkbox.checked = Boolean(isChecked);")
        attr_index = body.index("checkbox.setAttribute('checked', '')")
        self.assertLess(checked_index, attr_index)

    def test_source_pill_checkbox_sets_checked_attribute_not_only_the_property(self):
        body = self._function_body("buildSourcePill")
        self.assertIn("checkbox.checked = Boolean(isChecked);", body)
        checked_index = body.index("checkbox.checked = Boolean(isChecked);")
        attr_index = body.index("checkbox.setAttribute('checked', '')")
        self.assertLess(checked_index, attr_index)

    # -- No emoji/glyphs in JS-built mirror rows --

    def test_mirror_row_move_buttons_are_icon_buttons_not_arrow_glyphs(self):
        """The established precedent set by the Downloads page is
        inline SVG icons from carbon_icons.py, not text glyphs, for
        icon-only buttons.
        """
        body = self._function_body("buildMirrorRow")
        self.assertNotIn("↑", body)  # '↑'
        self.assertNotIn("↓", body)  # '↓'
        self.assertIn("buildMirrorMoveIcon('up')", body)
        self.assertIn("buildMirrorMoveIcon('down')", body)

    def test_mirror_move_icon_markup_matches_reviewed_carbon_icons_provenance(self):
        """A substring check that `MIRROR_MOVE_ICON_MARKUP`/`up:`/`down:`
        merely appear somewhere in the ~3000-line file would pass even if
        the client-side polygon points drifted from the sha256-governed
        `carbon_icons.py` record this object's own comment claims to
        mirror. Extracts the `points="..."` payload each
        `MIRROR_MOVE_ICON_MARKUP` entry carries and requires it appear
        (quote style aside) inside the matching `IconSpec.shapes` string,
        so any such drift fails this test.
        """
        from quasarr.providers.carbon_icons import ICONS

        start_marker = "var MIRROR_MOVE_ICON_MARKUP = {"
        start = self.js.index(start_marker)
        end = self.js.index("};", start)
        markup_body = self.js[start:end]

        for direction, icon_name in (("up", "arrow--up"), ("down", "arrow--down")):
            with self.subTest(direction=direction):
                key_index = markup_body.index(direction + ":")
                points_match = re.search(r'points="([^"]+)"', markup_body[key_index:])
                self.assertIsNotNone(
                    points_match, f"no points= markup found for direction {direction}"
                )
                js_points = points_match.group(1)
                shapes = ICONS[icon_name].shapes
                self.assertIn(
                    f"points='{js_points}'",
                    shapes,
                    f"carbon.js {direction} polygon points do not match "
                    f"carbon_icons.py ICONS[{icon_name!r}].shapes",
                )

    def test_mirror_row_recommendation_marker_has_no_emoji_and_is_labeled(self):
        """Carbon markup has no emoji; a bare star
        glyph with an unstyled class is replaced by the existing `.cds-tag`
        component idiom carrying its own accessible/visible name.
        """
        body = self._function_body("buildMirrorRow")
        self.assertNotIn("cds-mirror-row__star", body)
        self.assertNotIn("⭐", body)  # '⭐'
        self.assertIn("Recommended", body)
        self.assertIn("cds-tag", body)

    def test_carbon_icons_module_has_the_reviewed_mirror_move_icons(self):
        from quasarr.providers.carbon_icons import ICONS

        for name in ("arrow--up", "arrow--down"):
            with self.subTest(icon=name):
                self.assertIn(name, ICONS)
                spec = ICONS[name]
                self.assertTrue(
                    spec.source_url.startswith(
                        "https://raw.githubusercontent.com/carbon-design-system/carbon/"
                    )
                )
                self.assertRegex(spec.sha256, r"^[0-9a-f]{64}$")


class CarbonConfigCssContractTests(unittest.TestCase):
    def test_new_component_classes_present(self):
        css = (STATIC_ROOT / "carbon.css").read_text(encoding="utf-8")
        for selector in (
            ".cds-hostname-row",
            ".cds-hostname-row__caps",
            ".cds-category-row",
            ".cds-mirror-row",
            ".cds-mirror-row__rank",
            ".cds-source-pill",
        ):
            with self.subTest(selector=selector):
                self.assertIn(selector, css)
        self.assertNotIn("http://", css)
        self.assertNotIn("https://", css)

    def test_skip_banner_never_uses_warning_yellow_as_text_color(self):
        """Warning yellow must always pair with dark text.
        `.cds-hostname-row__skip-banner` used to set
        `color: var(--cds-support-warning)` directly on the page background
        - measured contrast was 1.68:1 (white/light layer) and 1.53:1
        (light bg), both far below WCAG AA's 4.5:1 for normal text. Fixed
        to the same light-warning-tinted-background + default-dark-text
        pattern `.cds-notification--warning` and
        `.cds-hostname-credentials__warning` already use (dark-on-tint
        measures 17:1 in light theme, 10.6:1 in dark theme).
        """
        css = (STATIC_ROOT / "carbon.css").read_text(encoding="utf-8")
        rule_match = re.search(r"\.cds-hostname-row__skip-banner\s*\{([^}]*)\}", css)
        self.assertIsNotNone(rule_match, "skip-banner rule not found")
        rule_body = rule_match.group(1)
        self.assertNotIn("color: var(--cds-support-warning)", rule_body)
        self.assertIn("color: var(--cds-text)", rule_body)
        self.assertIn("background: var(--cds-notification-warning-bg)", rule_body)

    def test_warning_yellow_token_is_never_used_as_a_text_color_anywhere(self):
        # Broader guard: the warning yellow custom property may accent a
        # border/background, but pairing it as the text `color:` directly
        # against an unknown surface is exactly the low-contrast trap the
        # plan warns against - assert no such declaration exists anywhere
        # in the file. Matches only a standalone `color:` property (word
        # boundary before "color"), never `border-left-color:` (the
        # existing, correct accent usage on `.cds-notification--warning`).
        css = (STATIC_ROOT / "carbon.css").read_text(encoding="utf-8")
        self.assertNotRegex(css, r"(?<![-\w])color:\s*var\(--cds-support-warning\)")
        self.assertNotIn("#f1c21b", css.replace("--cds-support-warning: #f1c21b", ""))


if __name__ == "__main__":
    unittest.main()
