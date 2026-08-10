import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from quasarr.constants import SEARCH_CAT_SHOWS
from quasarr.search import get_search_results


class FakeSearchExecutor:
    latest = None

    def __init__(self, deadline=None):
        self.searches = []
        self.added = []
        self.deadline = deadline
        FakeSearchExecutor.latest = self

    def add(self, source, args, kwargs, **options):
        self.searches.append(source.initials)
        self.added.append((source, args, kwargs, options))

    def run_all(self):
        return [], "", False, 0


class DateNumberingDispatchTests(unittest.TestCase):
    @staticmethod
    def _sources_and_state():
        enabled = SimpleNamespace(
            initials="aa",
            supports_imdb=True,
            supports_absolute_numbering=False,
            supports_date_numbering=True,
            supported_categories=[SEARCH_CAT_SHOWS],
        )
        disabled = SimpleNamespace(
            initials="bb",
            supports_imdb=True,
            supports_absolute_numbering=False,
            supports_date_numbering=False,
            supported_categories=[SEARCH_CAT_SHOWS],
        )
        shared_state = SimpleNamespace(
            values={
                "sonarr_client": object(),
                "config": lambda _section: {
                    "aa": "aa.invalid",
                    "bb": "bb.invalid",
                },
            }
        )
        return enabled, disabled, shared_state

    def test_date_mode_clears_numeric_episode_fields_before_source_dispatch(self):
        enabled, disabled, shared_state = self._sources_and_state()

        with (
            patch(
                "quasarr.search.get_sources",
                return_value={"aa": enabled, "bb": disabled},
            ),
            patch("quasarr.search.get_imdb_metadata"),
            patch("quasarr.search.get_search_category_sources", return_value=[]),
            patch("quasarr.search.SearchExecutor", FakeSearchExecutor),
        ):
            results = get_search_results(
                shared_state,
                "sonarr",
                SEARCH_CAT_SHOWS,
                imdb_id="tt0000001",
                season=2031,
                episode="02/03",
            )

        self.assertEqual([], results)
        self.assertEqual(["aa"], FakeSearchExecutor.latest.searches)
        _source, _args, kwargs, _options = FakeSearchExecutor.latest.added[0]
        self.assertEqual(
            {
                "search_string": "tt0000001",
                "season": None,
                "episode": None,
                "episode_date": date(2031, 2, 3),
            },
            kwargs,
        )

    def test_normal_episode_mode_preserves_numeric_fields_for_every_source(self):
        enabled, disabled, shared_state = self._sources_and_state()

        with (
            patch(
                "quasarr.search.get_sources",
                return_value={"aa": enabled, "bb": disabled},
            ),
            patch("quasarr.search.get_imdb_metadata"),
            patch("quasarr.search.get_search_category_sources", return_value=[]),
            patch("quasarr.search.SearchExecutor", FakeSearchExecutor),
        ):
            results = get_search_results(
                shared_state,
                "sonarr",
                SEARCH_CAT_SHOWS,
                imdb_id="tt0000001",
                season=2,
                episode=7,
            )

        self.assertEqual([], results)
        self.assertEqual(["aa", "bb"], FakeSearchExecutor.latest.searches)
        for _source, _args, kwargs, _options in FakeSearchExecutor.latest.added:
            self.assertEqual(
                {
                    "search_string": "tt0000001",
                    "season": 2,
                    "episode": 7,
                },
                kwargs,
            )

    def test_invalid_date_shape_stays_on_normal_numbering_path(self):
        enabled, disabled, shared_state = self._sources_and_state()

        with (
            patch(
                "quasarr.search.get_sources",
                return_value={"aa": enabled, "bb": disabled},
            ),
            patch("quasarr.search.get_imdb_metadata"),
            patch("quasarr.search.get_search_category_sources", return_value=[]),
            patch("quasarr.search.SearchExecutor", FakeSearchExecutor),
        ):
            results = get_search_results(
                shared_state,
                "sonarr",
                SEARCH_CAT_SHOWS,
                imdb_id="tt0000001",
                season=2031,
                episode="02/30",
            )

        self.assertEqual([], results)
        self.assertEqual(["aa", "bb"], FakeSearchExecutor.latest.searches)
        for _source, _args, kwargs, _options in FakeSearchExecutor.latest.added:
            self.assertEqual(
                {
                    "search_string": "tt0000001",
                    "season": 2031,
                    "episode": "02/30",
                },
                kwargs,
            )


if __name__ == "__main__":
    unittest.main()
