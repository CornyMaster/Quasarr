# -*- coding: utf-8 -*-

"""Contracts for the Carbon statistics view.

Pins the 37-value coverage of ``StatsHelper.get_stats()`` against the Carbon
renderer's four top KPI metric tiles plus its five detail tiles in one
self-arranging ``cds-grid--auto`` grid, the two pinned headings ("Filecrypt
cohort", "Terminal operations"), the Filecrypt lifecycle state rendered as a
status badge, the tested/total ratio bar, the ``<time data-epoch>`` deadline
contract, and the structural/privacy guards (no emoji, no identifier-shaped
lifecycle data, no remote resources, no inline handlers/scripts).
"""

import importlib
import os
import re
import unittest
from datetime import datetime, timezone
from io import BytesIO
from unittest import mock

from bottle import Bottle

# Distinct, unique multi-digit values so each assertion can only be satisfied
# by the field it names - no two formatted values are substrings of another.
STATS_FIXTURE = {
    "total_download_attempts": 10234,
    "download_success_rate": 91.2,
    "total_captcha_decryptions": 20345,
    "decryption_success_rate": 82.5,
    "packages_downloaded": 30456,
    "links_processed": 40567,
    "failed_downloads": 5067,
    "average_links_per_package": 4.7,
    "captcha_decryptions_automatic": 60789,
    "automatic_decryption_success_rate": 77.1,
    "captcha_decryptions_manual": 70891,
    "manual_decryption_success_rate": 66.3,
    "failed_decryptions_automatic": 8012,
    "failed_decryptions_manual": 9123,
    "crypter_block_observations": 11234,
    "crypter_cooldowns": 12345,
    "crypter_probes": 13456,
    "deferred_packages": 14567,
    "crypter_sweep_state": "sweeping",
    "crypter_sweep_tested": 15678,
    "crypter_sweep_total": 99999,
    "crypter_sweep_deadline_epoch": 1750000000,
    "crypter_cooldown_count": 16789,
    "crypter_retest_depth": 17890,
    "crypter_individual_mode": "cohort_oversized",
    "terminal_operations_prepared": 18901,
    "terminal_operations_submitted": 19012,
    "terminal_operations_complete": 20123,
    "metadata_total_cached": 21234,
    "imdb_total_cached": 22345,
    "imdb_with_title": 23456,
    "imdb_with_poster": 24567,
    "imdb_with_localized": 25678,
    "xem_all_names_valid": 26789,
    "xem_all_names_cached": 27890,
    "xem_season_total_cached": 28901,
    "xem_season_valid_cached": 29012,
}

assert len(STATS_FIXTURE) == 37, "fixture must cover exactly 37 values"


class _FakeProtectedDB:
    def __init__(self, titles):
        self._titles = titles

    def retrieve_all_titles(self):
        return self._titles


class _FakeSharedState:
    """Minimal shared_state stub: only what captcha-count reading touches."""

    def __init__(self, protected_titles=()):
        self._protected_titles = list(protected_titles)
        self.values = {
            "database": lambda table: _FakeProtectedDB(
                self._protected_titles if table == "protected" else []
            )
        }


class CarbonStatisticsCoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = importlib.import_module("quasarr.api.statistics.carbon")

    def _render(self, stats=None, protected_titles=()):
        stats = stats if stats is not None else dict(STATS_FIXTURE)
        shared_state = _FakeSharedState(protected_titles=protected_titles)
        with (
            mock.patch.object(self.mod, "StatsHelper") as helper_cls,
            mock.patch.object(self.mod, "show_logout_link", return_value=True),
        ):
            helper_cls.return_value.get_stats.return_value = stats
            html = self.mod.render_statistics(shared_state)
        return html

    def test_render_statistics_exists(self):
        self.assertTrue(callable(self.mod.render_statistics))

    def test_all_37_values_are_rendered(self):
        html = self._render()

        expectations = [
            f"{STATS_FIXTURE['total_download_attempts']:,}",
            f"{STATS_FIXTURE['download_success_rate']:.1f}%",
            f"{STATS_FIXTURE['total_captcha_decryptions']:,}",
            f"{STATS_FIXTURE['decryption_success_rate']:.1f}%",
            f"{STATS_FIXTURE['packages_downloaded']:,}",
            f"{STATS_FIXTURE['links_processed']:,}",
            f"{STATS_FIXTURE['failed_downloads']:,}",
            f"{STATS_FIXTURE['average_links_per_package']:.1f}",
            f"{STATS_FIXTURE['captcha_decryptions_automatic']:,}",
            f"{STATS_FIXTURE['automatic_decryption_success_rate']:.1f}%",
            f"{STATS_FIXTURE['captcha_decryptions_manual']:,}",
            f"{STATS_FIXTURE['manual_decryption_success_rate']:.1f}%",
            f"{STATS_FIXTURE['failed_decryptions_automatic']:,}",
            f"{STATS_FIXTURE['failed_decryptions_manual']:,}",
            f"{STATS_FIXTURE['crypter_block_observations']:,}",
            f"{STATS_FIXTURE['crypter_cooldowns']:,}",
            f"{STATS_FIXTURE['crypter_probes']:,}",
            f"{STATS_FIXTURE['deferred_packages']:,}",
            # The Filecrypt cohort state renders through `status()`, which
            # capitalizes it for display (a presentation choice, not data
            # loss - the same state text is shown, just title-cased).
            STATS_FIXTURE["crypter_sweep_state"].capitalize(),
            (
                f"{STATS_FIXTURE['crypter_sweep_tested']:,} / "
                f"{STATS_FIXTURE['crypter_sweep_total']:,}"
            ),
            f"{STATS_FIXTURE['crypter_cooldown_count']:,}",
            f"{STATS_FIXTURE['crypter_retest_depth']:,}",
            STATS_FIXTURE["crypter_individual_mode"],
            f"{STATS_FIXTURE['terminal_operations_prepared']:,}",
            f"{STATS_FIXTURE['terminal_operations_submitted']:,}",
            f"{STATS_FIXTURE['terminal_operations_complete']:,}",
            f"{STATS_FIXTURE['metadata_total_cached']:,}",
            f"{STATS_FIXTURE['imdb_total_cached']:,}",
            f"{STATS_FIXTURE['imdb_with_title']:,}",
            f"{STATS_FIXTURE['imdb_with_poster']:,}",
            f"{STATS_FIXTURE['imdb_with_localized']:,}",
            f"{STATS_FIXTURE['xem_all_names_valid']:,}",
            f"{STATS_FIXTURE['xem_all_names_cached']:,}",
            f"{STATS_FIXTURE['xem_season_total_cached']:,}",
            f"{STATS_FIXTURE['xem_season_valid_cached']:,}",
        ]
        # The tested/total entry covers two fields (crypter_sweep_tested and
        # crypter_sweep_total) in one string, and crypter_sweep_deadline_epoch
        # is asserted separately below (formatted time) - together the list
        # plus those two extras cover exactly 37 fields.
        self.assertEqual(len(expectations) + 2, 37)

        for expected in expectations:
            with self.subTest(expected=expected):
                self.assertIn(str(expected), html)

        self.assertIn(
            f'data-epoch="{STATS_FIXTURE["crypter_sweep_deadline_epoch"]}"', html
        )
        moment = datetime.fromtimestamp(
            STATS_FIXTURE["crypter_sweep_deadline_epoch"], tz=timezone.utc
        )
        self.assertIn(moment.strftime("%Y-%m-%d"), html)

    def test_pinned_headings_present(self):
        html = self._render()
        self.assertIn("Filecrypt cohort", html)
        self.assertIn("Terminal operations", html)

    def test_lifecycle_state_rendered_as_status_badge(self):
        """The Filecrypt cohort state renders through the shared `status()`
        component (a colored-dot badge, not a `cds-tag`) with the state text
        capitalized for display; the individual-mode reason stays plain,
        unbadged text in its own key/value row.
        """
        html = self._render()
        self.assertRegex(
            html,
            r'<span class="cds-status cds-status--warning cds-status--strong">'
            r'<span class="cds-status__dot" aria-hidden="true"></span>Sweeping</span>',
        )
        self.assertIn(
            '<span class="cds-kv__label">Individual mode</span>'
            '<span class="cds-kv__value">cohort_oversized</span>',
            html,
        )

    def test_individual_mode_falls_back_to_none(self):
        stats = dict(STATS_FIXTURE)
        stats["crypter_individual_mode"] = ""
        html = self._render(stats=stats)
        self.assertIn(
            '<span class="cds-kv__label">Individual mode</span>'
            '<span class="cds-kv__value">none</span>',
            html,
        )

    def test_zero_deadline_epoch_has_no_epoch_time_element(self):
        stats = dict(STATS_FIXTURE)
        stats["crypter_sweep_deadline_epoch"] = 0
        html = self._render(stats=stats)
        self.assertNotIn('data-epoch="0"', html)
        self.assertNotIn("1970", html)
        # Absence of the epoch element isn't enough on its own - the reader
        # depends on the fixed fallback text actually appearing in its place.
        self.assertIn(
            '<span class="cds-kv__label">Deadline</span>'
            '<span class="cds-kv__value">No active deadline</span>',
            html,
        )

    def test_deadline_is_a_time_element_with_data_epoch(self):
        html = self._render()
        self.assertRegex(
            html,
            r'<time datetime="[^"]+" data-epoch="1750000000">[^<]+</time>',
        )

    def test_active_page_is_statistics(self):
        html = self._render()
        self.assertIn('href="/statistics" aria-current="page"', html)
        self.assertIn("<title>Statistics</title>", html)

    def test_captcha_count_reflects_live_protected_queue(self):
        html = self._render(protected_titles=[("id-1", "t"), ("id-2", "t")])
        self.assertIn(
            '<span class="cds-header__badge" aria-hidden="true">2</span>', html
        )
        self.assertIn('aria-label="Notifications, 2 CAPTCHA items"', html)

    def test_captcha_count_defaults_to_zero_on_lookup_failure(self):
        shared_state = _FakeSharedState()
        shared_state.values["database"] = lambda table: (_ for _ in ()).throw(
            RuntimeError("unavailable")
        )
        with (
            mock.patch.object(self.mod, "StatsHelper") as helper_cls,
            mock.patch.object(self.mod, "show_logout_link", return_value=True),
        ):
            helper_cls.return_value.get_stats.return_value = dict(STATS_FIXTURE)
            html = self.mod.render_statistics(shared_state)
        # An empty queue shows no bell badge at all (spec 2.5: badge only
        # for a counter > 0); the swallowed read failure must still reach
        # the shell as a real 0, which the bell's accessible name carries.
        self.assertIn('aria-label="Notifications, 0 CAPTCHA items"', html)
        self.assertNotIn("cds-header__badge", html)

    def test_captcha_decryption_bars_show_value_and_help_text(self):
        """The CAPTCHA-decryptions detail tile's two bars keep every
        underlying number visible as text: the raw decryption count in the
        bar's head row, and the success rate plus failed count in its help
        line - the same six values the old kv rows showed, just regrouped.
        """
        html = self._render()
        self.assertIn(
            '<div class="cds-bar__head"><span>Automatic (SponsorsHelper)</span>'
            "<strong>60,789</strong></div>",
            html,
        )
        self.assertIn(
            '<div class="cds-bar__head"><span>Manual (browser userscript)</span>'
            "<strong>70,891</strong></div>",
            html,
        )
        self.assertIn('<p class="cds-bar__help">77.1% success · 8,012 failed</p>', html)
        self.assertIn('<p class="cds-bar__help">66.3% success · 9,123 failed</p>', html)

    def test_bar_width_clamped_to_0_100(self):
        """`_bar()`'s fill width is clamped to [0, 100] independent of the
        displayed value text, matching the old ratio bar's clamp contract -
        and `aria-valuenow` carries that exact same clamped integer, never
        the raw unclamped input.
        """
        over = self.mod._bar("Label", "150", 150)
        under = self.mod._bar("Label", "-12", -12)
        self.assertIn('style="width:100%"', over)
        self.assertIn('style="width:0%"', under)
        self.assertIn('aria-valuenow="100"', over)
        self.assertIn('aria-valuenow="0"', under)
        self.assertNotIn('aria-valuenow="150"', over)
        self.assertNotIn('aria-valuenow="-12"', under)
        # The displayed value text is never rewritten by the clamp.
        self.assertIn("<strong>150</strong>", over)
        self.assertIn("<strong>-12</strong>", under)

    def test_progress_bars_carry_aria_contract(self):
        """Every `.cds-progress` bar keeps the accessibility contract the
        old `_ratio_bar()` had: `role="progressbar"` plus
        `aria-valuemin`/`aria-valuemax`/`aria-valuenow` and an identifying
        `aria-label`, so a screen reader still announces which bar it is and
        its current value. Regression guard for that contract silently
        disappearing again.
        """
        html = self._render()
        self.assertEqual(html.count('role="progressbar"'), 3)
        self.assertEqual(html.count('aria-valuemin="0"'), 3)
        self.assertEqual(html.count('aria-valuemax="100"'), 3)
        # Automatic: 60,789 / 131,680 share -> 46%; Manual -> 53%;
        # Filecrypt cohort Tested: 15,678 / 99,999 -> 16% (STATS_FIXTURE).
        self.assertIn(
            '<div class="cds-progress cds-progress--thick" role="progressbar" '
            'aria-valuemin="0" aria-valuemax="100" aria-valuenow="46" '
            'aria-label="Automatic (SponsorsHelper)">',
            html,
        )
        self.assertIn(
            '<div class="cds-progress cds-progress--thick" role="progressbar" '
            'aria-valuemin="0" aria-valuemax="100" aria-valuenow="53" '
            'aria-label="Manual (browser userscript)">',
            html,
        )
        self.assertIn(
            '<div class="cds-progress cds-progress--thick" role="progressbar" '
            'aria-valuemin="0" aria-valuemax="100" aria-valuenow="16" '
            'aria-label="Tested">',
            html,
        )

    def test_captcha_share_bars_safe_at_zero(self):
        """A fresh install (both CAPTCHA counters at 0) must never divide by
        zero computing the two bars' fill share, and both bars must still
        render real, count-consistent zero state - a zero-width fill, a
        zero `aria-valuenow`, and an actual "0" value in the head row -
        rather than merely not crashing.
        """
        stats = dict(STATS_FIXTURE)
        stats["captcha_decryptions_automatic"] = 0
        stats["captcha_decryptions_manual"] = 0
        html = self._render(stats=stats)
        self.assertEqual(html.count('style="width:0%"'), 2)
        self.assertEqual(html.count('aria-valuenow="0"'), 2)
        self.assertIn(
            '<div class="cds-bar__head"><span>Automatic (SponsorsHelper)</span>'
            "<strong>0</strong></div>",
            html,
        )
        self.assertIn(
            '<div class="cds-bar__head"><span>Manual (browser userscript)</span>'
            "<strong>0</strong></div>",
            html,
        )

    def test_four_top_kpi_tiles(self):
        html = self._render()
        self.assertEqual(html.count("cds-tile--is-metric"), 4)

    def test_four_metric_tiles_with_large_values(self):
        html = self._render()
        self.assertEqual(html.count("cds-tile--is-metric"), 4)
        self.assertIn('<h2 class="cds-tile__heading">Download attempts</h2>', html)
        self.assertIn('<p class="cds-metric__value">10,234</p>', html)
        self.assertIn(
            '<p class="cds-metric__sub cds-metric__sub--success">'
            "91.2% success rate</p>",
            html,
        )

    def test_detail_tiles_replace_tables(self):
        """The five detail tiles used to split across a ragged 2-column-
        plus-3-column pair (a hole under the short CAPTCHA tile whenever a
        row didn't fill evenly); they now all sit inside one self-arranging
        `cds-grid--auto` grid instead, so the headings are asserted both
        present AND in their original left-to-right/top-to-bottom order
        inside that one container - not merely present anywhere on the
        page, since "CAPTCHA decryptions" is also a top KPI tile heading
        that renders earlier in the document.
        """
        html = self._render()
        self.assertNotIn("<table", html)
        self.assertNotIn("cds-grid--2", html)
        self.assertNotIn("cds-grid--3", html)
        grid_start = html.index('<div class="cds-grid--auto">')
        grid_html = html[grid_start : html.index("</main>", grid_start)]

        headings = (
            "CAPTCHA decryptions",
            "Cached metadata",
            "Linkcrypter blocks",
            "Filecrypt cohort",
            "Terminal operations",
        )
        positions = []
        for heading in headings:
            with self.subTest(heading=heading):
                marker = f'<h2 class="cds-tile__heading">{heading}</h2>'
                self.assertIn(marker, grid_html)
                positions.append(grid_html.index(marker))
        self.assertEqual(positions, sorted(positions))

        # automatic, manual, cohort tested - the thick modifier class always
        # rides alongside the base class, matching `_bar()`'s literal markup.
        self.assertEqual(html.count('class="cds-progress cds-progress--thick"'), 3)

    def test_kpi_tiles_wrapped_in_kpi_row(self):
        """The four top KPI tiles render inside one `.cds-kpi-row` wrapper,
        so carbon.css's grid layout applies to them instead of the default
        vertical tile stack. Each tile itself nests a `.cds-tile__content`
        div, so the wrapper's own close tag is found by slicing up to the
        immediately following detail-tile grid rather than matching the
        first (nested) `</div>`.
        """
        html = self._render()
        start = html.index('<div class="cds-kpi-row">')
        end = html.index('<div class="cds-grid--auto">', start)
        row_html = html[start:end]

        self.assertTrue(row_html.endswith("</div>"))
        self.assertEqual(row_html.count("cds-tile--is-metric"), 4)

    def test_no_renderer_owned_emoji(self):
        html = self._render()
        self.assertIsNone(re.search("[\U0001f300-\U0001faff☀-➿]", html))

    def test_no_identifier_shaped_lifecycle_data(self):
        html = self._render()
        self.assertIsNone(re.search(r"\b[0-9a-f]{64}\b", html))
        self.assertIsNone(re.search(r"\b[0-9a-f]{32}\b", html))

    def test_no_remote_resources_or_forbidden_labels(self):
        html = self._render()
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        for forbidden in (
            "sweep_id",
            "offer_id",
            "fingerprint",
            "operation_id",
            "package_id",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, html)

    def test_structural_guards_pass(self):
        templates = importlib.import_module("quasarr.providers.carbon_templates")
        html = self._render()
        # render_carbon_html already runs this at the end of construction; a
        # second, explicit pass here documents the contract for this view.
        templates._assert_structural_guards(html)


class CarbonStatisticsRouteDispatchTests(unittest.TestCase):
    """Proves the lazy `carbon()` import inside setup_statistics resolves here."""

    def _request(self, app, path="/statistics"):
        environ = {
            "REQUEST_METHOD": "GET",
            "PATH_INFO": path,
            "QUERY_STRING": "",
            "SERVER_NAME": "localhost",
            "SERVER_PORT": "8080",
            "SERVER_PROTOCOL": "HTTP/1.1",
            "HTTP_HOST": "localhost:8080",
            "wsgi.url_scheme": "http",
            "wsgi.input": BytesIO(b""),
            "wsgi.errors": BytesIO(),
        }
        captured = {}

        def start_response(status, response_headers, exc_info=None):
            captured["status"] = status
            captured["headers"] = list(response_headers)

        body = b"".join(app(environ, start_response))
        return captured.get("status", "500"), captured.get("headers", []), body

    def test_statistics_route_renders_carbon_when_active(self):
        from quasarr.api.statistics import setup_statistics

        carbon_mod = importlib.import_module("quasarr.api.statistics.carbon")
        app = Bottle()
        shared_state = _FakeSharedState()
        setup_statistics(app, shared_state)

        with (
            mock.patch.dict(os.environ, {"QUASARR_UI": "carbon"}, clear=True),
            mock.patch(
                "quasarr.providers.page_dispatch.carbon_assets_available",
                return_value=True,
            ),
            mock.patch.object(carbon_mod, "StatsHelper") as helper_cls,
            mock.patch.object(carbon_mod, "show_logout_link", return_value=True),
        ):
            helper_cls.return_value.get_stats.return_value = dict(STATS_FIXTURE)
            status, _headers, body = self._request(app)

        self.assertEqual(status.split()[0], "200")
        html = body.decode("utf-8")
        self.assertIn("Filecrypt cohort", html)
        self.assertIn("<title>Statistics</title>", html)


if __name__ == "__main__":
    unittest.main()
