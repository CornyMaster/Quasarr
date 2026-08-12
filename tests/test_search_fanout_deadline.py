import time
import unittest
from threading import Barrier, Event, get_ident
from types import SimpleNamespace
from unittest.mock import patch

from quasarr.constants import SEARCH_CAT_MOVIES
from quasarr.search import SearchCache, SearchExecutor, get_search_results
from quasarr.search.runtime import SearchRuntime


class FakeSource:
    def __init__(self, initials, func):
        self.initials = initials
        self._func = func

    def search(self, *args, **kwargs):
        return self._func()


class ThreadScopedClock:
    """Deterministic stand-in for time.time() across the fan-out boundary.

    The fan-out thread always reads a time before the deadline and a source
    worker always reads one after it, so "finished too late" is decided by the
    code under test rather than by how fast the machine happens to be.
    """

    def __init__(self, fanout_time, worker_time):
        self._fanout_thread = get_ident()
        self._fanout_time = fanout_time
        self._worker_time = worker_time

    def __call__(self):
        if get_ident() == self._fanout_thread:
            return self._fanout_time
        return self._worker_time


class SearchFanoutDeadlineTests(unittest.TestCase):
    def test_slow_source_is_dropped_instead_of_stalling_the_response(self):
        # A source that outlives the fan-out deadline must not hold up the
        # response: *arr clients disable an indexer that answers too late.
        release = Event()
        self.addCleanup(release.set)
        overdue_resolved = Event()
        runtime = SearchRuntime(memory_reader=lambda: {})
        cache = SearchCache()
        resolved_tokens = []
        resolve_source_overdue = runtime.resolve_source_overdue

        def resolve_overdue(token):
            resolved_tokens.append(token)
            resolved = resolve_source_overdue(token)
            overdue_resolved.set()
            return resolved

        runtime.resolve_source_overdue = resolve_overdue

        def fast():
            return [{"details": {"title": "Fast.Release"}}]

        def slow():
            release.wait(30)
            return [{"details": {"title": "Slow.Release"}}]

        executor = SearchExecutor(deadline=time.time() + 0.5)
        executor.add(FakeSource("fa", fast), (None, 0.0, 2000), {})
        executor.add(FakeSource("sl", slow), (None, 0.0, 2000), {}, use_cache=True)

        with (
            patch("quasarr.search.search_runtime", runtime, create=True),
            patch("quasarr.search.search_cache", cache),
        ):
            started = time.time()
            results, bar, _, _ = executor.run_all()
            elapsed = time.time() - started

            self.assertEqual(1, runtime.snapshot()["overdue_source_tasks"])

        release.set()
        self.assertTrue(overdue_resolved.wait(10))

        self.assertLess(elapsed, 10)
        self.assertEqual(["Fast.Release"], [r["details"]["title"] for r in results])
        self.assertIn("FA", bar)
        self.assertIn("SL", bar)
        snapshot = runtime.snapshot()
        self.assertEqual(0, snapshot["active_source_tasks"])
        self.assertEqual(0, snapshot["overdue_source_tasks"])
        self.assertEqual(1, snapshot["source_completed"])
        self.assertEqual(1, snapshot["source_dropped"])
        self.assertEqual(1, len(resolved_tokens))
        self.assertEqual({}, cache.cache)

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
        runtime = SearchRuntime(memory_reader=lambda: {})

        with patch("quasarr.search.search_runtime", runtime, create=True):
            results, bar, _, _ = executor.run_all()

        self.assertEqual([], started)
        self.assertEqual([], results)
        self.assertIn("SL", bar)
        snapshot = runtime.snapshot()
        self.assertEqual(0, snapshot["source_started"])
        self.assertEqual(1, snapshot["source_skipped"])

    def test_source_error_is_recorded_after_its_task_scope_closes(self):
        def fail():
            raise RuntimeError("synthetic source failure")

        executor = SearchExecutor()
        executor.add(FakeSource("er", fail), (None, 0.0, 2000), {})
        runtime = SearchRuntime(memory_reader=lambda: {})

        with patch("quasarr.search.search_runtime", runtime, create=True):
            results, bar, _, _ = executor.run_all()

        self.assertEqual([], results)
        self.assertIn("ER", bar)
        snapshot = runtime.snapshot()
        self.assertEqual(0, snapshot["active_source_tasks"])
        self.assertEqual(1, snapshot["source_started"])
        self.assertEqual(1, snapshot["source_errored"])

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


class SearchCacheEligibilityTests(unittest.TestCase):
    """Only an ordinary, complete release list produced at or before the
    deadline may be written to the cache."""

    def setUp(self):
        self.runtime = SearchRuntime(memory_reader=lambda: {})
        self.cache = SearchCache()

    def run_one(self, source, deadline, clock=time.time):
        executor = SearchExecutor(deadline=deadline, clock=clock)
        executor.add(source, (None, 0.0, 2000), {}, use_cache=True)
        with (
            patch("quasarr.search.search_runtime", self.runtime, create=True),
            patch("quasarr.search.search_cache", self.cache),
        ):
            return executor.run_all()

    def test_a_result_finished_after_the_deadline_is_never_cached(self):
        # as_completed keeps a 0.1s floor under its timeout, so a source that
        # lands just past the deadline is still collected. Answering with it is
        # fine; caching it would keep serving a result the deadline had already
        # given up on for the whole TTL.
        deadline = 1005.0
        clock = ThreadScopedClock(fanout_time=1000.0, worker_time=deadline + 1.0)

        results, bar, _, _ = self.run_one(
            FakeSource("lt", lambda: [{"details": {"title": "Late.Release"}}]),
            deadline,
            clock,
        )

        self.assertEqual({}, self.cache.cache)
        # Response semantics are unchanged: it was collected, so it is answered
        # with and counted as completed.
        self.assertEqual(["Late.Release"], [r["details"]["title"] for r in results])
        self.assertIn("LT", bar)
        self.assertEqual(1, self.runtime.snapshot()["source_completed"])

    def test_a_result_finished_on_the_deadline_is_still_cached(self):
        # The bound is inclusive: a source that lands exactly on the deadline
        # answered in time and must keep its entry.
        deadline = 1005.0
        clock = ThreadScopedClock(fanout_time=1000.0, worker_time=deadline)

        results, _, _, _ = self.run_one(
            FakeSource("ot", lambda: [{"details": {"title": "On.Time"}}]),
            deadline,
            clock,
        )

        self.assertEqual(["On.Time"], [r["details"]["title"] for r in results])
        self.assertEqual(1, len(self.cache.cache))

    def test_a_source_that_raises_is_not_cached(self):
        def fail():
            raise RuntimeError("synthetic source failure")

        results, _, _, _ = self.run_one(FakeSource("er", fail), time.time() + 5.0)

        self.assertEqual([], results)
        self.assertEqual({}, self.cache.cache)
        self.assertEqual(1, self.runtime.snapshot()["source_errored"])


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
