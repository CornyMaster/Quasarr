import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from quasarr.providers import imdb_metadata
from quasarr.search.sources.helpers.budget import use_search_budget


class ManualClock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


class LocalizedTitleDeadlineTests(unittest.TestCase):
    def test_direct_html_timeout_is_clamped_to_the_explicit_deadline(self):
        response = SimpleNamespace(status_code=200, text="<html></html>")
        with (
            patch.object(imdb_metadata.requests, "get", return_value=response) as get,
            patch.object(
                imdb_metadata.IMDbHTML,
                "_parse_localized_title",
                return_value="Synthetic Title",
            ),
            patch.object(imdb_metadata.time, "time", return_value=9.75),
        ):
            imdb_metadata.IMDbHTML._request(
                "https://metadata.invalid/releaseinfo/", "fr", deadline=10.0
            )

        self.assertEqual(0.25, get.call_args.kwargs["timeout"])

    def test_direct_html_timeout_uses_the_stricter_worker_budget(self):
        response = SimpleNamespace(status_code=200, text="<html></html>")
        with (
            patch.object(imdb_metadata.requests, "get", return_value=response) as get,
            patch.object(
                imdb_metadata.IMDbHTML,
                "_parse_localized_title",
                return_value="Synthetic Title",
            ),
            patch.object(imdb_metadata.time, "time", return_value=0.0),
            use_search_budget(0.2, clock=ManualClock()),
        ):
            imdb_metadata.IMDbHTML._request(
                "https://metadata.invalid/releaseinfo/", "fr", deadline=10.0
            )

        self.assertEqual(0.2, get.call_args.kwargs["timeout"])

    def test_expired_deadline_skips_the_network_fallbacks(self):
        # The IMDb HTML request allows 30s and the FlareSolverr fallback 70s, so
        # a caller under a wall-clock budget must not enter them once its time
        # is up - the work would land long after it stopped waiting.
        with (
            patch.object(imdb_metadata, "_get_cached_metadata", return_value=None),
            patch.object(imdb_metadata, "_refresh_imdb_metadata") as refresh,
            patch.object(imdb_metadata.IMDbHTML, "get_localized_title") as html,
        ):
            title = imdb_metadata.get_localized_title(
                None, "tt0000001", "fr", None, deadline=time.time() - 1
            )

        self.assertIsNone(title)
        refresh.assert_not_called()
        html.assert_not_called()

    def test_a_fresh_cache_answers_even_when_out_of_time(self):
        cached = {
            "ttl": time.time() + 600,
            "localized": {"fr": "Un Titre"},
        }
        with (
            patch.object(imdb_metadata, "_get_cached_metadata", return_value=cached),
            patch.object(imdb_metadata.IMDbHTML, "get_localized_title") as html,
        ):
            title = imdb_metadata.get_localized_title(
                None, "tt0000001", "fr", None, deadline=time.time() - 1
            )

        self.assertEqual("Un Titre", title)
        html.assert_not_called()

    def test_the_deadline_is_rechecked_after_the_arr_refresh(self):
        # The Arr refresh costs a request of its own, so a budget that was intact
        # on entry can be gone before the far more expensive IMDb fallbacks would
        # start. Checking only once on entry would let them run anyway: the clock
        # here is inside the budget at the first gate and past it at the second.
        with (
            patch.object(imdb_metadata, "_get_cached_metadata", return_value=None),
            patch.object(
                imdb_metadata, "_refresh_imdb_metadata", return_value=({}, {})
            ) as refresh,
            patch.object(imdb_metadata.IMDbHTML, "get_localized_title") as html,
            patch.object(imdb_metadata.time, "time", side_effect=[0, 100]),
        ):
            title = imdb_metadata.get_localized_title(
                None, "tt0000001", "fr", None, deadline=10
            )

        self.assertIsNone(title)
        refresh.assert_called_once()
        html.assert_not_called()

    def test_the_flaresolverr_fallback_is_skipped_when_time_ran_out(self):
        # The direct request can consume what was left. FlareSolverr allows 60s
        # of its own on top, which is the stage worth giving up rather than
        # starting once the caller has stopped waiting.
        def slow_direct(*_args, **_kwargs):
            raise RuntimeError("IMDb unreachable")

        with (
            patch("quasarr.providers.imdb_metadata.requests.get", slow_direct),
            patch("quasarr.providers.imdb_metadata.requests.post") as post,
            patch.object(imdb_metadata.time, "time", return_value=100),
        ):
            html = imdb_metadata.IMDbHTML._request(
                "https://www.imdb.com/fr/title/tt0000001/releaseinfo/",
                "fr",
                deadline=10,
            )

        self.assertIsNone(html)
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
