import unittest
from contextlib import ExitStack
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch
from wsgiref.util import setup_testing_defaults

from bottle import Bottle

from quasarr.api.arr import setup_arr_routes
from quasarr.constants import (
    SEARCH_CAT_BOOKS,
    SEARCH_CAT_MOVIES,
    SEARCH_CAT_MOVIES_UHD,
    SEARCH_CAT_SHOWS,
)
from quasarr.providers.utils import is_site_usable
from quasarr.search import SearchCache, get_search_results
from quasarr.search.runtime import SearchRuntime
from quasarr.search.singleflight import SearchSingleFlight
from quasarr.storage.setup.arr import (
    _arr_client_selection_form_html,
    missing_arr_client_requirement,
    split_arr_required_sites,
)
from quasarr.storage.setup.radarr import (
    _configured_required_sites as radarr_required_sites,
)
from quasarr.storage.setup.radarr import _radarr_setup_form_html, is_radarr_skipped
from quasarr.storage.setup.sonarr import (
    _configured_required_sites as sonarr_required_sites,
)
from quasarr.storage.setup.sonarr import _sonarr_setup_form_html, is_sonarr_skipped


class SearchArrRequirementTests(unittest.TestCase):
    def setUp(self):
        self._patches = ExitStack()
        self.addCleanup(self._patches.close)
        self._patches.enter_context(
            patch("quasarr.search.search_singleflight", SearchSingleFlight())
        )

    @staticmethod
    def _state(**clients):
        return SimpleNamespace(
            values={
                "config": lambda _section: {},
                **clients,
            }
        )

    def test_movie_search_stops_without_radarr(self):
        with patch("quasarr.search.error") as log_error:
            results = get_search_results(
                self._state(),
                "radarr",
                SEARCH_CAT_MOVIES,
                imdb_id="tt0000010",
            )

        self.assertEqual([], results)
        log_error.assert_called_once_with(
            "Movie search unavailable: Radarr is not configured"
        )

    def test_tv_feed_stops_without_sonarr(self):
        with patch("quasarr.search.error") as log_error:
            results = get_search_results(self._state(), "sonarr", SEARCH_CAT_SHOWS)

        self.assertEqual([], results)
        log_error.assert_called_once_with(
            "TV search unavailable: Sonarr is not configured"
        )

    def test_book_phrase_search_does_not_require_arr_client(self):
        with (
            patch("quasarr.search.get_sources", return_value={}),
            patch("quasarr.search.get_search_category_sources", return_value=[]),
            patch("quasarr.search.error") as log_error,
        ):
            results = get_search_results(
                self._state(),
                "magazarr",
                SEARCH_CAT_BOOKS,
                search_phrase="Synthetic Author",
            )

        self.assertEqual([], results)
        log_error.assert_not_called()

    def test_movie_search_warms_metadata_from_radarr(self):
        state = self._state(radarr_client=object())
        with (
            patch("quasarr.search.get_sources", return_value={}),
            patch("quasarr.search.get_search_category_sources", return_value=[]),
            patch("quasarr.search.get_imdb_metadata") as metadata_lookup,
        ):
            get_search_results(
                state,
                "radarr",
                SEARCH_CAT_MOVIES,
                imdb_id="tt0000011",
            )

        metadata_lookup.assert_called_once_with(state, "tt0000011", SEARCH_CAT_MOVIES)

    def test_multi_category_request_tracks_one_cache_family_without_extra_fanout(self):
        class State:
            pass

        class MovieSource:
            initials = "aa"
            supported_categories = [SEARCH_CAT_MOVIES]
            supports_imdb = True
            calls = 0

            def search(self, *_args, **_kwargs):
                self.calls += 1
                return [
                    {
                        "details": {
                            "title": "Synthetic.Movie.1080p",
                            "date": "Thu, 01 Jan 2026 00:00:00 +0000",
                            "link": "https://downloads.invalid/movie-hd",
                            "source": "Synthetic source",
                            "hostname": "AA",
                            "size": 1,
                        }
                    },
                    {
                        "details": {
                            "title": "Synthetic.Movie.2160p",
                            "date": "Thu, 01 Jan 2026 00:00:00 +0000",
                            "link": "https://downloads.invalid/movie-uhd",
                            "source": "Synthetic source",
                            "hostname": "AA",
                            "size": 1,
                        }
                    },
                ]

        app = Bottle()
        setup_arr_routes(app)
        source = MovieSource()
        runtime = SearchRuntime(memory_reader=lambda: {})
        state = State()
        state.values = {
            "radarr_client": object(),
            "config": lambda section: (
                {"aa": "source.invalid"} if section == "Hostnames" else {}
            ),
        }
        calls = []

        def run_search(*args, **kwargs):
            results = get_search_results(*args, **kwargs)
            calls.append(
                (
                    args[2],
                    [release["details"]["title"] for release in results],
                )
            )
            return results

        with (
            patch("quasarr.api.arr.shared_state", state),
            patch("quasarr.api.arr.get_search_results", side_effect=run_search),
            patch("quasarr.api.arr.search_runtime", runtime),
            patch("quasarr.api.arr.debug") as log_debug,
            patch("quasarr.api.arr.info") as log_info,
            patch("quasarr.search.get_sources", return_value={"aa": source}),
            patch("quasarr.search.get_search_category_sources", return_value=[]),
            patch("quasarr.search.get_imdb_metadata"),
            patch("quasarr.search.search_cache", SearchCache()),
            patch("quasarr.search.search_runtime", runtime),
        ):
            status, body = self._call_app(
                app,
                "t=movie&cat=2045,2000&imdbid=tt0000012",
                user_agent="Radarr/5.0",
            )

        self.assertEqual("200 OK", status)
        self.assertEqual(
            [SEARCH_CAT_MOVIES, SEARCH_CAT_MOVIES_UHD],
            [category_id for category_id, _ in calls],
        )
        self.assertEqual(
            ["Synthetic.Movie.1080p", "Synthetic.Movie.2160p"], calls[0][1]
        )
        self.assertEqual(["Synthetic.Movie.2160p"], calls[1][1])
        self.assertEqual(1, source.calls)
        self.assertIn("Synthetic.Movie.1080p", body)
        self.assertIn("Synthetic.Movie.2160p", body)

        snapshot = runtime.snapshot()
        self.assertEqual(0, snapshot["active_requests"])
        self.assertEqual(1, snapshot["requests_started"])
        self.assertEqual(1, snapshot["requests_completed"])
        self.assertEqual(2, snapshot["categories_planned"])
        self.assertEqual(1, snapshot["families_planned"])

        debug_summary_calls = [
            call
            for call in log_debug.call_args_list
            if call.args and call.args[0].startswith("Search runtime summary: ")
        ]
        info_summary_calls = [
            call
            for call in log_info.call_args_list
            if call.args and call.args[0].startswith("Search runtime summary: ")
        ]
        self.assertEqual((1, 0), (len(debug_summary_calls), len(info_summary_calls)))
        summary = debug_summary_calls[0].args[0]
        self.assertNotIn("tt0000012", summary)
        self.assertNotIn("2000", summary)
        self.assertNotIn("2045", summary)
        self.assertNotIn("AA", summary)

    def test_setup_forms_do_not_offer_arr_skip_loopholes(self):
        with (
            patch("quasarr.storage.setup.radarr.Config", return_value={}),
            patch("quasarr.storage.setup.sonarr.Config", return_value={}),
            patch("quasarr.storage.setup.radarr.DataBase") as radarr_database,
            patch("quasarr.storage.setup.sonarr.DataBase") as sonarr_database,
        ):
            radarr_html = _radarr_setup_form_html(["aa"])
            sonarr_html = _sonarr_setup_form_html(["bb"])
            radarr_database.return_value.retrieve.return_value = None
            sonarr_database.return_value.retrieve.return_value = None

            self.assertNotIn("skip", radarr_html.lower())
            self.assertNotIn("skip", sonarr_html.lower())
            self.assertEqual(2, radarr_html.count(" required"))
            self.assertEqual(2, sonarr_html.count(" required"))
            self.assertFalse(is_radarr_skipped())
            self.assertFalse(is_sonarr_skipped())

    def test_arr_skip_preferences_are_preserved(self):
        with (
            patch("quasarr.storage.setup.radarr.DataBase") as radarr_database,
            patch("quasarr.storage.setup.sonarr.DataBase") as sonarr_database,
        ):
            radarr_database.return_value.retrieve.return_value = "true"
            sonarr_database.return_value.retrieve.return_value = "true"

            self.assertTrue(is_radarr_skipped())
            self.assertTrue(is_sonarr_skipped())

    def test_dual_category_setup_lets_user_choose_one_arr_client(self):
        html = _arr_client_selection_form_html(["aa"], ["bb"])

        self.assertIn('value="radarr"', html)
        self.assertIn('value="sonarr"', html)
        self.assertIn("does not require both", html)

    def test_arr_setup_keeps_exclusive_and_dual_source_requirements_separate(self):
        radarr_only, sonarr_only, dual_category = split_arr_required_sites(
            ["movie", "both"], ["tv", "both"]
        )

        self.assertEqual({"movie"}, radarr_only)
        self.assertEqual({"tv"}, sonarr_only)
        self.assertEqual({"both"}, dual_category)

    def test_dual_category_source_accepts_either_arr_client(self):
        radarr_required = {"movie", "both"}
        sonarr_required = {"tv", "both"}

        self.assertIsNone(
            missing_arr_client_requirement(
                "both", radarr_required, sonarr_required, True, False
            )
        )
        self.assertIsNone(
            missing_arr_client_requirement(
                "both", radarr_required, sonarr_required, False, True
            )
        )
        self.assertEqual(
            "Sonarr",
            missing_arr_client_requirement(
                "tv", radarr_required, sonarr_required, True, False
            ),
        )

    def test_dual_category_source_is_usable_with_either_arr_client(self):
        state = SimpleNamespace(
            values={
                "config": lambda section: (
                    {"both": "both.invalid"} if section == "Hostnames" else {}
                ),
            }
        )
        with (
            patch(
                "quasarr.providers.utils.get_radarr_required_hostnames",
                return_value=["both"],
            ),
            patch(
                "quasarr.providers.utils.get_sonarr_required_hostnames",
                return_value=["both"],
            ),
            patch(
                "quasarr.providers.utils.get_login_required_hostnames",
                return_value=[],
            ),
        ):
            state.values["radarr_client"] = object()
            self.assertTrue(is_site_usable(state, "both"))

            state.values.pop("radarr_client")
            state.values["sonarr_client"] = object()
            self.assertTrue(is_site_usable(state, "both"))

            state.values.pop("sonarr_client")
            self.assertFalse(is_site_usable(state, "both"))

    def test_dual_category_source_does_not_block_clearing_other_arr_client(self):
        state = self._state(radarr_client=object(), sonarr_client=object())
        with (
            patch(
                "quasarr.storage.setup.radarr.Config",
                return_value={"both": "both.invalid"},
            ),
            patch(
                "quasarr.storage.setup.sonarr.Config",
                return_value={"both": "both.invalid"},
            ),
            patch(
                "quasarr.search.sources.helpers.get_radarr_required_hostnames",
                return_value=["both"],
            ),
            patch(
                "quasarr.search.sources.helpers.get_sonarr_required_hostnames",
                return_value=["both"],
            ),
        ):
            self.assertEqual(set(), radarr_required_sites(state))
            self.assertEqual(set(), sonarr_required_sites(state))

    @staticmethod
    def _call_app(app, query_string, user_agent=""):
        environ = {}
        setup_testing_defaults(environ)
        environ.update(
            {
                "REQUEST_METHOD": "GET",
                "PATH_INFO": "/api",
                "QUERY_STRING": query_string,
                "HTTP_USER_AGENT": user_agent,
                "wsgi.input": BytesIO(b""),
            }
        )
        captured = {}

        def start_response(status, headers, exc_info=None):
            captured["status"] = status
            captured["headers"] = headers

        body = b"".join(app(environ, start_response)).decode("utf-8")
        return captured["status"], body


if __name__ == "__main__":
    unittest.main()
