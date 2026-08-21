# -*- coding: utf-8 -*-

"""Contracts for the Carbon Downloads page (`api.packages.carbon.render_downloads`)
and its `carbon.js`/`carbon.css` companions.

Covers:
  - A1: `render_downloads()` ships the search toolbar OUTSIDE the refreshed
    `#downloads-content` subtree, plus a loading skeleton for the deferred
    table (selection/release/state/evidence/next check/sweep
    progress/actions columns, bulk toolbar disabled at zero selection),
    Queue, History, and a collapsible Other-packages section. It never
    touches JDownloader or the database device path itself - every row is
    built client-side in carbon.js from `GET /api/packages/list` (the shared
    packages-list data contract), the same house pattern
    `api.carbon._dashboard_queue_tile()` established.
  - The shared countdown parser/updater (`deferredCountdownEpoch`) and its
    reuse for the Statistics page's `<time data-epoch>` upgrade
    (`upgradeEpochTimes`), both pinned via static analysis of the shipped
    carbon.js text (no JS engine in this suite).
  - D1 wording: ordinary queue/history deletion still says "Delete package
    and files" and names disk deletion/irreversibility; deferred pending-row
    removal uses the distinct "Remove pending package" wording with no
    `records_only` anywhere.
  - Selection snapshot/restore by `Set.has(checkbox.value)`, never a selector
    built from package data.
  - The client-side row design: a status dot (not a text column) plus a
    category tag and an announced progress bar in Queue rows, a
    Completed/Failed tag leading each History row, a state dot and a sweep
    bar in Deferred rows, and the Dashboard queue preview built from the
    same vocabulary - all pinned against the shipped carbon.js text and the
    table heads those rows line up with.
  - A blacklist-scrub simulation (c294f65) against the real
    `GET /api/packages/list` route with a real `CrypterCooldownService` and
    an injected clock, proving the exact JSON carbon.js consumes keeps
    alternatives without leaking a removed fingerprint and reports a
    sole-link failure once with the fixed reason text.
"""

import io
import json
import re
import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest import mock

from bottle import Bottle

import quasarr.api.packages as packages_api
import quasarr.api.packages.carbon as carbon
from quasarr.providers.crypter_cooldowns import CrypterCooldownService

PACKAGE_A = "Quasarr_movies_" + "a" * 32
PACKAGE_B = "Quasarr_movies_" + "b" * 32
PACKAGE_C = "Quasarr_movies_" + "c" * 32
NOW = 1_700_000_000


class FakeClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def protected_blob(title="Synthetic Release", links=None, password="secret-pw"):
    return json.dumps(
        {
            "title": title,
            "links": links or [["https://filecrypt.invalid/c/1", "filecrypt"]],
            "password": password,
            "size_mb": 700,
        }
    )


def failed_blob(title="Synthetic Failure", reason="Synthetic download error"):
    return json.dumps({"title": title, "error": reason})


class MemoryDatabase:
    """Minimal in-memory DataBase double sharing one `tables` dict per
    shared_state, matching test_carbon_downloads_contract.py's fixture -
    reimplemented locally per tests/AGENTS.md (no shared test-helpers
    module).
    """

    def __init__(self, tables=None):
        self.rows = {}
        self.tables = {} if tables is None else tables

    def _peer(self, table):
        if table not in self.tables:
            self.tables[table] = MemoryDatabase(tables=self.tables)
        return self.tables[table]

    def retrieve(self, key):
        return self.rows.get(key)

    def retrieve_all_titles(self):
        items = [[key, value] for key, value in sorted(self.rows.items())]
        return items if items else None

    def mutate_value(self, key, mutator):
        value = mutator(self.rows.get(key))
        if value is None:
            self.rows.pop(key, None)
        else:
            self.rows[key] = value
        return value

    def mutate_values(self, targets, mutator):
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

    def delete(self, key):
        self.rows.pop(key, None)
        return True

    def delete_exact(self, key, value):
        if self.rows.get(key) != value:
            return False
        self.rows.pop(key)
        return True


class RaisingSharedState:
    """A device-free shared_state double: `get_device()` always raises, so
    `render_downloads()` touching it would fail this suite immediately.
    """

    def __init__(self, protected_rows=(), failed_rows=(), block_mode="defer"):
        self.values = {"crypter_block_mode": block_mode}
        self.databases = {}
        self.databases["protected"] = MemoryDatabase(tables=self.databases)
        self.databases["failed"] = MemoryDatabase(tables=self.databases)
        for package_id, blob in protected_rows:
            self.databases["protected"].rows[package_id] = blob
        for package_id, blob in failed_rows:
            self.databases["failed"].rows[package_id] = blob

    def get_db(self, table):
        if table not in self.databases:
            self.databases[table] = MemoryDatabase(tables=self.databases)
        return self.databases[table]

    def get_device(self):
        raise AssertionError("render_downloads() must never call get_device()")


class NoopFakeDevice:
    """A JDownloader device double whose linkgrabber/downloader/extraction
    calls are all no-ops - enough for `get_packages_for_device()` to walk an
    empty inventory and project only the protected/failed DB rows a test
    seeds directly, matching test_carbon_downloads_contract.py's
    `RecordingFakeDevice()` shape (reimplemented locally per tests/AGENTS.md).
    """

    def __init__(self):
        self.linkgrabber = SimpleNamespace(
            query_packages=lambda: [],
            query_links=lambda: [],
            is_collecting=lambda: False,
            cleanup=lambda *a, **k: None,
            remove_links=lambda *a, **k: None,
            move_to_downloadlist=lambda *a, **k: None,
        )
        self.downloads = SimpleNamespace(
            query_packages=lambda: [],
            query_links=lambda: [],
            remove_links=lambda *a, **k: None,
            cleanup=lambda *a, **k: None,
        )
        self.extraction = SimpleNamespace(get_archive_info=lambda *a, **k: [])


class FakeProtectedDatabaseTable:
    """Fake for `shared_state.values['database']("protected")` -
    `protected_captcha_count()`'s only read - independent of MemoryDatabase's
    `get_db()` used by the package projection.
    """

    def __init__(self, titles):
        self._titles = titles

    def retrieve_all_titles(self):
        return self._titles


def _read_static(filename):
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    return root.joinpath("quasarr", "static", filename).read_text(encoding="utf-8")


def _read_carbon_py_source():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    return root.joinpath("quasarr", "api", "packages", "carbon.py").read_text(
        encoding="utf-8"
    )


def javascript_function_body(source, name):
    start = source.index(f"function {name}(")
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[opening + 1 : index]
    raise AssertionError(f"unbalanced braces in {name}")


# carbon.js ships ONE shared script for every Carbon page (Dashboard,
# Settings, Hostnames/Categories, Captcha, status pages, Statistics-adjacent
# CarbonTime, and Downloads all live in one file, each as its own top-level
# IIFE). A whole-file scan for a token this page's OWN rows must never carry
# (e.g. the packages-list `.hostname` prohibition on package rows) is a false
# positive once another page's IIFE legitimately uses that same token for
# its own, unrelated data - the Hostnames/Categories
# `bootstrapCarbonHostnamesAndCategories` IIFE reads `.hostname` all over its
# (correct, in-scope) hostname rows. Any
# assertion that checks the ABSENCE of a token that another page could
# plausibly also use for its own legitimate purpose must be scoped to the
# relevant IIFE slice instead of `cls.js`/`self.js` (the whole file);
# assertions that only need to find ONE specific, uniquely-named function
# (`javascript_function_body`) or that check PRESENCE of a literal are safe
# on the whole file regardless, since there is no plausible second page
# defining an identically-named function or duplicating an unrelated,
# specific literal.
DOWNLOADS_IIFE_MARKER = "(function bootstrapCarbonDownloads() {"
CARBON_TIME_IIFE_MARKER = "(function bootstrapCarbonTime() {"


def _iife_slice_to_eof(js, marker):
    """`js` from `marker` onward. Both `bootstrapCarbonDownloads` (the
    Downloads page's own IIFE) and `bootstrapCarbonTime` (the shared
    countdown/time module immediately before it, with no other IIFE ever
    sitting between them) are the LAST IIFE(s) carbon.js ships, so slicing
    to end-of-file is exact - not an approximation - as long as the marker
    is still unique. Asserting that keeps this slice honest if a future
    change reorders or duplicates the IIFE.
    """
    count = js.count(marker)
    if count != 1:
        raise AssertionError(
            f"expected exactly one {marker!r} marker in carbon.js, found {count} "
            "- the Downloads-only slice this helper builds is no longer safe"
        )
    return js[js.index(marker) :]


def _downloads_iife_source(js):
    """Only `bootstrapCarbonDownloads` - for assertions about Downloads'
    OWN row/action code specifically (e.g. the packages-list forbidden-field
    list), not the shared countdown module it calls into.
    """
    return _iife_slice_to_eof(js, DOWNLOADS_IIFE_MARKER)


def _carbon_time_and_downloads_iife_source(js):
    """`bootstrapCarbonTime` (the shared countdown/time module) plus
    `bootstrapCarbonDownloads` - for assertions about the countdown
    mechanism's own closed dataset-attribute set, which spans both IIFEs
    (the module defines the read, Downloads is its only current consumer)
    but must not be read as a claim about every OTHER page in the file too.
    """
    return _iife_slice_to_eof(js, CARBON_TIME_IIFE_MARKER)


# ---------------------------------------------------------------------------
# 1. render_downloads() skeleton
# ---------------------------------------------------------------------------


class DownloadsSkeletonRenderTests(unittest.TestCase):
    def _shared_state(self, titles=()):
        shared_state = RaisingSharedState()
        shared_state.values["database"] = lambda table: FakeProtectedDatabaseTable(
            list(titles)
        )
        return shared_state

    def _render(self, query_string="", titles=()):
        shared_state = self._shared_state(titles)
        app = Bottle()

        @app.get("/packages")
        def route():
            return carbon.render_downloads(shared_state)

        environ = {
            "REQUEST_METHOD": "GET",
            "PATH_INFO": "/packages",
            "QUERY_STRING": query_string,
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
        return body.decode("utf-8")

    def test_render_downloads_exists(self):
        self.assertTrue(callable(carbon.render_downloads))

    def test_active_page_is_downloads(self):
        html = self._render()
        self.assertIn('href="/packages" aria-current="page"', html)
        self.assertIn("<title>Downloads</title>", html)

    def test_never_touches_jdownloader_device(self):
        # RaisingSharedState.get_device() raises; no exception means
        # render_downloads() never called it.
        self._render()

    def test_search_input_lives_in_the_queue_tile_head(self):
        html = self._render()
        head_start = html.index('id="downloads-queue-section"')
        head_end = html.index('id="queue-table"', head_start)
        head_html = html[head_start:head_end]
        self.assertIn('class="cds-tile__head-row"', head_html)
        self.assertIn('id="queue-count"', head_html)
        self.assertIn('id="downloads-search"', head_html)
        self.assertIn('placeholder="Search releases"', head_html)

    def test_search_input_keeps_an_accessible_name(self):
        # The head row shows no visible field label (the placeholder is not
        # one), so the <label> stays in the accessibility tree only.
        html = self._render()
        start = html.index('for="downloads-search"')
        label_open = html.rindex("<label", 0, start)
        self.assertIn("cds-visually-hidden", html[label_open:start])

    def test_every_table_has_its_own_search_field(self):
        html = self._render()
        for field_id in (
            "deferred-search",
            "downloads-search",
            "history-search",
            "other-search",
        ):
            with self.subTest(field_id=field_id):
                self.assertIn(f'id="{field_id}"', html)
                # Every field keeps its label out of sight but in the tree.
                start = html.index(f'for="{field_id}"')
                label_open = html.rindex("<label", 0, start)
                self.assertIn("cds-visually-hidden", html[label_open:start])

    def test_sortable_heads_declare_a_key_and_start_unsorted(self):
        html = self._render()
        for sort_key in (
            "name",
            "crypter",
            "category",
            "size",
            "eta",
            "progress",
            "added",
            "status",
            "state",
            "evidence",
            "next-check",
            "sweep",
        ):
            with self.subTest(sort_key=sort_key):
                self.assertIn(f'data-sort-key="{sort_key}"', html)
        self.assertIn('aria-sort="none"', html)
        # Nothing may ship pre-sorted in the markup: carbon.js owns the live
        # state and rewrites aria-sort on every render.
        for rendered in ('aria-sort="ascending"', 'aria-sort="descending"'):
            self.assertNotIn(rendered, html)

    def test_sort_controls_are_real_buttons_for_keyboard_use(self):
        html = self._render()
        self.assertIn('class="cds-table__sort"', html)
        self.assertIn('data-action="table-sort"', html)
        # A clickable <th> alone is not reachable by keyboard, so every
        # sortable head must carry a button.
        head_count = html.count('data-sort-key="')
        button_count = html.count('data-action="table-sort"')
        self.assertEqual(head_count, button_count * 2)

    def test_every_table_declares_the_key_carbon_js_keeps_its_state_under(self):
        html = self._render()
        for table_key in (
            "deferred",
            "queue",
            "history",
            "other-queue",
            "other-history",
        ):
            with self.subTest(table_key=table_key):
                self.assertIn(f'data-table-key="{table_key}"', html)

    def test_queue_and_history_gained_the_crypter_and_added_columns(self):
        html = self._render()
        self.assertIn(">Crypter", html)
        self.assertIn(">Added", html)

    def test_the_deferred_release_column_stays_second_for_mobile_pinning(self):
        # .cds-table--sticky-col pins th:first-child and th:nth-child(2) below
        # 672px; inserting Crypter before Release would pin the wrong column.
        html = self._render()
        head_start = html.index('id="deferred-table"')
        head_end = html.index("</thead>", head_start)
        head_html = html[head_start:head_end]
        self.assertLess(
            head_html.index("deferred-select-all"), head_html.index(">Release")
        )
        self.assertLess(head_html.index(">Release"), head_html.index(">Crypter"))

    def test_all_four_sections_and_bodies_are_present(self):
        html = self._render()
        for element_id in (
            "downloads-deferred-section",
            "downloads-queue-section",
            "downloads-history-section",
            "downloads-other-section",
            "deferred-table-body",
            "queue-table-body",
            "history-table-body",
            "other-queue-table-body",
            "other-history-table-body",
        ):
            with self.subTest(element_id=element_id):
                self.assertIn(f'id="{element_id}"', html)

    def test_sections_share_the_stack_rhythm(self):
        """Regression pin: the deferred/queue/history/other sections used
        to be concatenated with nothing between them - .cds-tile carries no
        margin of its own, so the three sections ran together as one
        undifferentiated block. #downloads-content now carries the shared
        .cds-stack class (the same 16px rhythm every other Carbon page uses
        between tiles).
        """
        html = self._render()
        self.assertIn('id="downloads-content" class="cds-stack"', html)

    def test_table_captions_are_visually_hidden_not_deleted(self):
        """Regression pin: each of the deferred/queue/history sections
        rendered a visible <h2> heading AND a <caption> saying nearly the
        same thing ("Deferred linkcrypter checks" appeared verbatim twice,
        "Queue (n)" was followed by "Active downloads", "History" by
        "Recent history"), reading as a duplicated label. The captions
        exist for assistive technology and must survive - they are now
        visually hidden via the existing .cds-visually-hidden utility
        instead of deleted, so a screen reader still gets a table caption
        while sighted users stop seeing the label twice.
        """
        html = self._render()
        for caption_text in (
            "Deferred linkcrypter checks",
            "Active downloads",
            "Recent history",
        ):
            with self.subTest(caption=caption_text):
                self.assertIn(
                    f'<caption class="cds-visually-hidden">{caption_text}</caption>',
                    html,
                )
        # The visible section headings are untouched - the fix hides the
        # caption, not the heading.
        self.assertIn(
            '<h2 class="cds-tile__heading">Deferred linkcrypter checks</h2>', html
        )
        self.assertIn('<h2 class="cds-tile__heading">History</h2>', html)

    def test_deferred_table_columns_match_a1(self):
        html = self._render()
        table_start = html.index('id="deferred-table"')
        thead_end = html.index("</thead>", table_start)
        head_html = html[table_start:thead_end]
        headers = re.findall(r"<th[^>]*>(.*?)</th>", head_html, re.S)
        # First header is the select-all checkbox cell (selection column);
        # the last is the action column, unlabelled like the Queue and
        # History tables' own action columns - the row's buttons name
        # themselves, so a repeated column title only adds noise. Every
        # header between them is a sort button, so the label is the text
        # inside the markup rather than the cell's whole content.
        labels = [re.sub(r"<[^>]*>", "", header).strip() for header in headers]
        self.assertEqual(len(headers), 9)
        self.assertIn('id="deferred-select-all"', headers[0])
        self.assertEqual(
            [
                "Release",
                "Crypter",
                "State",
                "Evidence",
                "Next check",
                "Sweep progress",
                "Added",
                "",
            ],
            labels[1:],
        )

    def test_bulk_toolbar_disabled_at_zero_selection(self):
        html = self._render()
        probe_button = html[
            html.index('data-action="deferred-probe-selected"') - 20 : html.index(
                'data-action="deferred-probe-selected"'
            )
            + 200
        ]
        remove_button = html[
            html.index('data-action="deferred-remove-selected"') - 20 : html.index(
                'data-action="deferred-remove-selected"'
            )
            + 200
        ]
        self.assertIn("disabled", probe_button)
        self.assertIn("disabled", remove_button)

    def test_structural_guards_pass(self):
        from quasarr.providers.carbon_templates import _assert_structural_guards

        html = self._render()
        _assert_structural_guards(html)

    def test_no_forbidden_identifiers_or_remote_resources(self):
        html = self._render()
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        self.assertIsNone(re.search(r"\b[0-9a-f]{32}\b", html))
        self.assertIsNone(re.search(r"\b[0-9a-f]{64}\b", html))

    def test_captcha_count_reflected_in_header_badge(self):
        html = self._render(titles=[("id-1", "t"), ("id-2", "t")])
        self.assertIn(
            '<span class="cds-header__badge" aria-hidden="true">2</span>', html
        )
        self.assertIn('aria-label="Notifications, 2 CAPTCHA items"', html)

    def test_deleted_success_banner_renders_when_query_param_present(self):
        html = self._render(query_string="deleted=1")
        self.assertIn("Package deleted successfully.", html)

    def test_deleted_failure_banner_renders_when_query_param_present(self):
        html = self._render(query_string="deleted=0")
        self.assertIn("Failed to delete package.", html)

    def test_no_status_banner_when_query_param_absent(self):
        html = self._render()
        self.assertNotIn("Package deleted successfully.", html)
        self.assertNotIn("Failed to delete package.", html)

    def test_bulk_toolbar_buttons_are_icon_buttons_with_svg(self):
        # A1's actions are icon buttons, not plain text
        # buttons - the bulk toolbar (server-rendered) carries a real
        # render_icon() SVG (renew/trash-can) alongside its visible label.
        html = self._render()
        probe_start = html.index('data-action="deferred-probe-selected"')
        probe_end = html.index("</button>", probe_start)
        probe_html = html[probe_start:probe_end]
        self.assertIn("<svg", probe_html)
        self.assertIn("Check selected", probe_html)

        remove_start = html.index('data-action="deferred-remove-selected"')
        remove_end = html.index("</button>", remove_start)
        remove_html = html[remove_start:remove_end]
        self.assertIn("<svg", remove_html)
        self.assertIn("Remove selected", remove_html)

    def test_bulk_toolbar_aria_label_contains_the_visible_text(self):
        # WCAG 2.5.3 (label-in-name): the accessible name
        # (aria-label, since it overrides descendant text for buttons that
        # have one) must CONTAIN the visible label text as a substring - the
        # original "Check the selected..."/"Remove the selected..." wording
        # broke that by inserting "the" between the two visible words.
        html = self._render()
        for action, visible_text in (
            ("deferred-probe-selected", "Check selected"),
            ("deferred-remove-selected", "Remove selected"),
        ):
            with self.subTest(action=action):
                start = html.index(f'data-action="{action}"')
                end = html.index(">", start)
                tag_html = html[start:end]
                match = re.search(r'aria-label="([^"]*)"', tag_html)
                self.assertIsNotNone(match)
                self.assertIn(visible_text, match.group(1))


# ---------------------------------------------------------------------------
# 2. carbon.js structural contracts
# ---------------------------------------------------------------------------


class DownloadsCarbonJsStructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = _read_static("carbon.js")
        # Only for assertions that check the ABSENCE of a token another
        # page's IIFE could legitimately also use - see the module-level
        # comment above `_downloads_iife_source`.
        cls.downloads_js = _downloads_iife_source(cls.js)

    def test_key_functions_are_shipped(self):
        for name in (
            "deferredCountdownEpoch",
            "updateDeferredCountdowns",
            "upgradeEpochTimes",
            "selectedDeferredPackageIds",
            "restoreDeferredSelection",
            "loadDownloads",
            "runDeferredAction",
            "confirmRemovePending",
            "confirmDeletePackage",
            "applySearchFilter",
        ):
            with self.subTest(function=name):
                self.assertIn(f"function {name}(", self.js)

    def test_deferred_actions_reuse_existing_packages_api_routes(self):
        self.assertIn("'/api/packages/deferred/probe'", self.js)
        self.assertIn("'/api/packages/deferred'", self.js)
        self.assertIn("'/api/packages/list'", self.js)
        self.assertIn("method: 'POST'", self.js)
        self.assertIn("method: 'DELETE'", self.js)

    def test_ordinary_delete_navigates_to_the_existing_redirect_route(self):
        self.assertIn("'/packages/delete/'", self.js)

    def test_no_records_only_anywhere(self):
        # D1's "no records_only" is a design constraint on THIS page's
        # deferred-deletion semantics, not a file-global invariant - scoped
        # defensively even though no other IIFE plausibly needs this literal.
        self.assertNotIn("records_only", self.downloads_js)

    def test_forbidden_row_fields_are_never_accessed(self):
        # Scoped to bootstrapCarbonDownloads only: `.hostname` (among
        # others) is a forbidden field on THIS page's /api/packages/list row
        # objects (the packages-list contract), but the
        # bootstrapCarbonHostnamesAndCategories IIFE legitimately reads
        # `.hostname` all over its own, unrelated hostname rows - a
        # whole-file scan for this token is a false positive once that IIFE
        # exists (see this file's notes on the marker-to-EOF slicing
        # contract for the concrete collision this fixed).
        forbidden = (
            ".sweep_id",
            ".offer_id",
            ".link_fingerprint",
            ".operation_id",
            ".terminal_operation_id",
            ".hostname",
            "row.password",
            "row.url",
            "row.urls",
        )
        for token in forbidden:
            with self.subTest(token=token):
                self.assertNotIn(token, self.downloads_js)


# ---------------------------------------------------------------------------
# 3. Shared countdown parser + selection restore contracts (static analysis,
#    no JS engine - mirrors tests/test_deferred_packages_api.py's technique)
# ---------------------------------------------------------------------------

COUNTDOWN_EPOCH_FALLBACK = re.compile(
    r"([A-Za-z_$][\w$]*)\.dataset\.cohortDeadlineEpoch\s*\?\?\s*"
    r"\1\.dataset\.retryAfterEpoch"
)
DATASET_EPOCH_READ = re.compile(r"\.dataset\.(\w*Epoch)\b")


class DownloadsCountdownAndSelectionContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = _read_static("carbon.js")
        # deferredCountdownEpoch()/updateDeferredCountdowns()/
        # upgradeEpochTimes() all live in the shared bootstrapCarbonTime
        # IIFE, immediately before bootstrapCarbonDownloads (its only
        # current consumer) with no other IIFE between them - this slice
        # covers both. See the module-level comment above
        # `_carbon_time_and_downloads_iife_source`.
        cls.countdown_js = _carbon_time_and_downloads_iife_source(cls.js)

    def test_exactly_one_shared_countdown_fallback_read(self):
        # Kept file-global deliberately: this is a claim about the WHOLE
        # shipped script, not just this page - there must never be a second,
        # independently reimplemented, competing countdown-epoch fallback
        # anywhere in carbon.js. The exact backreferenced pattern
        # (`X.dataset.cohortDeadlineEpoch ?? X.dataset.retryAfterEpoch`) is
        # specific enough that no unrelated page could plausibly duplicate it
        # by coincidence for its own purpose (unlike a generic token such as
        # `.hostname`), so this is the same category as "no remote URLs" -
        # a genuinely file-global invariant, not a Downloads-row-data rule.
        fallbacks = list(COUNTDOWN_EPOCH_FALLBACK.finditer(self.js))
        self.assertEqual(
            1,
            len(fallbacks),
            "want exactly one `<el>.dataset.cohortDeadlineEpoch ?? "
            "<el>.dataset.retryAfterEpoch` read, shared by both attributes",
        )

    def test_only_the_two_documented_epoch_dataset_names_are_read(self):
        # Scoped to the countdown module + Downloads: unlike the fallback
        # pattern above, a bare `.dataset.<word>Epoch` read is a plausible,
        # generic shape another unrelated page could legitimately introduce
        # for its own feature (e.g. a future page's own deadline countdown)
        # without that being any kind of violation of THIS countdown
        # mechanism's closed two-name set - the same collision risk the
        # `.hostname` scan hit, so scoped the same way.
        epochs = sorted(set(DATASET_EPOCH_READ.findall(self.countdown_js)))
        self.assertEqual(["cohortDeadlineEpoch", "retryAfterEpoch"], epochs)

    def test_update_deferred_countdowns_delegates_to_the_shared_parser(self):
        body = javascript_function_body(self.js, "updateDeferredCountdowns")
        self.assertIn("deferredCountdownEpoch(", body)
        self.assertNotIn(".dataset.", body)

    def test_countdown_fallback_belongs_to_deferred_countdown_epoch(self):
        body = javascript_function_body(self.js, "deferredCountdownEpoch")
        match = COUNTDOWN_EPOCH_FALLBACK.search(body)
        self.assertIsNotNone(
            match, "the shared fallback must live inside deferredCountdownEpoch"
        )

    def test_upgrade_epoch_times_never_touches_the_deferred_countdown_pair(self):
        body = javascript_function_body(self.js, "upgradeEpochTimes")
        self.assertNotIn("retryAfterEpoch", body)
        self.assertNotIn("cohortDeadlineEpoch", body)
        self.assertIn("data-epoch", body)
        self.assertIn("deferred-countdown", body)

    def test_selection_snapshot_reads_checkbox_value_only(self):
        body = javascript_function_body(self.js, "selectedDeferredPackageIds")
        self.assertIn("checkbox.value", body)
        self.assertIn(".deferred-select:checked", body)

    def test_restore_builds_a_set_from_its_single_parameter(self):
        signature = re.search(r"function restoreDeferredSelection\(([^)]*)\)", self.js)
        self.assertIsNotNone(signature)
        parameters = [p.strip() for p in signature.group(1).split(",") if p.strip()]
        self.assertEqual(1, len(parameters))
        body = javascript_function_body(self.js, "restoreDeferredSelection")
        self.assertIn(f"new Set({parameters[0]})", body)

    def test_restore_guards_checked_assignment_on_set_membership(self):
        body = javascript_function_body(self.js, "restoreDeferredSelection")
        self.assertIn("selected.has(checkbox.value)", body)
        self.assertIn("checkbox.checked = true", body)
        # Never a selector built from a package value - only quoted literals.
        self.assertNotIn("querySelector('[value=", body)
        self.assertNotIn('querySelector("[value=', body)

    def test_restore_never_reads_a_second_snapshot(self):
        body = javascript_function_body(self.js, "restoreDeferredSelection")
        self.assertNotIn("selectedDeferredPackageIds(", body)


# ---------------------------------------------------------------------------
# 4. D1 wording
# ---------------------------------------------------------------------------


class DownloadsD1WordingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = _read_static("carbon.js")
        cls.downloads_js = _downloads_iife_source(cls.js)

    def test_ordinary_delete_names_disk_and_irreversibility(self):
        body = javascript_function_body(self.js, "confirmDeletePackage")
        self.assertIn("Delete package and files", body)
        # The modal uses a warning notification to communicate file
        # deletion consequences - specifically "downloaded files" - and
        # explicitly states the action cannot be undone.
        self.assertIn("downloaded files", body)
        self.assertIn("cannot be undone", body)

    def test_ordinary_delete_uses_danger_button_and_warning_notification(self):
        """The delete button must use the solid danger style
        (`cds-btn--danger`, not the ghost variant), and the modal body
        must include a warning notification explicitly stating that files
        are deleted along with the package.
        """
        body = javascript_function_body(self.js, "confirmDeletePackage")
        self.assertIn('class="cds-btn cds-btn--danger"', body)
        self.assertIn("Delete package and files</button>", body)
        self.assertIn("cds-notification cds-notification--warning", body)
        self.assertIn("Files are deleted too.", body)

    def test_deferred_removal_uses_distinct_pending_wording(self):
        body = javascript_function_body(self.js, "confirmRemovePending")
        self.assertIn("Remove pending package", body)
        self.assertNotIn("Delete package and files", body)
        self.assertNotIn("cannot be undone", body)

    def test_no_records_only_wording_anywhere(self):
        # D1's design constraint, scoped to this page - see the identical
        # reasoning on DownloadsCarbonJsStructureTests.test_no_records_only_anywhere.
        self.assertNotIn("records_only", self.downloads_js)
        self.assertNotIn("records only", self.downloads_js)


# ---------------------------------------------------------------------------
# 5. No dead "retest" badge
# ---------------------------------------------------------------------------


class NoDeadRetestBadgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = _read_static("carbon.js")

    def test_deferred_state_label_map_has_no_retest_key(self):
        body = self.js[
            self.js.index("DEFERRED_STATE_LABELS = {") : self.js.index(
                "};", self.js.index("DEFERRED_STATE_LABELS = {")
            )
        ]
        self.assertNotIn("retest:", body)
        for expected in ("observing:", "cooldown:", "probe_queued:"):
            self.assertIn(expected, body)

    def test_unknown_state_falls_back_safely_instead_of_a_dead_badge(self):
        body = javascript_function_body(self.js, "buildDeferredRow")
        self.assertIn("DEFERRED_STATE_LABELS[row.state] ||", body)


# ---------------------------------------------------------------------------
# 6. Blacklist-scrub simulation (c294f65) through the real route
# ---------------------------------------------------------------------------


class BlacklistScrubSimulationTests(unittest.TestCase):
    """Reuses the lifecycle test pattern from test_carbon_downloads_contract.py
    (a real CrypterCooldownService with an injected clock) against the actual
    Bottle route `GET /api/packages/list` that carbon.js's `loadDownloads()`
    calls, proving the exact JSON this page renders from keeps the c294f65
    blacklist-scrub guarantees.
    """

    def setUp(self):
        self.app = Bottle()
        packages_api.setup_packages_routes(self.app)

    def _route(self):
        return next(
            route.callback
            for route in self.app.routes
            if route.method == "GET" and route.rule == "/api/packages/list"
        )

    def _call(self, shared_state, device=None):
        # `packages_list_api()` resolves the bare name `shared_state` from
        # its enclosing module's globals at call time, so patching the
        # `packages_api.shared_state` binding (done by the caller, around
        # this call) is what actually redirects it to a fake - this only
        # seeds the one extra key the route reads directly. Category lookup
        # opens its own real SQLite `DataBase` off `shared_state.values
        # ["dbfile"]` (a separate path from this fake's `get_db()`), so it is
        # stubbed the same way test_carbon_downloads_contract.py does.
        shared_state.values["device"] = device or NoopFakeDevice()
        with mock.patch(
            "quasarr.downloads.packages.get_download_category_from_package_id",
            return_value="movies",
        ):
            return self._route()()

    def test_alternative_link_survives_a_scrub_without_leaking_the_fingerprint(self):
        shared_state = RaisingSharedState(
            protected_rows=[
                (
                    PACKAGE_A,
                    protected_blob(
                        links=[["https://mirror-alt.invalid/c/2", "filecrypt"]],
                    ),
                )
            ]
        )
        with mock.patch.object(packages_api, "shared_state", shared_state):
            response = self._call(shared_state)

        serialized = json.dumps(response)
        self.assertNotIn("mirror-alt.invalid", serialized)
        self.assertIsNone(re.search(r"\b[0-9a-f]{64}\b", serialized))
        self.assertEqual(1, len(response["queue"]))
        self.assertEqual(0, len(response["history"]))
        self.assertEqual("waiting_captcha", response["queue"][0]["status"])

    def test_sole_link_blacklist_failure_appears_once_with_the_fixed_reason(self):
        reason = "Filecrypt URL permanently blacklisted; no remaining links available."
        shared_state = RaisingSharedState(
            failed_rows=[(PACKAGE_A, failed_blob(reason=reason))]
        )
        with mock.patch.object(packages_api, "shared_state", shared_state):
            response = self._call(shared_state)

        self.assertEqual(1, len(response["history"]))
        row = response["history"][0]
        self.assertEqual("failed", row["status"])
        self.assertEqual(reason, row["error"])
        self.assertEqual(0, len(response["queue"]))
        self.assertEqual(0, len(response["deferred"]))

    def test_real_cooldown_service_deferred_row_carries_no_fingerprint(self):
        clock = FakeClock(NOW)
        shared_state = RaisingSharedState(
            protected_rows=[(PACKAGE_A, protected_blob())]
        )
        CrypterCooldownService(shared_state, clock=clock).defer_package(
            PACKAGE_A, "filecrypt", "ip_block_suspected", NOW + 900, 1
        )

        def build_service(inner_shared_state):
            return CrypterCooldownService(inner_shared_state, clock=clock)

        with (
            mock.patch.object(packages_api, "shared_state", shared_state),
            mock.patch(
                "quasarr.downloads.packages.CrypterCooldownService", build_service
            ),
        ):
            response = self._call(shared_state)

        self.assertEqual(1, len(response["deferred"]))
        serialized = json.dumps(response)
        self.assertIsNone(re.search(r"\b[0-9a-f]{32}\b", serialized))
        self.assertIsNone(re.search(r"\b[0-9a-f]{64}\b", serialized))
        self.assertNotIn("filecrypt.invalid", serialized)


# ---------------------------------------------------------------------------
# 7. fail-mode probe unavailable / delete still available (route wiring only
#    - the routes themselves are unchanged and already covered by
#    test_deferred_packages_api.py and test_carbon_downloads_contract.py)
# ---------------------------------------------------------------------------


class DeferredRouteAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.app = Bottle()
        packages_api.setup_packages_routes(self.app)

    def test_probe_route_is_unavailable_in_fail_mode(self):
        shared_state = RaisingSharedState(
            protected_rows=[(PACKAGE_A, protected_blob())], block_mode="fail"
        )
        probe_route = next(
            route.callback
            for route in self.app.routes
            if route.method == "POST" and route.rule == "/api/packages/deferred/probe"
        )
        with (
            mock.patch.object(packages_api, "shared_state", shared_state),
            mock.patch.object(
                packages_api,
                "request",
                SimpleNamespace(json={"package_ids": [PACKAGE_A]}),
            ),
        ):
            result = probe_route()

        self.assertEqual(
            {"success": False, "message": "Linkcrypter blocks are in fail mode"},
            result,
        )

    def test_delete_route_stays_available_in_fail_mode(self):
        shared_state = RaisingSharedState(
            protected_rows=[(PACKAGE_A, protected_blob())], block_mode="fail"
        )
        # delete_deferred_package() only succeeds against a row that carries
        # real defer metadata (deletion is not a hold read, so it stays
        # available in fail mode - but it still needs something to delete).
        CrypterCooldownService(shared_state).defer_package(
            PACKAGE_A, "filecrypt", "ip_block_suspected", NOW + 900, 1
        )
        delete_route = next(
            route.callback
            for route in self.app.routes
            if route.method == "DELETE" and route.rule == "/api/packages/deferred"
        )
        with (
            mock.patch.object(packages_api, "shared_state", shared_state),
            mock.patch.object(
                packages_api,
                "request",
                SimpleNamespace(json={"package_ids": [PACKAGE_A]}),
            ),
        ):
            result = delete_route()

        self.assertEqual([PACKAGE_A], result["deleted"])


# ---------------------------------------------------------------------------
# Regression coverage from a review of the Downloads page (six findings)
# ---------------------------------------------------------------------------


class DownloadsRegressionFixesTests(unittest.TestCase):
    """Six findings from a review of the Downloads page, fixed together:

    The ordinary-delete confirmation control was an `<a>` with
    no `href`, which the shell's focus machinery never selects and which
    Enter/Space cannot activate: keyboard-only/screen-reader users could not
    delete a package at all. Fixed to a real `<button type="button">`.

    `restoreCollapseState()`'s last line assigned
    `summary.textContent = <string>`, destroying the
    `#downloads-other-count` span; from the second poll onward
    `byId('downloads-other-count')` returned null and the visible count
    froze at whatever it first showed. Fixed by rebuilding the summary from
    DOM nodes (`updateOtherSummary()`), never a bare string assignment, with
    the live total stored in `lastOtherTotal` instead of being re-parsed out
    of the summary's own previous text.

    The `deleted=` query param was never cleared and the
    status banner never auto-hid; `carbon.py`'s docstring falsely claimed
    this already happened. Fixed with
    `clearDeletedQueryParamAndScheduleBannerHide()`, and the docstring now
    describes the real mechanism.

    A1's row/bulk actions must be icon buttons, not text
    buttons. `trash-can`/`renew`/`unlocked` were added to
    `carbon_icons.py` (sourced from the Carbon repo, sha256
    recorded) and mirrored client-side (render_icon() is server-only) via
    `buildActionIcon()`; `.cds-row-actions` gained gap/alignment CSS and a
    danger color variant scoped so it never inherits the header's
    dark-background hover tint.

    Selection must be snapshotted immediately before the
    tbody is replaced, not before the fetch that produced the new rows (the
    race window widens on a slow connection). Moved from `loadDownloads()`
    into `renderDeferredTable()`.

    Icon-only buttons' `aria-label` (their whole accessible
    name, since aria-label overrides descendant content) must lead with the
    fixed action phrase, e.g. "Delete package and files: <name>", with the
    tooltip (`title`) matching exactly.
    """

    @classmethod
    def setUpClass(cls):
        cls.js = _read_static("carbon.js")
        cls.css = _read_static("carbon.css")

    # -- Delete confirm control is keyboard-reachable -------------------

    def test_ordinary_delete_confirm_control_is_a_button_not_an_anchor(self):
        body = javascript_function_body(self.js, "confirmDeletePackage")
        match = re.search(r'<(button|a)\b[^>]*id="downloads-confirm-delete"', body)
        self.assertIsNotNone(match, "no downloads-confirm-delete control found")
        self.assertEqual("button", match.group(1))
        self.assertIn('type="button"', body)

    def test_ordinary_delete_confirm_handler_has_no_pointless_preventdefault(self):
        body = javascript_function_body(self.js, "confirmDeletePackage")
        self.assertNotIn("preventDefault", body)

    # -- Other-packages count survives repeated polls --------------------

    def test_other_summary_never_destroys_the_count_span_via_blind_text_assignment(
        self,
    ):
        body = javascript_function_body(self.js, "updateOtherSummary")
        self.assertNotIn("summary.textContent = (", body)
        self.assertIn("createTextNode", body)
        self.assertIn("appendChild(countEl)", body)

    def test_other_summary_count_element_is_refetched_and_recreated_if_missing(self):
        body = javascript_function_body(self.js, "updateOtherSummary")
        self.assertIn("byId('downloads-other-count')", body)
        self.assertIn("createElement('span')", body)

    def test_render_other_tables_stores_the_live_total_every_call(self):
        body = javascript_function_body(self.js, "renderOtherTables")
        self.assertIn("lastOtherTotal = total", body)

    def test_restore_collapse_state_no_longer_reparses_stale_summary_text(self):
        body = javascript_function_body(self.js, "restoreCollapseState")
        self.assertNotIn("summary.textContent.match", body)
        self.assertNotIn("countMatch", body)

    # -- deleted= query param is cleared ---------------------------------

    def test_deleted_query_param_clear_and_banner_hide_function_shipped(self):
        self.assertIn("function clearDeletedQueryParamAndScheduleBannerHide(", self.js)
        body = javascript_function_body(
            self.js, "clearDeletedQueryParamAndScheduleBannerHide"
        )
        self.assertIn("searchParams.delete('deleted')", body)
        self.assertIn("history.replaceState", body)
        self.assertIn("5000", body)

    def test_deleted_query_param_clear_is_wired_into_init(self):
        init_body = self.js[self.js.rindex("addEventListener('DOMContentLoaded'") :]
        self.assertIn("clearDeletedQueryParamAndScheduleBannerHide();", init_body)

    def test_carbon_py_docstring_no_longer_falsely_claims_js_already_clears_it(self):
        carbon_py = _read_carbon_py_source()
        docstring_start = carbon_py.index("def _downloads_delete_status_banner")
        docstring_end = carbon_py.index(
            '"""', carbon_py.index('"""', docstring_start) + 3
        )
        docstring = carbon_py[docstring_start:docstring_end]
        self.assertIn("clearDeletedQueryParamAndScheduleBannerHide", docstring)

    # -- Row/bulk actions are icon buttons -------------------------------

    def test_carbon_icons_module_has_the_three_new_reviewed_icons(self):
        from quasarr.providers.carbon_icons import ICONS

        for name in ("trash-can", "renew", "unlocked"):
            with self.subTest(icon=name):
                self.assertIn(name, ICONS)
                spec = ICONS[name]
                self.assertTrue(
                    spec.source_url.startswith(
                        "https://raw.githubusercontent.com/carbon-design-system/carbon/"
                    )
                )
                self.assertRegex(spec.sha256, r"^[0-9a-f]{64}$")

    def test_delete_row_action_is_an_icon_button(self):
        # The queue/history trash control is icon-only: it repeats once per
        # row in a dense table, where a spelled-out label would dominate.
        body = javascript_function_body(self.js, "buildActionButton")
        self.assertIn("cds-icon-button", body)
        self.assertIn("buildActionIcon(", body)

    def test_deferred_row_actions_are_labelled_buttons(self):
        # The deferred table's actions are rarer, less obvious and not
        # destructive-by-icon, so they carry their words.
        body = javascript_function_body(self.js, "buildTextActionButton")
        self.assertIn("'cds-btn ' + variantClass + ' cds-btn--compact'", body)
        self.assertIn("buttonTextForAction(action)", body)
        self.assertNotIn("cds-icon-button", body)
        self.assertNotIn("buildActionIcon(", body)

        deferred = javascript_function_body(self.js, "buildDeferredRow")
        self.assertIn("buildTextActionButton(", deferred)
        self.assertIn("'cds-btn--ghost'", deferred)
        self.assertIn("'cds-btn--danger-ghost'", deferred)
        # No icon-only button is left in this row's action group.
        self.assertNotIn("buildActionButton(", deferred)

    def test_deferred_action_button_text_matches_the_design(self):
        body = javascript_function_body(self.js, "buttonTextForAction")
        self.assertIn("return 'Check';", body)
        self.assertIn("return 'Remove';", body)

    def test_captcha_link_is_an_icon_button(self):
        body = javascript_function_body(self.js, "buildCaptchaLink")
        self.assertIn("cds-icon-button", body)
        self.assertIn("buildActionIcon('unlocked')", body)

    def test_row_action_icon_set_matches_expected_icons(self):
        # Only the icons carbon.js draws itself are mirrored client-side.
        # The bulk toolbar's renew icon is server-rendered by render_icon(),
        # and the deferred row's actions carry words rather than icons.
        self.assertIn("'trash-can':", self.js)
        self.assertIn("unlocked:", self.js)
        self.assertNotIn("renew:", self.js)

    def test_row_actions_css_has_gap_and_alignment(self):
        self.assertRegex(
            self.css,
            r"\.cds-row-actions\s*\{[^}]*display:\s*flex;[^}]*gap:\s*4px;",
        )
        self.assertIn(".cds-icon-button--danger", self.css)

    # -- Selection snapshot timing ----------------------------------------

    def test_selection_snapshot_moved_into_render_deferred_table(self):
        body = javascript_function_body(self.js, "renderDeferredTable")
        self.assertIn("selectedDeferredPackageIds()", body)

    def test_load_downloads_no_longer_snapshots_before_the_fetch(self):
        body = javascript_function_body(self.js, "loadDownloads")
        self.assertNotIn("selectedDeferredPackageIds()", body)

    # -- Icon-only button aria-label ---------------------------------------

    def test_row_action_aria_labels_lead_with_the_action_phrase(self):
        build_deferred = javascript_function_body(self.js, "buildDeferredRow")
        self.assertIn("'Check now: ' + row.name", build_deferred)
        self.assertIn("'Remove pending package: ' + row.name", build_deferred)

        build_queue = javascript_function_body(self.js, "buildQueueRow")
        self.assertIn("'Delete package and files: ' + row.name", build_queue)

        build_history = javascript_function_body(self.js, "buildHistoryRow")
        self.assertIn("'Delete package and files: ' + row.name", build_history)

        build_captcha = javascript_function_body(self.js, "buildCaptchaLink")
        self.assertIn("'Solve CAPTCHA: '", build_captcha)

    def test_labelled_row_actions_satisfy_label_in_name(self):
        # WCAG 2.5.3: the accessible name must contain the visible words.
        # Pulls the real visible label buttonTextForAction() returns and
        # the real aria-label phrase buildDeferredRow() passes to
        # buildTextActionButton() for each action straight out of the
        # shipped source, then relates those two actual strings to each
        # other - not the test's own literal tuple against itself.
        deferred = javascript_function_body(self.js, "buildDeferredRow")
        text_map = javascript_function_body(self.js, "buttonTextForAction")
        for action in ("deferred-probe-one", "deferred-remove-one"):
            with self.subTest(action=action):
                phrase_match = re.search(
                    r"buildTextActionButton\(\s*'"
                    + re.escape(action)
                    + r"'\s*,\s*'([^']+)'\s*\+\s*row\.name",
                    deferred,
                )
                self.assertIsNotNone(
                    phrase_match, f"no aria-label phrase found for {action}"
                )
                phrase = phrase_match.group(1)

                visible_match = re.search(
                    r"case '" + re.escape(action) + r"':\s*return '([^']+)';",
                    text_map,
                )
                self.assertIsNotNone(
                    visible_match, f"no visible label found for {action}"
                )
                visible = visible_match.group(1)

                self.assertTrue(
                    phrase.startswith(visible),
                    f"visible label {visible!r} is not a prefix of the "
                    f"aria-label phrase {phrase!r}",
                )

    def test_row_action_title_matches_aria_label_exactly(self):
        # buildActionButton()/buildCaptchaLink() both set title from the
        # same `label` variable used for aria-label - never a second,
        # independently worded string.
        body = javascript_function_body(self.js, "buildActionButton")
        self.assertIn("setAttribute('aria-label', label)", body)
        self.assertIn("setAttribute('title', label)", body)

        text_body = javascript_function_body(self.js, "buildTextActionButton")
        self.assertIn("setAttribute('aria-label', label)", text_body)
        self.assertIn("setAttribute('title', label)", text_body)

        captcha_body = javascript_function_body(self.js, "buildCaptchaLink")
        self.assertIn("setAttribute('aria-label', label)", captcha_body)
        self.assertIn("link.title = label", captcha_body)


class StickyCheckboxColumnTests(unittest.TestCase):
    """Under 672px the deferred table's checkbox column (first-child, sized
    40px at every viewport) was only SIZED, never pinned - only the release
    column (nth-child(2)) was made
    `position: sticky`. Scrolling the table horizontally therefore let the
    checkbox column slide out from under the still-pinned release column,
    leaving its 40px reserved on the left unpinned. Both columns must be
    sticky together inside the same 672px breakpoint.
    """

    @classmethod
    def setUpClass(cls):
        cls.css = _read_static("carbon.css")

    def _breakpoint_block(self):
        # carbon.css ships several `@media (max-width: 672px)` blocks (one
        # per feature area). The sizing rule `.cds-table--sticky-col
        # th:first-child { width: 40px; }` lives OUTSIDE any media query,
        # immediately followed by the media block that actually pins both
        # columns - so anchor on that sizing rule and take the NEXT 672px
        # media block after it, not the nearest preceding one (an earlier,
        # unrelated 672px block for a different feature area is closer by
        # text position but is not this one).
        anchor = self.css.index(".cds-table--sticky-col th:first-child")
        media_start = self.css.index("@media (max-width: 672px) {", anchor)
        # The media query's own closing brace sits at column 0 (`\n}\n`);
        # every nested rule inside it closes tab-indented (`\n\t}\n`), so
        # this literal search cannot stop early on a nested rule.
        close_index = self.css.index("\n}\n", media_start)
        return self.css[media_start:close_index]

    def test_checkbox_column_is_pinned_alongside_the_release_column(self):
        block = self._breakpoint_block()
        self.assertRegex(
            block,
            r"\.cds-table--sticky-col th:first-child,\s*"
            r"\.cds-table--sticky-col td:first-child\s*\{"
            r"[^}]*position:\s*sticky;"
            r"[^}]*left:\s*0;"
            r"[^}]*background:\s*var\(--cds-layer\);"
            r"[^}]*z-index:\s*1;",
        )

    def test_release_column_still_pinned_at_the_checkbox_columns_width(self):
        block = self._breakpoint_block()
        self.assertRegex(
            block,
            r"\.cds-table--sticky-col th:nth-child\(2\),\s*"
            r"\.cds-table--sticky-col td:nth-child\(2\)\s*\{"
            r"[^}]*position:\s*sticky;"
            r"[^}]*left:\s*40px;",
        )

    def test_checkbox_column_width_is_unconditional_and_matches_the_sticky_offset(
        self,
    ):
        # The nth-child(2) `left: 40px` offset only lines up with the real
        # checkbox column width if that width rule (outside any media
        # query, so it applies at every viewport) still says 40px.
        self.assertRegex(
            self.css,
            r"\.cds-table--sticky-col th:first-child,\s*"
            r"\.cds-table--sticky-col td:first-child\s*\{\s*width:\s*40px;\s*\}",
        )


class DownloadRowDesignTests(unittest.TestCase):
    """Every download row is built client-side, so the target design for the
    Queue/History/Deferred tables and the Dashboard queue preview is pinned
    against the shipped `carbon.js` text (no JS engine in this suite) plus
    the server-rendered table heads those rows have to line up with.

    The design replaces the queue's status text column with a coloured status
    dot, tags the category, and puts a progress bar with a visible percentage
    in the Progress column; History leads with a Completed/Failed tag;
    Deferred shows its state as a dot and its sweep as a bar.
    """

    @classmethod
    def setUpClass(cls):
        cls.js = _read_static("carbon.js")
        cls.css = _read_static("carbon.css")

    def _render(self):
        shared_state = RaisingSharedState()
        shared_state.values["database"] = lambda table: FakeProtectedDatabaseTable([])
        return carbon.render_downloads(shared_state)

    # -- Shared row vocabulary --------------------------------------------

    def test_status_dot_helper_builds_a_carbon_status_indicator(self):
        body = javascript_function_body(self.js, "buildStatusDot")
        self.assertIn("cds-status cds-status--", body)
        self.assertIn("cds-status__dot", body)
        self.assertIn("'aria-hidden', 'true'", body)

    def test_progress_helper_announces_its_value_to_screen_readers(self):
        # The same accessibility contract the Statistics ratio bars carry
        # (`role="progressbar"` plus aria-valuemin/max/now and an identifying
        # aria-label) - a bar that announces nothing is a regression this
        # project has already rejected once.
        body = javascript_function_body(self.js, "buildProgress")
        self.assertIn("cds-progress__fill cds-progress__fill--", body)
        self.assertIn("'role', 'progressbar'", body)
        self.assertIn("'aria-valuemin', '0'", body)
        self.assertIn("'aria-valuemax', '100'", body)
        self.assertIn("'aria-valuenow'", body)
        self.assertIn("'aria-label'", body)

    # -- Queue rows --------------------------------------------------------

    def test_queue_row_leads_with_a_status_dot_instead_of_a_status_column(self):
        body = javascript_function_body(self.js, "buildQueueRow")
        self.assertIn("buildStatusDot(QUEUE_STATUS_TONES[row.status]", body)
        self.assertLess(body.index("buildStatusDot("), body.index("cds-release"))
        # The four status labels are the dot's accessible name and tooltip
        # now, never a plain text cell of their own.
        self.assertNotIn("appendTextCell(tr, QUEUE_STATUS_LABELS", body)

    def test_queue_row_status_dot_is_still_named_for_assistive_technology(self):
        body = javascript_function_body(self.js, "buildQueueRow")
        self.assertIn("cds-visually-hidden", body)
        self.assertIn("'title', statusLabel", body)

    def test_queue_row_tags_its_category(self):
        body = javascript_function_body(self.js, "buildQueueRow")
        self.assertIn("cds-tag cds-tag--", body)
        self.assertIn("CATEGORY_TONES[row.category]", body)

    def test_queue_row_offers_the_captcha_action_under_the_release_name(self):
        body = javascript_function_body(self.js, "buildQueueRow")
        self.assertIn("buildCaptchaLink(row.package_id, row.name, 'inline')", body)
        # Beside the release name, not down in the row's action group.
        self.assertLess(body.index("buildCaptchaLink("), body.index("cds-row-actions"))
        # One builder owns both presentations, so the inline variant cannot
        # drift away from the icon button's target or accessible name.
        link_body = javascript_function_body(self.js, "buildCaptchaLink")
        self.assertIn("cds-release__action", link_body)
        # Same label form the server-rendered link actions use.
        self.assertIn("'Solve CAPTCHA " + chr(0x2192) + "'", link_body)

    def test_polling_never_replaces_the_subtree_holding_the_filter(self):
        """The filter input sits inside `#downloads-content` now, so its
        value and focus survive a refresh only because every render path
        clears a `<tbody>` and never a whole tile or the container itself.
        """
        for name in (
            "renderDownloads",
            "renderQueueTable",
            "renderDeferredTable",
            "renderHistoryTable",
            "renderOtherTables",
            "renderDisconnected",
        ):
            with self.subTest(function=name):
                body = javascript_function_body(self.js, name)
                self.assertNotIn("innerHTML", body)
                self.assertNotIn("content.textContent", body)

    def test_filter_reads_the_input_and_still_matches_every_rendered_row(self):
        body = javascript_function_body(self.js, "applySearchFilter")
        self.assertIn("SEARCH_SCOPES.forEach(", body)
        self.assertIn("byId(scope.field)", body)
        self.assertIn("'tr[data-package-name]'", body)
        # Every table that renders rows must belong to exactly one scope, or
        # a filtered table silently keeps showing everything.
        scopes = self.js[
            self.js.index("var SEARCH_SCOPES = [") : self.js.index(
                "function rowMatchesTerm"
            )
        ]
        for body_id in (
            "deferred-table-body",
            "queue-table-body",
            "history-table-body",
            "other-queue-table-body",
            "other-history-table-body",
        ):
            with self.subTest(body_id=body_id):
                self.assertEqual(1, scopes.count(f"'{body_id}'"))
        for field_id in (
            "deferred-search",
            "downloads-search",
            "history-search",
            "other-search",
        ):
            with self.subTest(field_id=field_id):
                self.assertEqual(1, scopes.count(f"'{field_id}'"))

    def test_the_filter_matches_the_crypter_and_host_as_well_as_the_name(self):
        # "filecrypt" has to be a usable search term - that is what the
        # origin columns are for.
        body = javascript_function_body(self.js, "rowMatchesTerm")
        self.assertIn("row.dataset.packageName", body)
        self.assertIn("row.dataset.crypter", body)
        self.assertIn("row.dataset.mirror", body)

    def test_sorting_reorders_the_data_not_the_rendered_rows(self):
        # A DOM-level sort would be undone by the next 5s poll, and it would
        # break selection restore, which matches checkboxes by value.
        for name, table_key in (
            ("renderDeferredTable", "'deferred'"),
            ("renderQueueTable", "'queue'"),
            ("renderHistoryTable", "'history'"),
        ):
            with self.subTest(function=name):
                body = javascript_function_body(self.js, name)
                self.assertIn(f"sortRows({table_key}, rows)", body)
                self.assertLess(body.index("sortRows("), body.index("appendChild("))
        other = javascript_function_body(self.js, "renderOtherTables")
        self.assertIn("sortRows('other-queue', otherQueueRows)", other)
        self.assertIn("sortRows('other-history', otherHistoryRows)", other)

    def test_a_missing_sort_value_sorts_last_in_both_directions(self):
        # Packages older than the origin record have no added_epoch; a
        # descending sort must not push everything that matters off the top.
        body = javascript_function_body(self.js, "sortRows")
        self.assertIn("aMissing !== bMissing", body)
        self.assertIn("return aMissing ? 1 : -1", body)

    def test_sorting_is_tri_state_and_returns_to_the_table_default(self):
        body = javascript_function_body(self.js, "nextSortState")
        self.assertIn("direction: 'asc'", body)
        self.assertIn("direction: 'desc'", body)
        self.assertIn("DEFAULT_SORT[tableKey]", body)

    def test_the_default_column_toggles_instead_of_dead_ending_on_itself(self):
        # Added is the default for Queue/History (newest first). Falling back
        # to the default there would return the same state, so clicking that
        # header would do nothing at all and oldest-first would be
        # unreachable (measured in the browser before this branch existed).
        body = javascript_function_body(self.js, "nextSortState")
        self.assertIn("isDefaultSort(tableKey, state)", body)
        self.assertIn("state.direction === 'asc' ? 'desc' : 'asc'", body)
        compare = javascript_function_body(self.js, "isDefaultSort")
        self.assertIn("fallback.key === state.key", compare)
        self.assertIn("fallback.direction === state.direction", compare)

    def test_a_sort_click_reorders_without_waiting_for_the_next_poll(self):
        # loadDownloads() returns early while a poll is in flight, so routing
        # a sort click through it alone leaves the header unresponsive for up
        # to a whole refresh interval (measured in the browser).
        body = javascript_function_body(self.js, "onSortHeadClick")
        self.assertIn("renderDownloads(lastPayload)", body)
        render = javascript_function_body(self.js, "renderDownloads")
        self.assertIn("lastPayload = data", render)
        self.assertIn("lastPayload = null", render)

    def test_sort_state_is_mirrored_onto_aria_sort(self):
        body = javascript_function_body(self.js, "applySortIndicators")
        self.assertIn("'aria-sort'", body)
        self.assertIn("'descending'", body)
        self.assertIn("'ascending'", body)
        self.assertIn("'none'", body)

    def test_queue_row_ends_with_a_progress_bar_and_a_percentage(self):
        body = javascript_function_body(self.js, "buildQueueRow")
        self.assertIn("buildProgress(", body)
        self.assertIn("row.percentage", body)
        self.assertIn("QUEUE_BAR_TONES[row.status]", body)
        self.assertIn("String(row.percentage) + '%'", body)

    def test_queue_row_keeps_its_delete_action_and_row_identity(self):
        body = javascript_function_body(self.js, "buildQueueRow")
        self.assertIn("tr.dataset.packageId = row.package_id", body)
        self.assertIn("tr.dataset.packageName = row.name", body)
        self.assertIn("'Delete package and files: ' + row.name", body)

    # -- History rows ------------------------------------------------------

    def test_history_row_builder_uses_a_status_tag_before_the_release(self):
        body = javascript_function_body(self.js, "buildHistoryRow")
        self.assertIn("HISTORY_STATUS_TONES[row.status]", body)
        self.assertLess(body.index("cds-tag cds-tag--"), body.index("cds-release"))

    def test_history_row_shows_the_failure_reason_under_the_release_name(self):
        body = javascript_function_body(self.js, "buildHistoryRow")
        self.assertIn("cds-release__error", body)
        self.assertLess(body.index("'cds-release'"), body.index("cds-release__error"))

    def test_history_row_keeps_its_delete_action_and_row_identity(self):
        body = javascript_function_body(self.js, "buildHistoryRow")
        self.assertIn("tr.dataset.packageId = row.package_id", body)
        self.assertIn("tr.dataset.packageName = row.name", body)
        self.assertIn("'Delete package and files: ' + row.name", body)

    # -- Deferred rows -----------------------------------------------------

    def test_deferred_row_builder_uses_a_status_dot_not_a_tag(self):
        body = javascript_function_body(self.js, "buildDeferredRow")
        self.assertIn("DEFERRED_STATE_TONES[row.state]", body)
        self.assertIn("buildStatusDot(", body)
        self.assertNotIn("cds-tag cds-tag--' + tone", body)

    def test_deferred_row_names_crypter_and_reason_before_the_state(self):
        # The crypter moved from the helper line under the release into its
        # own sortable column (appendOriginCell), which also carries the
        # host; the reason is what stays under the release. Both still come
        # before the state dot, as the design has it.
        body = javascript_function_body(self.js, "buildDeferredRow")
        self.assertIn("appendOriginCell(tr, row)", body)
        self.assertIn("row.reason_label", body)
        self.assertLess(body.index("row.reason_label"), body.index("buildStatusDot("))
        self.assertLess(
            body.index("appendOriginCell(tr, row)"), body.index("buildStatusDot(")
        )

    def test_every_row_builder_emits_exactly_as_many_cells_as_its_head(self):
        # A row builder and its table head are edited in two different files;
        # nothing but this pins them to the same column count, and a mismatch
        # silently shifts every value one column sideways.
        html = self._render()
        for builder, table_id in (
            ("buildDeferredRow", "deferred-table"),
            ("buildQueueRow", "queue-table"),
            ("buildHistoryRow", "history-table"),
        ):
            with self.subTest(builder=builder):
                body = javascript_function_body(self.js, builder)
                cells = len(re.findall(r"\btr\.appendChild\(", body)) + len(
                    re.findall(r"\bappend(?:Text|Origin|Added)Cell\(tr\b", body)
                )
                self.assertEqual(len(self._head_labels(html, table_id)), cells)

    def test_every_row_builder_carries_the_origin_and_added_cells(self):
        for builder in ("buildDeferredRow", "buildQueueRow", "buildHistoryRow"):
            with self.subTest(builder=builder):
                body = javascript_function_body(self.js, builder)
                self.assertIn("appendOriginCell(tr, row)", body)
                self.assertIn("appendAddedCell(tr, row.added_epoch)", body)
                # The search matches on crypter and host, so both must reach
                # the row's dataset, not only its rendered cells.
                self.assertIn("markOriginDataset(tr, row)", body)

    def test_the_origin_cell_never_builds_markup_from_response_data(self):
        body = javascript_function_body(self.js, "appendOriginCell")
        self.assertNotIn("innerHTML", body)
        self.assertIn("buildEl('span', 'cds-origin__name'", body)
        self.assertIn("buildEl('span', 'cds-origin__host'", body)

    def test_deferred_row_shows_evidence_in_mono(self):
        body = javascript_function_body(self.js, "buildDeferredRow")
        self.assertIn("cds-mono", body)
        self.assertIn("row.evidence_count", body)

    def test_deferred_row_keeps_both_shared_countdowns(self):
        body = javascript_function_body(self.js, "buildDeferredRow")
        self.assertIn("buildCountdownRow('Retry', 'data-retry-after-epoch'", body)
        self.assertIn("'data-cohort-deadline-epoch'", body)

    def test_deferred_row_shows_sweep_progress_as_a_bar(self):
        body = javascript_function_body(self.js, "buildDeferredRow")
        self.assertIn("buildProgress(", body)
        self.assertIn("row.cohort_tested", body)
        self.assertIn("row.cohort_total", body)

    def test_deferred_row_keeps_selection_checkbox_and_row_identity(self):
        body = javascript_function_body(self.js, "buildDeferredRow")
        self.assertIn("deferred-select", body)
        self.assertIn("checkbox.value = row.package_id", body)
        self.assertIn("tr.dataset.packageId = row.package_id", body)
        self.assertIn("tr.dataset.packageName = row.name", body)

    # -- Dashboard queue preview -------------------------------------------

    def test_dashboard_queue_preview_uses_the_same_row_vocabulary(self):
        body = javascript_function_body(self.js, "renderQueueRows")
        self.assertIn("cds-queue-preview__row", body)
        self.assertIn("cds-release", body)
        self.assertIn("cds-queue-preview__meta", body)
        self.assertIn("buildPreviewProgress(", body)

    def test_dashboard_queue_preview_bar_is_announced_too(self):
        body = javascript_function_body(self.js, "buildPreviewProgress")
        self.assertIn("cds-progress__fill cds-progress__fill--", body)
        self.assertIn("'role', 'progressbar'", body)
        self.assertIn("'aria-valuenow'", body)
        self.assertIn("'aria-label'", body)

    def test_dashboard_queue_preview_meta_pairs_percentage_with_eta(self):
        body = javascript_function_body(self.js, "queuePreviewMeta")
        self.assertIn("row.percentage", body)
        self.assertIn("row.eta", body)
        self.assertIn("row.eta_unknown", body)

    # -- Table heads the client-side rows line up with ---------------------

    def _head_labels(self, html, table_id):
        table_start = html.index(f'id="{table_id}"')
        thead_end = html.index("</thead>", table_start)
        head_html = html[table_start:thead_end]
        return [
            re.sub(r"<[^>]*>", "", header).strip()
            for header in re.findall(r"<th[^>]*>(.*?)</th>", head_html, re.S)
        ]

    def test_queue_table_head_follows_the_design(self):
        # Queue and Other-queue share one row builder, so both heads have to
        # carry exactly the same columns in the same order - that invariant,
        # not any particular markup, is what this pins.
        html = self._render()
        expected = [
            "",
            "Release",
            "Crypter",
            "Category",
            "Size",
            "ETA",
            "Progress",
            "Added",
            "",
        ]
        self.assertEqual(expected, self._head_labels(html, "queue-table"))
        self.assertEqual(expected, self._head_labels(html, "other-queue-table"))

    def test_history_table_head_follows_the_design(self):
        html = self._render()
        expected = ["Status", "Release", "Crypter", "Category", "Size", "Added", ""]
        self.assertEqual(expected, self._head_labels(html, "history-table"))
        self.assertEqual(expected, self._head_labels(html, "other-history-table"))

    def test_queue_tile_head_carries_a_live_counter(self):
        html = self._render()
        self.assertIn('id="queue-count"', html)
        body = javascript_function_body(self.js, "renderQueueTable")
        self.assertIn("updateQueueCount(rows.length)", body)

    def test_deferred_bulk_toolbar_leads_with_the_selection_count(self):
        html = self._render()
        self.assertLess(
            html.index('id="deferred-selection-count"'),
            html.index('data-action="deferred-probe-selected"'),
        )
        self.assertIn("cds-btn cds-btn--tertiary", html)

    # -- CSS the new row parts need -----------------------------------------

    def test_row_component_css_is_shipped(self):
        for selector in (
            ".cds-release {",
            ".cds-release__action {",
            ".cds-release__error {",
            ".cds-progress-cell {",
            ".cds-progress-cell__label {",
            ".cds-queue-preview__row {",
        ):
            with self.subTest(selector=selector):
                self.assertIn(selector, self.css)


class DownloadsNoscriptTests(unittest.TestCase):
    """render_downloads() ships only a loading skeleton - every row for
    every section is populated client-side
    from GET /api/packages/list. A JS-disabled visitor previously saw an
    empty shell (four "Loading..." messages that never resolve) with no
    indication anything was wrong. A <noscript> block must point them to the
    fully server-rendered Classic UI instead.
    """

    def _render(self):
        shared_state = RaisingSharedState()
        shared_state.values["database"] = lambda table: FakeProtectedDatabaseTable([])
        return carbon.render_downloads(shared_state)

    def test_noscript_block_present(self):
        html = self._render()
        self.assertIn("<noscript>", html)
        self.assertIn("</noscript>", html)

    def test_noscript_links_to_classic_ui(self):
        # The `?next=/packages` round-trip is the useful part - without it
        # a JS-disabled visitor following this link out of Downloads would
        # land on Classic's Dashboard, not back on Downloads.
        html = self._render()
        noscript_start = html.index("<noscript>")
        noscript_end = html.index("</noscript>") + len("</noscript>")
        noscript_html = html[noscript_start:noscript_end]
        self.assertIn('href="/ui/classic?next=/packages"', noscript_html)

    def test_noscript_precedes_the_client_rendered_content(self):
        # Must be visible/readable regardless of where JS gives up, so it
        # has to appear before the skeleton it is explaining.
        html = self._render()
        self.assertLess(html.index("<noscript>"), html.index('id="downloads-content"'))

    def test_noscript_survives_structural_guards(self):
        from quasarr.providers.carbon_templates import _assert_structural_guards

        html = self._render()
        _assert_structural_guards(html)


if __name__ == "__main__":
    unittest.main()
