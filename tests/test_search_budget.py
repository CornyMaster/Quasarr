import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from contextvars import copy_context
from threading import Lock, Thread, get_ident
from unittest.mock import patch

from quasarr.search import SearchCache, SearchExecutor
from quasarr.search.runtime import SearchRuntime
from quasarr.search.singleflight import SearchSingleFlight
from quasarr.search.sources.helpers.budget import (
    SearchBudget,
    SearchBudgetExhausted,
    checkpoint,
    clamp_timeout,
    current_budget,
    remaining_seconds,
    use_search_budget,
)


class FakeClock:
    """Wall clock a test moves by hand, so no assertion depends on speed."""

    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class FakeSource:
    def __init__(self, initials, func):
        self.initials = initials
        self._func = func

    def search(self, *args, **kwargs):
        return self._func()


class ScriptedWorkerClock:
    """Deterministic `time.time()` stand-in split by thread.

    The fan-out thread always reads a time inside the deadline; worker reads
    follow `worker_times` in order and then repeat the last entry. That lets a
    source observe an exhausted budget while its result is still stamped as
    finished in time, which is the only way to tell the budget gate apart from
    the finish-timestamp gate in one run.
    """

    def __init__(self, fanout_time, worker_times):
        self._fanout_thread = get_ident()
        self._fanout_time = fanout_time
        self._worker_times = list(worker_times)
        self._index = 0
        self._lock = Lock()

    def __call__(self):
        if get_ident() == self._fanout_thread:
            return self._fanout_time
        with self._lock:
            index = min(self._index, len(self._worker_times) - 1)
            self._index += 1
            return self._worker_times[index]


class SearchBudgetTests(unittest.TestCase):
    """The budget is one absolute deadline plus whether it ran out."""

    def test_remaining_seconds_counts_down_an_absolute_deadline(self):
        # The deadline is the same absolute wall-clock instant the fan-out
        # answers by, never a per-call duration - a source that starts late
        # must inherit the time that is actually left, not a fresh allowance.
        clock = FakeClock(100.0)

        with use_search_budget(160.0, clock=clock) as budget:
            self.assertIs(budget, current_budget())
            self.assertEqual(160.0, budget.deadline)
            self.assertEqual(60.0, remaining_seconds())
            clock.now = 159.5
            self.assertEqual(0.5, remaining_seconds())
            self.assertFalse(budget.exhausted)

    def test_a_passed_deadline_reports_no_time_and_marks_the_budget(self):
        clock = FakeClock(100.0)

        with use_search_budget(160.0, clock=clock) as budget:
            clock.now = 200.0
            self.assertEqual(0.0, remaining_seconds())
            self.assertTrue(budget.exhausted)

    def test_a_budget_standing_exactly_on_its_deadline_has_nothing_left(self):
        # Zero seconds left is spent: the next round trip could only finish
        # after the instant the response is due.
        budget = SearchBudget(160.0, clock=lambda: 160.0)

        self.assertEqual(0.0, budget.remaining())
        self.assertTrue(budget.exhausted)

    def test_the_exhausted_flag_cannot_be_set_from_outside(self):
        budget = SearchBudget(160.0, clock=lambda: 100.0)

        self.assertFalse(budget.exhausted)
        with self.assertRaises(AttributeError):
            budget.exhausted = True

    def test_clamp_timeout_keeps_a_default_that_still_fits(self):
        clock = FakeClock(100.0)

        with use_search_budget(160.0, clock=clock) as budget:
            self.assertEqual(15, clamp_timeout(15))
            self.assertFalse(budget.exhausted)

    def test_clamp_timeout_returns_the_time_that_is_actually_left(self):
        clock = FakeClock(100.0)

        with use_search_budget(102.5, clock=clock):
            self.assertEqual(2.5, clamp_timeout(15))

    def test_clamp_timeout_never_drops_below_the_minimum(self):
        # A timeout of zero means "wait forever" to requests, so a spent
        # budget still hands out the floor rather than an unbounded call.
        clock = FakeClock(160.0)

        with use_search_budget(160.0, clock=clock) as budget:
            self.assertEqual(0.1, clamp_timeout(15))
            self.assertEqual(0.5, clamp_timeout(15, minimum_seconds=0.5))
            self.assertTrue(budget.exhausted)

    def test_clamp_timeout_without_a_budget_returns_the_call_site_value(self):
        # Slow mode rebinds the timeout constants at runtime, so the helper
        # takes the caller's current value and never imports one itself.
        self.assertIsNone(current_budget())
        self.assertIsNone(remaining_seconds())
        self.assertEqual(45, clamp_timeout(45))
        self.assertEqual(15, clamp_timeout(15))

    def test_checkpoint_without_a_budget_does_nothing(self):
        self.assertIsNone(current_budget())

        checkpoint()

    def test_checkpoint_raises_and_marks_the_budget_once_time_is_up(self):
        clock = FakeClock(100.0)

        with use_search_budget(160.0, clock=clock) as budget:
            checkpoint()
            self.assertFalse(budget.exhausted)
            clock.now = 160.0
            with self.assertRaises(SearchBudgetExhausted):
                checkpoint()
            self.assertTrue(budget.exhausted)


class SearchBudgetContextTests(unittest.TestCase):
    """The budget travels in a `ContextVar`, so it stays worker-local."""

    def test_a_nested_budget_restores_the_outer_one(self):
        with use_search_budget(160.0, clock=lambda: 100.0) as outer:
            with use_search_budget(120.0, clock=lambda: 100.0) as inner:
                self.assertIs(inner, current_budget())
                self.assertEqual(20.0, remaining_seconds())
            self.assertIs(outer, current_budget())
            self.assertEqual(60.0, remaining_seconds())

        self.assertIsNone(current_budget())

    def test_the_budget_is_cleared_after_the_body_raises(self):
        # Pool threads are reused, so a budget left behind by a failing source
        # would silently apply to whatever runs on that thread next.
        with self.assertRaises(RuntimeError):
            with use_search_budget(160.0, clock=lambda: 100.0):
                raise RuntimeError("synthetic source failure")

        self.assertIsNone(current_budget())

    def test_a_budget_does_not_leak_into_another_thread(self):
        seen = []

        with use_search_budget(160.0, clock=lambda: 100.0):
            worker = Thread(target=lambda: seen.append(current_budget()))
            worker.start()
            worker.join(10)

        self.assertEqual([None], seen)

    def test_a_nested_pool_only_sees_the_budget_through_copy_context(self):
        # Sources that fan out into their own ThreadPoolExecutor (AT, SL) are
        # the case this breaks on: a pool thread starts from an empty context,
        # so the callable must carry the context over explicitly or it runs
        # unbounded while the request that started it is already out of time.
        with use_search_budget(160.0, clock=lambda: 100.0) as budget:
            with ThreadPoolExecutor(max_workers=1) as pool:
                bare = pool.submit(current_budget).result(10)
                context = copy_context()
                carried = pool.submit(context.run, current_budget).result(10)

        self.assertIsNone(bare)
        self.assertIs(budget, carried)

    def test_a_carried_budget_is_shared_with_the_nested_worker(self):
        # The nested worker holds the same object, so time it spends past the
        # deadline marks the outer source's result as partial.
        clock = FakeClock(160.0)

        with use_search_budget(160.0, clock=clock) as budget:
            with ThreadPoolExecutor(max_workers=1) as pool:
                context = copy_context()
                pool.submit(context.run, remaining_seconds).result(10)

            self.assertTrue(budget.exhausted)


class SourceTaskBudgetOutcomeTests(unittest.TestCase):
    """`SearchExecutor` runs every source inside the request's own budget."""

    def setUp(self):
        self._patches = ExitStack()
        self.addCleanup(self._patches.close)
        self._patches.enter_context(
            patch("quasarr.search.search_singleflight", SearchSingleFlight())
        )
        self.runtime = SearchRuntime(memory_reader=lambda: {})
        self.cache = SearchCache()

    def run_one(self, source, deadline, clock=time.time):
        executor = SearchExecutor(deadline=deadline, clock=clock)
        executor.add(source, (None, 0.0, 2000), {}, use_cache=True)
        with (
            patch("quasarr.search.search_runtime", self.runtime),
            patch("quasarr.search.search_cache", self.cache),
        ):
            return executor.run_all()

    def test_the_worker_budget_carries_the_requests_absolute_deadline(self):
        seen = []
        deadline = time.time() + 12.5

        def search():
            budget = current_budget()
            seen.append(None if budget is None else budget.deadline)
            return []

        self.run_one(FakeSource("db", search), deadline)

        self.assertEqual([deadline], seen)

    def test_a_source_inside_its_budget_is_completed_and_cached(self):
        seen = []

        def search():
            seen.append(remaining_seconds())
            return [{"details": {"title": "In.Time"}}]

        results, bar, _, _ = self.run_one(FakeSource("ok", search), time.time() + 30.0)

        self.assertEqual(["In.Time"], [r["details"]["title"] for r in results])
        self.assertIn("<bg green><black>OK</black></bg green>", bar)
        self.assertEqual(1, len(self.cache.cache))
        snapshot = self.runtime.snapshot()
        self.assertEqual(1, snapshot["source_completed"])
        self.assertEqual(0, snapshot["source_budget_exhausted"])
        self.assertEqual(1, len(seen))
        self.assertGreater(seen[0], 0.0)

    def test_a_partial_result_is_answered_with_but_never_cached(self):
        # The finish stamp is deliberately inside the deadline here: only the
        # source's own observation that its budget went makes the result
        # partial, and a partial answer must not be served for a whole TTL.
        deadline = 1005.0
        clock = ScriptedWorkerClock(
            fanout_time=1000.0, worker_times=[deadline + 1.0, deadline - 1.0]
        )
        partial = {"details": {"title": "Partial.Release"}}

        def search():
            releases = [partial]
            if remaining_seconds() <= 0:
                return releases
            releases.append({"details": {"title": "Never.Fetched"}})
            return releases

        results, bar, _, _ = self.run_one(FakeSource("pa", search), deadline, clock)

        self.assertEqual([partial], results)
        self.assertIn("<bg yellow><black>PA</black></bg yellow>", bar)
        self.assertEqual({}, self.cache.cache)
        snapshot = self.runtime.snapshot()
        self.assertEqual(1, snapshot["source_budget_exhausted"])
        self.assertEqual(0, snapshot["source_completed"])
        self.assertEqual(0, snapshot["source_errored"])

    def test_a_source_that_raises_budget_exhausted_answers_with_nothing(self):
        deadline = 1005.0
        clock = ScriptedWorkerClock(
            fanout_time=1000.0, worker_times=[deadline + 1.0, deadline - 1.0]
        )

        def search():
            checkpoint()
            return [{"details": {"title": "Never.Returned"}}]

        results, bar, _, _ = self.run_one(FakeSource("bx", search), deadline, clock)

        self.assertEqual([], results)
        self.assertIn("<bg yellow><black>BX</black></bg yellow>", bar)
        self.assertEqual({}, self.cache.cache)
        snapshot = self.runtime.snapshot()
        self.assertEqual(1, snapshot["source_budget_exhausted"])
        self.assertEqual(0, snapshot["source_errored"])
        self.assertEqual(0, snapshot["active_source_tasks"])

    def test_a_source_error_is_still_reported_as_an_error(self):
        # Wrapping the return value must not swallow a failure: an exception
        # keeps its red badge, its counter, and its empty uncached result.
        def fail():
            raise RuntimeError("synthetic source failure")

        results, bar, _, _ = self.run_one(FakeSource("er", fail), time.time() + 30.0)

        self.assertEqual([], results)
        self.assertIn("<bg red><white>ER</white></bg red>", bar)
        self.assertEqual({}, self.cache.cache)
        snapshot = self.runtime.snapshot()
        self.assertEqual(1, snapshot["source_errored"])
        self.assertEqual(0, snapshot["source_budget_exhausted"])
        self.assertEqual(0, snapshot["source_completed"])
        self.assertEqual(0, snapshot["active_source_tasks"])

    def test_a_worker_leaves_no_budget_behind_for_the_next_run(self):
        # `run_all()` builds a fresh pool per run, but nothing stops a thread
        # from outliving it - the context has to be reset by the wrapper.
        seen = []

        def search():
            seen.append(current_budget() is not None)
            return []

        self.run_one(FakeSource("rs", search), time.time() + 30.0)

        self.assertEqual([True], seen)
        self.assertIsNone(current_budget())


if __name__ == "__main__":
    unittest.main()
