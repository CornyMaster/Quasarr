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


def javascript_function_body(source, name):
    """Brace-matching JS function body extractor, mirroring the equivalent
    house helper in tests/test_carbon_templates.py (kept as a local,
    unshared copy by the same convention). Used by the mirrors/category
    modal-anatomy tests below instead of the CarbonConfigJsContractTests
    instance method so it can be called as a bare function.
    """
    for prefix in ("async function ", "function "):
        marker = f"{prefix}{name}("
        if marker in source:
            start = source.index(marker)
            break
    else:
        raise AssertionError(f"No function named {name} found in carbon.js")
    depth = 0
    opening = source.index("{", start)
    for index in range(opening, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[opening : index + 1]
    raise AssertionError(f"Unbalanced braces in {name}")


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

# "FF" is appended last (not alphabetically grouped with the GA../DJ/SJ
# block above) so it is deterministically the LAST rendered row - the
# table-row-design pins below slice from its data-hostname-id to the next
# "</div></div>" boundary, which only lands exactly on that row's own
# closing tag when it is the final child of .cds-hostname-table.
SITES = ["GA", "GB", "GC", "GD", "GE", "GF", "GG", "DJ", "SJ", "FF"]

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
    # "ff" carries no SOURCE_METADATA entry on purpose - a minimal
    # no-capability, status="ok" row for the table-row-design pins.
    "ff": "ff-fixture.invalid",
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

    def _render_hostnames(self, sites=SITES):
        return self._render(sites)


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
        self.assertEqual(html.count('class="cds-hostname-table__row"'), len(SITES))

    def test_renders_more_rows_when_more_sites_configured(self):
        extra_sites = SITES + ["GH"]
        extra_metadata = dict(SOURCE_METADATA)
        extra_metadata["gh"] = SOURCE_METADATA["ga"]
        with mock.patch(
            "quasarr.storage.setup.hostnames.get_source_metadata",
            return_value=extra_metadata,
        ):
            html = self.mod.render_hostnames(_build_shared_state(extra_sites))
        self.assertEqual(
            html.count('class="cds-hostname-table__row"'), len(extra_sites)
        )

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
            "Account required",
            "Invite only",
            "Login required",
            "FlareSolverr",
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
        self.assertIn("Login skipped – open the status to require login again.", html)

    def test_save_form_targets_existing_endpoint_with_unchanged_field_names(self):
        html = self._render()
        self.assertIn(
            '<form id="hostnames-form" action="/api/hostnames" method="post">', html
        )
        self.assertIn('name="hostnames_url"', html)
        for site in SITES:
            with self.subTest(site=site):
                self.assertIn(f'name="{site.lower()}"', html)

    def test_hostname_row_is_table_row_with_status_button(self):
        html = self._render_hostnames()
        self.assertIn('<div class="cds-hostname-table">', html)
        self.assertIn(
            '<div class="cds-hostname-table__head"><span></span><span>Status</span>'
            "<span>Hostname</span><span>Capabilities</span></div>",
            html,
        )
        row_start = html.index('data-hostname-id="ff"')
        row = html[row_start : html.index("</div></div>", row_start)]
        self.assertIn('<span class="cds-hostname-table__code">FF</span>', row)
        self.assertIn(
            'class="cds-status cds-status--success cds-status--dot-only '
            'cds-status--link" title="Working normally" '
            'data-action="hostname-status" data-hostname-id="ff"',
            row,
        )
        self.assertNotIn("Details", row)
        self.assertNotIn("cds-hostname-row__status", html)

    def test_hostname_status_dot_carries_its_label_to_assistive_tech(self):
        """The status cell drops its visible text down to a dot alone (a
        150px column wrapping "Hostname not configured" onto two lines for
        21 of 22 sources), but colour must never be the only carrier of
        that status: the label is still readable on hover (`title`) and by
        a screen reader (a visually-hidden text node inside the control).
        """
        html = self._render_hostnames()
        row_start = html.index('data-hostname-id="ff"')
        row = html[row_start : html.index("</div></div>", row_start)]
        self.assertIn('<span class="cds-visually-hidden">Working normally</span>', row)
        # The label must be wrapped in its own visually-hidden span, never
        # sitting as a bare, sighted-visible text node right after the dot
        # (the pre-change markup: `...aria-hidden="true"></span>Working
        # normally</button>`).
        self.assertNotIn('aria-hidden="true"></span>Working normally<', row)

    def test_capability_chip_colours_follow_the_design(self):
        html = self._render_hostnames()
        self.assertIn('<span class="cds-tag cds-tag--gray">German</span>', html)
        self.assertIn('<span class="cds-tag cds-tag--blue">Movies</span>', html)
        self.assertIn('<span class="cds-tag cds-tag--purple">TV</span>', html)
        self.assertIn('<span class="cds-tag cds-tag--teal">Anime</span>', html)
        self.assertIn('<span class="cds-tag cds-tag--red">Login required</span>', html)
        self.assertIn('<span class="cds-tag cds-tag--red">FlareSolverr</span>', html)
        self.assertIn(
            '<span class="cds-tag cds-tag--red">Account required</span>', html
        )

    def test_import_and_save_actions(self):
        html = self._render_hostnames()
        self.assertIn(
            'class="cds-btn cds-btn--tertiary" type="button" '
            'data-action="hostname-import">Import</button>',
            html,
        )
        self.assertIn(
            'class="cds-btn cds-btn--primary cds-btn--cta" type="submit">'
            "Save hostnames</button>",
            html,
        )
        self.assertIn(
            'class="cds-btn cds-btn--secondary cds-btn--cta" type="button" '
            'data-action="hostname-reset">Cancel</button>',
            html,
        )

    def test_page_renders_the_modal_host_the_submit_interceptor_requires(self):
        """The other half of the first-run regression pin
        (test_hostnames_form_interceptor_never_fires_on_the_setup_wizards_form
        in CarbonConfigJsContractTests, and
        test_setup_wizards_hostnames_form_keeps_a_real_native_submit for the
        setup side): this page's shell (render_carbon_html) must keep
        rendering `#cds-modal`, since the JS interceptor's gate is that
        element's presence in the document, not a URL check.
        """
        html = self._render()
        self.assertIn('id="cds-modal"', html)

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
            f'<span class="cds-tag cds-tag--blue">Movies ({SEARCH_CAT_MOVIES})</span>',
            html,
        )
        self.assertIn(
            f'<span class="cds-tag cds-tag--purple">TV ({SEARCH_CAT_SHOWS})</span>',
            html,
        )

    def test_categories_render_as_a_two_column_grid(self):
        """Design spec: the two category tiles sit side by side instead of
        stacking, matching every other Carbon config page's 2-column grid.
        """
        html = self._render()
        self.assertIn('<div class="cds-grid--2">', html)

    def test_button_hierarchy_matches_design_weights(self):
        """Design spec §2.3: row Edit is ghost, row Delete (custom only) is
        danger-ghost, and both Add actions are tertiary - no filled
        secondary buttons anywhere on this page.
        """
        html = self._render()
        self.assertIn(
            'class="cds-btn cds-btn--ghost" type="button" '
            'data-action="download-category-edit"',
            html,
        )
        self.assertIn(
            'class="cds-btn cds-btn--ghost" type="button" '
            'data-action="search-category-edit"',
            html,
        )
        self.assertIn(
            'class="cds-btn cds-btn--tertiary" type="button" '
            'data-action="download-category-add">',
            html,
        )
        self.assertIn(
            'class="cds-btn cds-btn--tertiary" type="button" '
            'data-action="search-category-add">',
            html,
        )
        self.assertNotIn("cds-btn--secondary", html)

    def test_tile_help_text_and_eyebrow(self):
        html = self._render()
        self.assertIn(
            "Used to organize downloads in JDownloader. Mirror whitelists "
            "apply to the download client.",
            html,
        )
        self.assertIn(
            "Hostname whitelists for Newznab search categories used by the indexer.",
            html,
        )
        self.assertIn('<p class="cds-page-header__eyebrow">Organization</p>', html)

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

        self.assertEqual(html.count('class="cds-hostname-table__row"'), len(sites))
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

    def test_hostnames_page_dialogs_all_carry_an_eyebrow(self):
        """Spec 2.6: the modal head is eyebrow + title. These five
        Hostnames-page dialogs (the shared error modal, Save, Import,
        Require-login-again, and the flaresolverr-next-required gate named
        by acceptance criterion 4) shipped with no eyebrow option at all -
        every other Hostnames dialog (status detail, Restart) already
        carries one.
        """
        for name in (
            "showErrorModal",
            "openHostnamesSaveModal",
            "openHostnameImportModal",
            "openFlareSolverrRequiredModal",
            "openSkipLoginModal",
        ):
            with self.subTest(function=name):
                body = self._function_body(name)
                self.assertIn("eyebrow: 'Hostnames'", body)

    def test_flaresolverr_required_dialog_keeps_its_eyebrow(self):
        """Named explicitly by spec acceptance criterion 4 ("Modal footer
        full-width on every dialog ... FlareSolverr required") - pinned on
        its own so a future refactor of the shared eyebrow text cannot
        silently drop this specific call site.
        """
        body = self._function_body("openFlareSolverrRequiredModal")
        self.assertIn("window.showModal(", body)
        self.assertIn("'flaresolverr-next required'", body)
        self.assertIn("{ eyebrow: 'Hostnames' }", body)

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
        self.assertNotIn("setAttribute('data-details'", body)

    def test_show_modal_supports_title_mono_suffix_option(self):
        """design spec §2.4: a title's hostname suffix renders in Mono.
        `showModal` writes its plain `title` argument via a text node
        (unstyled), so a caller-owned `options.titleMonoSuffix` string gets
        appended as its own `.cds-mono`-classed node instead of being baked
        into the plain-text title - the minimum addition needed, and every
        existing caller (3-argument, or 4-argument without this field)
        keeps getting a plain-text-only title exactly as before.
        """
        body = self._function_body("showModal")
        self.assertIn("titleMonoSuffix", body)
        self.assertIn("suffixEl.className = 'cds-mono'", body)
        self.assertIn(
            "modalTitle.appendChild(document.createTextNode(String(title || 'Dialog')));",
            body,
        )

    def test_status_modal_title_carries_a_mono_hostname_suffix(self):
        """design spec §3 Hostnames: the modal title is the row's code plus
        a "Mono suffix" (`NX · nx.example.invalid`), via showModal's new
        titleMonoSuffix option (checked above) rather than plain-text
        concatenation - the status_title that used to live in the title
        moved into a separate dot+text status line in the body instead
        (checked below).
        """
        body = self._function_body("openHostnameStatusModal")
        self.assertIn("titleMonoSuffix: row.hostname ? ' · ' + row.hostname : ''", body)
        self.assertNotIn("row.label + ' - ' + row.status_title", body)
        self.assertNotIn("row.label + ' · ' + row.hostname", body)

    def test_status_modal_has_dot_text_status_line_and_last_checked_helper(self):
        body = self._function_body("openHostnameStatusModal")
        self.assertIn("buildStatusLine(row)", body)
        self.assertIn("cds-status__dot", self.js)
        self.assertIn("'Last checked: '", body)

    def test_status_modal_eyebrow_is_hostname_status(self):
        body = self._function_body("openHostnameStatusModal")
        self.assertIn("eyebrow: 'Hostname status'", body)

    def test_status_modal_restores_open_hostname_link_in_the_body(self):
        """Design spec §3 requires the modal footer to contain exactly
        Close + "Check & save session", so the "Open <ID>" quick link
        must be placed in the body instead. The link is built at runtime
        from `row.hostname` (never a literal source hostname in this file -
        see the security-carry-in pin above).
        """
        body = self._function_body("openHostnameStatusModal")
        self.assertIn("body.appendChild(openLink)", body)
        self.assertIn(
            "openLink.textContent = 'Open ' + String(row.id).toUpperCase();", body
        )
        # The link is built and appended to `body` before `actions` is
        # assembled - i.e. it lives in the body, not the footer.
        self.assertLess(
            body.index("body.appendChild(openLink)"), body.index("var actions = ")
        )

    def test_status_modal_footer_is_close_and_check_and_save_session_only(self):
        """design spec §3 Hostnames: footer is exactly "Close" (secondary) +
        "Check & save session" (primary, only rendered when credentials are
        supported) - the restored "Open <ID>" link (checked above) lives in
        the body, so the footer's own `actions` string never mentions it.
        """
        body = self._function_body("openHostnameStatusModal")
        self.assertIn('data-action="modal-close">Close</button>', body)
        self.assertIn('data-action="hostname-credentials-check"', body)
        self.assertIn("Check &amp; save session</button>", body)
        self.assertNotIn("link.outerHTML", body)
        actions_start = body.index("var actions = ")
        show_modal_start = body.index("window.showModal(")
        actions_region = body[actions_start:show_modal_start]
        self.assertNotIn("openLink", actions_region)
        self.assertNotIn("'Open '", actions_region)

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

    def test_hostname_filter_matches_both_site_code_and_hostname_value(self):
        body = self._function_body("applyHostnameFilter")
        self.assertIn("data-hostname-id", body)
        self.assertIn("cds-hostname-table__input", body)

    def test_hostname_reset_action_resets_the_form(self):
        start = self.js.index("case 'hostname-reset':")
        end = self.js.index("break;", start)
        segment = self.js[start:end]
        self.assertIn("hostnames-form", segment)
        self.assertIn(".reset()", segment)

    def test_hostname_reset_also_reapplies_the_filter(self):
        """Regression pin: `<form>.reset()` reverts the filter `<input>`'s
        value (it lives inside #hostnames-form) but fires no 'input' event,
        and applyHostnameFilter() only runs on 'input' - without a direct
        call here, rows a stale filter had hidden stayed hidden even though
        the now-empty filter box looked cleared. Must run after reset(),
        not before, so it sees the reverted (empty) value.
        """
        start = self.js.index("case 'hostname-reset':")
        end = self.js.index("break;", start)
        segment = self.js[start:end]
        reset_index = segment.index(".reset()")
        filter_index = segment.index("applyHostnameFilter()")
        self.assertLess(reset_index, filter_index)

    def test_hostname_import_action_renamed_from_hostnames_import_open(self):
        """Scoped to bootstrapCarbonHostnamesAndCategories - the setup
        wizard's own, separate #hostnames-import-url field
        (bootstrapCarbonSetupFlows) is untouched (out of scope: "Do not
        touch the setup wizard's logic") and legitimately keeps that id.
        """
        start = self.js.index("(function bootstrapCarbonHostnamesAndCategories() {")
        end = self.js.index("(function bootstrapCarbonCaptcha() {")
        slice_ = self.js[start:end]
        self.assertIn("case 'hostname-import':", slice_)
        self.assertNotIn("hostnames-import-open", slice_)
        self.assertIn("hostname-import-url", slice_)
        self.assertNotIn("hostnames-import-url", slice_)

    def test_hostnames_form_submit_is_intercepted_before_native_post(self):
        """The Save button is now a real `type="submit"` with no
        data-action (design spec §3), but `submitHostnamesForm()` still has
        to run first to sync the import-URL field and inject the API key
        (see the two regression pins above) - a bare native submit would
        skip both. A `submit` listener on #hostnames-form intercepts the
        native event, preventDefault()s it, and routes through the existing
        confirm-then-submit flow instead.
        """
        self.assertIn("addEventListener('submit'", self.js)
        self.assertIn("event.preventDefault()", self.js)
        self.assertIn("openHostnamesSaveModal()", self.js)

    def test_hostnames_form_interceptor_never_fires_on_the_setup_wizards_form(self):
        """First-run-blocking regression pin: carbon.js is one bundle served
        on both this config page AND the setup wizard's own standalone
        Hostnames step (quasarr/storage/setup/carbon.py), which renders a
        SECOND, unrelated `<form id="hostnames-form">` with a real,
        uninterrupted native `type="submit"`. Before this pin, the
        interceptor matched on the id alone, fired on the setup page too,
        called `preventDefault()`, then `openHostnamesSaveModal()` ->
        `showModal()`, which is a hard no-op there because
        render_carbon_simple_page (the setup wizard's shell) never renders
        `#cds-modal` - so clicking Save on a first run did nothing at all:
        no modal, no POST, no navigation.

        The gate must be the modal host's actual presence
        (`document.getElementById('cds-modal')`), never a URL/page check
        and never the setup form's `data-guard-submit` attribute - an
        incidental marker that could be added to this form too, or dropped
        from that one, for unrelated reasons, silently flipping an
        attribute-based gate without anyone noticing.
        """
        body = self._function_body("onHostnamesFormSubmit")
        self.assertIn("document.getElementById('cds-modal')", body)
        self.assertNotIn("data-guard-submit", body)

    def test_setup_wizards_hostnames_form_keeps_a_real_native_submit(self):
        """The other half of the same regression: the setup wizard's own
        form must still be a plain, guarded, native submit - no
        data-action, so this page's delegated click dispatcher never
        touches it either.
        """
        setup_module = importlib.import_module("quasarr.storage.setup.carbon")
        with (
            mock.patch.object(setup_module, "build_hostname_rows", return_value=[]),
            mock.patch.object(
                setup_module,
                "Config",
                side_effect=_config_factory({"Settings": {"hostnames_url": ""}}),
            ),
            mock.patch.object(
                setup_module, "DataBase", side_effect=_database_factory({}, {})
            ),
        ):
            html = setup_module.render_setup_hostnames(_build_shared_state())
        self.assertIn(
            '<form id="hostnames-form" action="/api/hostnames" method="post" '
            "data-guard-submit></form>",
            html,
        )
        self.assertIn(
            '<button class="cds-btn cds-btn--primary" type="submit" '
            'form="hostnames-form">Save</button>',
            html,
        )
        self.assertNotIn('id="cds-modal"', html)

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

    # -- Mirror row anatomy: name before tag, Mono styling --

    def test_mirror_row_name_renders_in_mono_before_the_recommended_tag(self):
        """Design spec row order is rank, checkbox, hoster (Mono), tag,
        arrows - the hoster name must be its own Mono-styled node and must
        appear before the Recommended tag it sits beside.
        """
        body = javascript_function_body(self.js, "buildMirrorRow")
        self.assertIn("cds-mirror-row__rank", body)
        self.assertLess(body.index("cds-mirror-row__name"), body.index("Recommended"))
        self.assertIn("cds-mono", body)

    def test_mirror_row_move_buttons_render_for_every_row_not_only_checked_ones(self):
        """Regression pin: buildMirrorRow must build the reorder controls
        for EVERY row, not only rows checked at construction time. Gating
        construction itself behind `isChecked` meant a mirror ticked
        mid-session got no arrows until the modal was reopened - a loss of
        function. Whether the arrows are visible is a live, checkbox-
        driven display concern (see the paired CSS and onChange pins
        below), never a build-time branch.
        """
        body = javascript_function_body(self.js, "buildMirrorRow")
        self.assertNotIn("if (isChecked) {", body)
        self.assertIn("cds-mirror-row__move", body)
        self.assertIn("row.appendChild(moveGroup);", body)

    def test_mirror_checkbox_toggle_keeps_the_selected_class_live(self):
        """Paired with the pin above: the arrows' visibility (driven by
        the CSS pair pinned in CarbonConfigCssContractTests below) depends
        on the row's `is-selected` class, so that class must be
        recomputed on every checkbox toggle - not only once when the
        modal opens. onChange already wires this via reflowMirrorRows();
        this pin is what stops that wiring from silently regressing.

        carbon.js defines more than one `onChange` (one per page IIFE), so
        this scopes the extraction to the Hostnames/Categories IIFE the
        same way test_hostname_import_action_renamed_from_hostnames_import_open
        above does, rather than matching the first `onChange` in the file.
        """
        iife_start = self.js.index(
            "(function bootstrapCarbonHostnamesAndCategories() {"
        )
        iife_end = self.js.index("(function bootstrapCarbonCaptcha() {")
        iife_slice = self.js[iife_start:iife_end]
        body = javascript_function_body(iife_slice, "onChange")
        self.assertIn("cds-mirror-row__checkbox", body)
        self.assertIn("reflowMirrorRows()", body)

    # -- Mirrors modal: warn-notification and "Save mirrors" primary action --

    def test_mirrors_modal_carries_a_real_warn_notification(self):
        """The mirrors modal already had eyebrow/wide anatomy; design spec
        §3 Categories additionally requires a real warn-notification
        component in the body, not a bare tinted paragraph.
        """
        body = javascript_function_body(self.js, "openDownloadCategoryEditModal")
        self.assertIn("cds-notification cds-notification--warning", body)
        self.assertIn("cds-notification__message", body)
        self.assertIn("setAttribute('role', 'alert')", body)

    def test_mirrors_modal_primary_action_is_labeled_save_mirrors(self):
        body = javascript_function_body(self.js, "openDownloadCategoryEditModal")
        self.assertIn("eyebrow: 'Download category'", body)
        self.assertIn("wide: true", body)
        self.assertIn(">Save mirrors</button>", body)
        self.assertNotIn(">Save</button>", body)

    # -- Search-sources modal: eyebrow, title, and .cds-pill component --

    def test_search_sources_modal_uses_search_category_eyebrow_and_title(self):
        body = javascript_function_body(self.js, "openSearchCategoryEditModal")
        self.assertIn("'Edit hostnames · ' + name", body)
        self.assertIn("eyebrow: 'Search category'", body)

    def test_source_pill_uses_the_shared_cds_pill_component(self):
        body = javascript_function_body(self.js, "buildSourcePill")
        self.assertIn("cds-pill", body)
        self.assertNotIn("cds-source-pill", body)

    # -- Delete-category modals: confirm-deletion anatomy --

    def test_download_category_delete_modal_uses_confirm_deletion_anatomy(self):
        body = javascript_function_body(self.js, "openDownloadCategoryDeleteModal")
        self.assertIn("eyebrow: 'Confirm deletion'", body)
        self.assertIn("'Delete · ' + name", body)

    def test_search_category_delete_modal_uses_confirm_deletion_anatomy(self):
        """Same modal anatomy as its download-category sibling above - the
        search-category delete dialog must carry it too.
        """
        body = javascript_function_body(self.js, "openSearchCategoryDeleteModal")
        self.assertIn("eyebrow: 'Confirm deletion'", body)
        self.assertIn("'Delete · ' + name", body)


class CarbonConfigCssContractTests(unittest.TestCase):
    def test_new_component_classes_present(self):
        css = (STATIC_ROOT / "carbon.css").read_text(encoding="utf-8")
        for selector in (
            ".cds-hostname-table__row",
            ".cds-hostname-table__caps",
            ".cds-category-row",
            ".cds-mirror-row",
            ".cds-mirror-row__rank",
            ".cds-mirror-row__name",
            ".cds-pill",
        ):
            with self.subTest(selector=selector):
                self.assertIn(selector, css)
        self.assertNotIn("http://", css)
        self.assertNotIn("https://", css)

    def test_pill_selected_state_uses_the_interactive_token_not_button_primary(self):
        """The search-sources pills are a selection component, not a
        primary button - their filled state must key off the shared
        --cds-interactive accent token (identical to --cds-button-primary
        in light theme but distinct in dark theme) rather than borrowing
        the button token.
        """
        css = (STATIC_ROOT / "carbon.css").read_text(encoding="utf-8")
        rule_match = re.search(r"\.cds-pill\.is-selected\s*\{([^}]*)\}", css)
        self.assertIsNotNone(rule_match, "cds-pill.is-selected rule not found")
        rule_body = rule_match.group(1)
        self.assertIn("var(--cds-interactive)", rule_body)
        self.assertNotIn("var(--cds-button-primary)", rule_body)

    def test_mirror_row_move_visibility_is_driven_by_the_live_selected_class(self):
        """Pairs with the buildMirrorRow/onChange pins above: since the
        reorder arrows are now always built, they must be hidden by
        default and shown only through the row's live `is-selected`
        class - never through a build-time branch.
        """
        css = (STATIC_ROOT / "carbon.css").read_text(encoding="utf-8")
        move_rule = re.search(r"\.cds-mirror-row__move\s*\{([^}]*)\}", css)
        self.assertIsNotNone(move_rule, "cds-mirror-row__move rule not found")
        self.assertIn("visibility: hidden", move_rule.group(1))
        selected_rule = re.search(
            r"\.cds-mirror-row\.is-selected \.cds-mirror-row__move\s*\{([^}]*)\}", css
        )
        self.assertIsNotNone(
            selected_rule,
            "cds-mirror-row.is-selected .cds-mirror-row__move rule not found",
        )
        self.assertIn("visibility: visible", selected_rule.group(1))

    def test_narrow_viewport_row_min_width_is_scoped_to_the_scroll_container(self):
        """Regression pin: a bare `.cds-hostname-table__row { min-width:
        760px }` selector at <=1056px applies to ANY row carrying that
        class, forcing the width even where there is no
        `.cds-hostname-table` ancestor to turn into a horizontal scroll
        container (`overflow-x: auto` is scoped to that ancestor) - exactly
        what overflowed the setup wizard's narrower card before it gained
        its own `.cds-hostname-table` wrapper. The selector must be a
        descendant combinator so the rule cannot apply without that
        ancestor, wrapper or no wrapper.
        """
        css = (STATIC_ROOT / "carbon.css").read_text(encoding="utf-8")
        self.assertIn(
            ".cds-hostname-table .cds-hostname-table__head,\n"
            "\t.cds-hostname-table .cds-hostname-table__row { min-width: 760px; }",
            css,
        )
        self.assertNotIn(
            "\t.cds-hostname-table__head,\n\t.cds-hostname-table__row { min-width: 760px; }",
            css,
        )

    def test_hostname_table_columns_are_separated_by_a_gap(self):
        """Regression pin: with a 48px status column and zero column-gap,
        the status header text sat flush against the hostname header text
        (measured live: "Status" ends and "Hostname" starts at the exact
        same x-coordinate), reading as one run-on label instead of two
        column headings. `.cds-hostname-table__head` and
        `.cds-hostname-table__row` share one selector on purpose - both
        must carry the identical `grid-template-columns`/`column-gap` pair
        or the header stops lining up with the columns below it.
        """
        css = (STATIC_ROOT / "carbon.css").read_text(encoding="utf-8")
        self.assertRegex(
            css,
            r"\.cds-hostname-table__head,\n\.cds-hostname-table__row \{"
            r"[^}]*grid-template-columns:\s*"
            r"56px 48px minmax\(200px, 1\.2fr\) minmax\(260px, 1\.6fr\);"
            r"[^}]*column-gap:\s*16px;",
        )

    def test_skip_banner_never_uses_warning_yellow_as_text_color(self):
        """Warning yellow must always be readable against the plain row
        background the note now sits on directly (the dense table row has
        no tinted box around it, unlike the old flex-row skip banner). The
        original `.cds-hostname-row__skip-banner` set
        `color: var(--cds-support-warning)` directly on the page background
        - measured contrast was 1.68:1 (white/light layer) and 1.53:1
        (light bg), both far below WCAG AA's 4.5:1 for normal text. This
        pins the replacement `.cds-hostname-table__note` to the separate
        `--cds-warning-text` token instead (the spec-mandated `#b28600` in
        light theme) - a guard that the known-bad raw `--cds-support-warning`
        literal never comes back as a bare text color here, not a fresh
        contrast measurement of this specific rule (see the broader
        no-raw-literal guard below).
        """
        css = (STATIC_ROOT / "carbon.css").read_text(encoding="utf-8")
        rule_match = re.search(r"\.cds-hostname-table__note\s*\{([^}]*)\}", css)
        self.assertIsNotNone(rule_match, "skip-banner rule not found")
        rule_body = rule_match.group(1)
        self.assertNotIn("color: var(--cds-support-warning)", rule_body)
        self.assertIn("color: var(--cds-warning-text)", rule_body)

    def test_warning_yellow_token_is_never_used_as_a_text_color_anywhere(self):
        # Broader guard: the warning yellow custom property may accent a
        # border/background, but pairing it as the text `color:` directly
        # against an unknown surface is exactly the low-contrast trap the
        # plan warns against - assert no such declaration exists anywhere
        # in the file. Matches only a standalone `color:` property (word
        # boundary before "color"), never `border-left-color:` (the
        # existing, correct accent usage on `.cds-notification--warning`).
        #
        # Two token DEFINITIONS are exempt from the raw-literal half of the
        # guard, and only their definitions: `--cds-support-warning` (the
        # accent) and `--cds-warning-text` (the separate, theme-specific
        # TEXT token that exists precisely to keep warning-coloured text
        # readable - #b28600 on light, #f1c21b on dark, per the design).
        # Every other occurrence of the raw literal stays forbidden, and
        # the `color: var(--cds-support-warning)` guard above is unchanged.
        css = (STATIC_ROOT / "carbon.css").read_text(encoding="utf-8")
        self.assertNotRegex(css, r"(?<![-\w])color:\s*var\(--cds-support-warning\)")
        self.assertNotIn(
            "#f1c21b",
            css.replace("--cds-support-warning: #f1c21b", "").replace(
                "--cds-warning-text: #f1c21b", ""
            ),
        )


if __name__ == "__main__":
    unittest.main()
