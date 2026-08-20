# -*- coding: utf-8 -*-

"""Contracts for the Carbon statistics view.

Pins the 37-value coverage of ``StatsHelper.get_stats()`` against the Carbon
renderer, the two pinned headings ("Filecrypt cohort", "Terminal
operations"), lifecycle text states rendered as tags, the tested/total
fraction, the ``<time data-epoch>`` deadline contract, and the structural/
privacy guards (no emoji, no identifier-shaped lifecycle data, no remote
resources, no inline handlers/scripts).
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
            STATS_FIXTURE["crypter_sweep_state"],
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

    def test_lifecycle_states_rendered_as_tags(self):
        html = self._render()
        self.assertIn('class="cds-tag', html)
        self.assertRegex(html, r'<span class="cds-tag[^"]*">sweeping</span>')
        self.assertRegex(html, r'<span class="cds-tag[^"]*">cohort_oversized</span>')

    def test_individual_mode_falls_back_to_none(self):
        stats = dict(STATS_FIXTURE)
        stats["crypter_individual_mode"] = ""
        html = self._render(stats=stats)
        self.assertRegex(html, r'<span class="cds-tag[^"]*">None</span>')

    def test_zero_deadline_epoch_has_no_epoch_time_element(self):
        stats = dict(STATS_FIXTURE)
        stats["crypter_sweep_deadline_epoch"] = 0
        html = self._render(stats=stats)
        self.assertNotIn('data-epoch="0"', html)
        self.assertNotIn("1970", html)

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
        self.assertIn("<strong>2</strong>", html)

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
        self.assertIn("<strong>0</strong>", html)

    def test_captcha_ratio_bars_present_with_visible_text(self):
        html = self._render()
        self.assertEqual(html.count('class="cds-progress"'), 2)
        self.assertEqual(html.count('role="progressbar"'), 2)
        # Automatic: 77.1%, Manual: 66.3% (STATS_FIXTURE) - both the visible
        # text and the bar width must reflect the unclamped provider value.
        self.assertRegex(
            html,
            r"<span>77\.1%</span>"
            r'<div class="cds-progress" role="progressbar" aria-valuemin="0" '
            r'aria-valuemax="100" aria-valuenow="77\.1" '
            r'aria-label="Automatic success rate 77\.1%">'
            r'<span style="width:77\.1%"></span></div>',
        )
        self.assertRegex(
            html,
            r"<span>66\.3%</span>"
            r'<div class="cds-progress" role="progressbar" aria-valuemin="0" '
            r'aria-valuemax="100" aria-valuenow="66\.3" '
            r'aria-label="Manual success rate 66\.3%">'
            r'<span style="width:66\.3%"></span></div>',
        )

    def test_ratio_bar_width_clamped_to_0_100(self):
        stats = dict(STATS_FIXTURE)
        stats["automatic_decryption_success_rate"] = 150.0
        stats["manual_decryption_success_rate"] = -12.0
        html = self._render(stats=stats)
        self.assertIn('style="width:100.0%"', html)
        self.assertIn('style="width:0.0%"', html)
        # The displayed text is not silently rewritten by the clamp.
        self.assertIn("<span>150.0%</span>", html)
        self.assertIn("<span>-12.0%</span>", html)
        self.assertNotIn('style="width:150.0%"', html)
        self.assertNotIn('style="width:-12.0%"', html)

    def test_kv_band_markup_matches_data_table_structure(self):
        """The raw-HTML-carrying bands (CAPTCHAs, Filecrypt cohort) must
        render byte-identical wrapper/caption/header/cell markup to the
        plain ``_band()`` groups, which delegate straight to the real,
        already-tested ``data_table()`` component - only the value cell's
        escaping differs, and only because it carries pre-built safe HTML.
        """
        templates = importlib.import_module("quasarr.providers.carbon_templates")
        columns = (
            templates.TableColumn("metric", "Metric"),
            templates.TableColumn("value", "Value", classes="is-num is-mono"),
        )
        rows = [("Alpha", "1,234"), ("Beta", "5,678")]
        expected = templates.data_table(
            columns,
            [{"metric": label, "value": value} for label, value in rows],
            caption="Sample",
        )
        actual = self.mod._kv_band("Sample", rows)
        self.assertEqual(expected, actual)

    def test_four_top_kpi_tiles(self):
        html = self._render()
        self.assertEqual(html.count("cds-tile--is-metric"), 4)

    def test_kpi_tiles_wrapped_in_kpi_row(self):
        """The four top KPI tiles render inside one `.cds-kpi-row` wrapper,
        so carbon.css's grid layout applies to them
        instead of the default vertical tile stack. Each tile itself nests a
        `.cds-tile__content` div, so the wrapper's own close tag is found by
        slicing up to the immediately following Downloads band rather than
        matching the first (nested) `</div>`.
        """
        html = self._render()
        start = html.index('<div class="cds-kpi-row">')
        end = html.index('<div class="cds-table-wrap">', start)
        row_html = html[start:end]

        self.assertTrue(row_html.endswith("</div>"))
        self.assertEqual(row_html.count("cds-tile--is-metric"), 4)
        # The row closes before the first banded table caption follows.
        self.assertLess(end, html.index("<caption>Downloads</caption>"))

    def test_six_grouped_bands(self):
        html = self._render()
        self.assertEqual(html.count("<caption>"), 6)
        for caption in (
            "Downloads",
            "CAPTCHAs",
            "Linkcrypter blocks",
            "Filecrypt cohort",
            "Terminal operations",
            "Cached metadata",
        ):
            with self.subTest(caption=caption):
                self.assertIn(f"<caption>{caption}</caption>", html)

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
