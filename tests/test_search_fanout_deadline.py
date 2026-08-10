import time
import unittest
from threading import Barrier, Event
from types import SimpleNamespace
from unittest.mock import patch

from quasarr.constants import SEARCH_CAT_MOVIES
from quasarr.search import SearchExecutor, get_search_results


class FakeSource:
    def __init__(self, initials, func):
        self.initials = initials
        self._func = func

    def search(self, *args, **kwargs):
        return self._func()


class SearchFanoutDeadlineTests(unittest.TestCase):
    def test_slow_source_is_dropped_instead_of_stalling_the_response(self):
        # A source that outlives the fan-out deadline must not hold up the
        # response: *arr clients disable an indexer that answers too late.
        release = Event()
        self.addCleanup(release.set)

        def fast():
            return [{"details": {"title": "Fast.Release"}}]

        def slow():
            release.wait(30)
            return [{"details": {"title": "Slow.Release"}}]

        executor = SearchExecutor(deadline=time.time() + 0.5)
        executor.add(FakeSource("fa", fast), (None, 0.0, 2000), {})
        executor.add(FakeSource("sl", slow), (None, 0.0, 2000), {})

        started = time.time()
        results, bar, _, _ = executor.run_all()
        elapsed = time.time() - started

        self.assertLess(elapsed, 10)
        self.assertEqual(["Fast.Release"], [r["details"]["title"] for r in results])
        self.assertIn("FA", bar)
        self.assertIn("SL", bar)

    def test_every_source_within_the_deadline_is_collected(self):
        def make(title):
            return lambda: [{"details": {"title": title}}]

        executor = SearchExecutor()
        executor.add(FakeSource("aa", make("A")), (None, 0.0, 2000), {})
        executor.add(FakeSource("bb", make("B")), (None, 0.0, 2000), {})

        results, bar, _, _ = executor.run_all()

        self.assertEqual({"A", "B"}, {r["details"]["title"] for r in results})
        self.assertIn("AA", bar)
        self.assertIn("BB", bar)

    def test_a_shared_deadline_is_not_restarted_by_the_next_run(self):
        # A multi-category request runs cache-sharing categories one after
        # another. Each run must inherit the one deadline the request started
        # with, or two categories add up to twice the wait the *arr client
        # allows.
        release = Event()
        self.addCleanup(release.set)

        def slow():
            release.wait(30)
            return [{"details": {"title": "Slow.Release"}}]

        deadline = time.time() + 0.5
        started = time.time()
        for _ in range(2):
            executor = SearchExecutor(deadline=deadline)
            executor.add(FakeSource("sl", slow), (None, 0.0, 2000), {})
            results, _, _, _ = executor.run_all()
            self.assertEqual([], results)
        elapsed = time.time() - started

        # Two runs off one 0.5s deadline, not 0.5s each.
        self.assertLess(elapsed, 1.0)

    def test_an_expired_deadline_starts_no_work_at_all(self):
        # The first category can use up a shared deadline. Submitting anyway
        # would hit the source for a result this response can no longer use.
        started = []

        def never_wanted():
            started.append(True)
            return [{"details": {"title": "Too.Late"}}]

        executor = SearchExecutor(deadline=time.time() - 1)
        executor.add(FakeSource("sl", never_wanted), (None, 0.0, 2000), {})

        results, bar, _, _ = executor.run_all()

        self.assertEqual([], started)
        self.assertEqual([], results)
        self.assertIn("SL", bar)

    def test_every_source_gets_a_worker_regardless_of_cpu_count(self):
        # The default pool is sized from the CPU count, so on a small host the
        # sources dispatched last would wait in the queue and be dropped by the
        # deadline without ever having run. The barrier only clears if all of
        # them are running at the same time.
        count = 40
        barrier = Barrier(count, timeout=10)

        def blocked():
            barrier.wait()
            return [{"details": {"title": "ok"}}]

        executor = SearchExecutor()
        for index in range(count):
            executor.add(FakeSource(f"s{index}", blocked), (None, 0.0, 2000), {})

        results, _, _, _ = executor.run_all()

        self.assertEqual(count, len(results))


class MetadataWarmingDeadlineTests(unittest.TestCase):
    def test_an_expired_deadline_skips_imdb_metadata_warming(self):
        # A failed refresh is not cached, so every category of a multi-category
        # request would pay the Arr client timeout again, past the ceiling the
        # deadline is there to hold.
        state = SimpleNamespace(
            values={"config": lambda _section: {}, "radarr_client": object()}
        )

        with (
            patch("quasarr.search.get_imdb_metadata") as warm,
            patch("quasarr.search.get_search_category_sources", return_value=[]),
            patch("quasarr.search.get_sources", return_value={}),
        ):
            get_search_results(
                state,
                "radarr",
                SEARCH_CAT_MOVIES,
                imdb_id="tt0000010",
                deadline=time.time() - 1,
            )

        warm.assert_not_called()

    def test_metadata_warming_still_runs_inside_the_deadline(self):
        state = SimpleNamespace(
            values={"config": lambda _section: {}, "radarr_client": object()}
        )

        with (
            patch("quasarr.search.get_imdb_metadata") as warm,
            patch("quasarr.search.get_search_category_sources", return_value=[]),
            patch("quasarr.search.get_sources", return_value={}),
        ):
            get_search_results(
                state,
                "radarr",
                SEARCH_CAT_MOVIES,
                imdb_id="tt0000010",
                deadline=time.time() + 60,
            )

        warm.assert_called_once()


if __name__ == "__main__":
    unittest.main()
