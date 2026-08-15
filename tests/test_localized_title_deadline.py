import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from quasarr.providers import imdb_metadata
from quasarr.search.sources.helpers.budget import (
    SearchBudgetExhausted,
    use_search_budget,
)


class ManualClock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


class ClockSpentAfter:
    """Wall clock for a budget that runs out after a known number of reads.

    `IMDbHTML._request` reads the budget twice for every timeout it computes -
    once for `checkpoint()`, once for `clamp_timeout()`. Four reads therefore
    carry the direct request and the solver's own maxTimeout, and the fifth is
    the POST timeout, which is the read that used to sit inside the solver's
    `try` and be reported as a solver failure.
    """

    def __init__(self, reads_before_spent, deadline):
        self._reads_before_spent = reads_before_spent
        self._deadline = deadline
        self.reads = 0

    def __call__(self):
        self.reads += 1
        return 0.0 if self.reads <= self._reads_before_spent else self._deadline


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

    def test_the_flaresolverr_fallback_does_not_log_a_spent_budget_as_a_failure(self):
        # Refusing to start a solve because the worker is out of time is the
        # budget stopping the source, not FlareSolverr failing: swallowing it
        # here hides the deadline and mislabels the host.
        clock = ClockSpentAfter(4, deadline=10.0)
        with (
            patch.object(
                imdb_metadata.requests, "get", side_effect=RuntimeError("unreachable")
            ),
            patch.object(imdb_metadata.requests, "post") as post,
            patch.object(
                imdb_metadata,
                "_get_config",
                return_value={"url": "https://solver.invalid"},
            ),
            patch.object(
                imdb_metadata,
                "_get_db",
                return_value=SimpleNamespace(retrieve=lambda _key: None),
            ),
            patch.object(imdb_metadata, "debug") as logged,
            use_search_budget(10.0, clock=clock),
        ):
            with self.assertRaises(SearchBudgetExhausted):
                imdb_metadata.IMDbHTML._request(
                    "https://metadata.invalid/releaseinfo/", "fr"
                )

        post.assert_not_called()
        self.assertEqual(
            [],
            [
                call
                for call in logged.call_args_list
                if "FlareSolverr request failed" in str(call)
            ],
        )


if __name__ == "__main__":
    unittest.main()
