# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

import time
import unittest
from concurrent.futures import Future
from contextlib import ExitStack
from threading import Barrier, Event, Lock, Thread
from typing import cast
from unittest.mock import patch

from quasarr.search import SearchCache, SearchExecutor
from quasarr.search.runtime import SearchRuntime
from quasarr.search.singleflight import SearchSingleFlight
from quasarr.search.sources.helpers.search_source import AbstractSearchSource


class FakeSource:
    def __init__(self, initials, func):
        self.initials = initials
        self._func = func

    def search(self, *_, **__):
        return self._func()


def make_source(initials, func):
    return cast(AbstractSearchSource, FakeSource(initials, func))


class NotifyingRuntime(SearchRuntime):
    def __init__(self, follower_arrived):
        super().__init__(memory_reader=lambda: {})
        self._follower_arrived = follower_arrived

    def record_coalesced_waiter(self):
        super().record_coalesced_waiter()
        self._follower_arrived.set()


class RecordingCache(SearchCache):
    def __init__(self):
        super().__init__()
        self.reads = []
        self.writes = []

    def get(self, key):
        self.reads.append(key)
        return super().get(key)

    def set(self, key, value, ttl=300):
        self.writes.append((key, ttl))
        super().set(key, value, ttl=ttl)


class RecordingSingleFlight(SearchSingleFlight):
    def __init__(self):
        super().__init__()
        self.keys = []
        self.handles = []

    def submit(self, key, executor, func, deadline):
        shared_work = super().submit(key, executor, func, deadline)
        with self._lock:
            self.keys.append(key)
            self.handles.append(shared_work)
        return shared_work


class ControlledExecutor:
    def __init__(self, futures=None):
        self._queued_futures = list(futures or [])
        self.futures = []
        self.functions = []

    def submit(self, func):
        future = self._queued_futures.pop(0) if self._queued_futures else Future()
        self.functions.append(func)
        self.futures.append(future)
        return future


class SearchSingleFlightIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._patches = ExitStack()
        self.addCleanup(self._patches.close)
        self._patches.enter_context(
            patch("quasarr.search.search_singleflight", SearchSingleFlight())
        )

    def test_fresh_registry_starts_work_after_a_previous_run_times_out(self):
        blocked_started = Event()
        release_blocked = Event()
        self.addCleanup(release_blocked.set)
        first_registry = SearchSingleFlight()
        second_calls = []
        fresh_release = {"details": {"title": "Fresh.Release"}}

        def blocked_search():
            blocked_started.set()
            release_blocked.wait(10)
            return [{"details": {"title": "Blocked.Release"}}]

        def fresh_search():
            second_calls.append(True)
            return [fresh_release]

        def run_like_test(source, registry):
            executor = SearchExecutor(deadline=time.time() + 0.2)
            executor.add(source, (None, 0.0, 2000), {})
            with (
                patch(
                    "quasarr.search.search_runtime",
                    SearchRuntime(memory_reader=lambda: {}),
                ),
                patch("quasarr.search.search_cache", SearchCache()),
                patch("quasarr.search.search_singleflight", registry),
            ):
                return executor.run_all()[0]

        first_response = run_like_test(
            make_source("sf", blocked_search), first_registry
        )
        self.assertTrue(blocked_started.is_set())
        self.assertEqual([], first_response)
        self.assertEqual(1, len(first_registry._flights))

        second_response = run_like_test(
            make_source("sf", fresh_search), SearchSingleFlight()
        )

        self.assertEqual([fresh_release], second_response)
        self.assertEqual([True], second_calls)

    def test_identical_in_flight_cache_keys_execute_the_source_once(self):
        start = Barrier(3, timeout=10)
        source_started = Event()
        follower_arrived = Event()
        release_source = Event()
        self.addCleanup(release_source.set)
        runtime = NotifyingRuntime(follower_arrived)
        cache = RecordingCache()
        state = object()
        calls = 0
        calls_lock = Lock()
        failures = []
        responses = [None, None]
        release = {"details": {"title": "Shared.Release"}}

        def search():
            nonlocal calls
            with calls_lock:
                calls += 1
                invocation = calls
            if invocation == 1:
                source_started.set()
            else:
                # Before singleflight exists, the duplicate invocation proves
                # the second executor reached the same in-flight key.
                follower_arrived.set()
            release_source.wait(10)
            return [release]

        source = make_source("sf", search)

        def run(index):
            try:
                executor = SearchExecutor(deadline=time.time() + 10)
                executor.add(source, (state, 0.0, 2000), {}, use_cache=True)
                start.wait()
                responses[index] = executor.run_all()[0]
            except Exception as exc:
                failures.append(exc)

        workers = [Thread(target=run, args=(index,)) for index in range(2)]
        with (
            patch("quasarr.search.search_runtime", runtime),
            patch("quasarr.search.search_cache", cache),
        ):
            for worker in workers:
                worker.start()
            start.wait()
            self.assertTrue(source_started.wait(10))
            self.assertTrue(follower_arrived.wait(10))
            release_source.set()
            for worker in workers:
                worker.join(10)

        self.assertEqual([], failures)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(1, calls)
        self.assertEqual([[release], [release]], responses)
        self.assertEqual(1, len(cache.writes))
        snapshot = runtime.snapshot()
        self.assertEqual(1, snapshot["source_completed"])
        self.assertEqual(1, snapshot["coalesced_waiters"])

    def test_later_deadline_follower_caches_after_the_leader_times_out(self):
        source_started = Event()
        follower_arrived = Event()
        release_source = Event()
        leader_finished = Event()
        self.addCleanup(release_source.set)
        runtime = NotifyingRuntime(follower_arrived)
        cache = SearchCache()
        singleflight = SearchSingleFlight()
        state = object()
        responses = {}
        failures = []
        release = {"details": {"title": "Shared.Release"}}

        def search():
            source_started.set()
            release_source.wait(10)
            return [release]

        source = make_source("sf", search)

        def run(name, deadline):
            try:
                executor = SearchExecutor(deadline=deadline)
                executor.add(source, (state, 0.0, 2000), {}, use_cache=True)
                responses[name] = executor.run_all()[0]
            except Exception as exc:
                failures.append(exc)
            finally:
                if name == "leader":
                    leader_finished.set()

        leader = Thread(target=run, args=("leader", time.time() + 0.5))
        follower = Thread(target=run, args=("follower", time.time() + 10))
        with (
            patch("quasarr.search.search_runtime", runtime),
            patch("quasarr.search.search_cache", cache),
            patch("quasarr.search.search_singleflight", singleflight),
        ):
            leader.start()
            self.assertTrue(source_started.wait(10))
            follower.start()
            self.assertTrue(follower_arrived.wait(10))
            self.assertTrue(leader_finished.wait(10))
            release_source.set()
            leader.join(10)
            follower.join(10)

        self.assertEqual([], failures)
        self.assertFalse(leader.is_alive())
        self.assertFalse(follower.is_alive())
        self.assertEqual([], responses["leader"])
        self.assertEqual([release], responses["follower"])
        self.assertEqual(1, cache.stats()["entry_count"])

    def test_follower_timeout_never_cancels_the_running_leader(self):
        source_started = Event()
        follower_arrived = Event()
        release_source = Event()
        self.addCleanup(release_source.set)
        runtime = NotifyingRuntime(follower_arrived)
        cache = RecordingCache()
        singleflight = RecordingSingleFlight()
        state = object()
        leader_response = []
        failures = []
        release = {"details": {"title": "Leader.Result"}}

        def search():
            source_started.set()
            release_source.wait(10)
            return [release]

        source = make_source("sf", search)

        def run_leader():
            try:
                executor = SearchExecutor(deadline=time.time() + 10)
                executor.add(source, (state, 0.0, 2000), {}, use_cache=True)
                leader_response.extend(executor.run_all()[0])
            except Exception as exc:
                failures.append(exc)

        leader = Thread(target=run_leader)
        with (
            patch("quasarr.search.search_runtime", runtime),
            patch("quasarr.search.search_cache", cache),
            patch("quasarr.search.search_singleflight", singleflight),
        ):
            leader.start()
            self.assertTrue(source_started.wait(10))

            follower = SearchExecutor(deadline=time.time() + 0.25)
            follower.add(source, (state, 0.0, 2000), {}, use_cache=True)
            follower_response = follower.run_all()[0]

            self.assertTrue(follower_arrived.is_set())
            leader_work, follower_work = singleflight.handles
            self.assertTrue(leader_work.is_leader)
            self.assertFalse(follower_work.is_leader)
            self.assertIs(leader_work.future, follower_work.future)
            self.assertFalse(leader_work.future.cancelled())
            self.assertFalse(leader_work.future.done())
            self.assertEqual(0, follower_work._flight.waiters)
            self.assertEqual(1, len(singleflight._flights))
            snapshot = runtime.snapshot()
            self.assertEqual(0, snapshot["source_dropped"])
            self.assertEqual(0, snapshot["overdue_source_tasks"])

            release_source.set()
            leader.join(10)

        self.assertEqual([], failures)
        self.assertFalse(leader.is_alive())
        self.assertEqual([], follower_response)
        self.assertEqual([release], leader_response)
        self.assertEqual(1, len(cache.writes))
        self.assertEqual({}, singleflight._flights)

    def test_shared_exception_reaches_all_waiters_and_is_not_cached(self):
        source_started = Event()
        follower_arrived = Event()
        release_source = Event()
        self.addCleanup(release_source.set)
        runtime = NotifyingRuntime(follower_arrived)
        cache = RecordingCache()
        singleflight = SearchSingleFlight()
        state = object()
        calls = 0
        failures = []
        responses = {}

        def search():
            nonlocal calls
            calls += 1
            source_started.set()
            release_source.wait(10)
            raise RuntimeError("synthetic shared failure")

        source = make_source("sf", search)

        def run(name):
            try:
                executor = SearchExecutor(deadline=time.time() + 10)
                executor.add(source, (state, 0.0, 2000), {}, use_cache=True)
                responses[name] = executor.run_all()
            except Exception as exc:
                failures.append(exc)

        leader = Thread(target=run, args=("leader",))
        follower = Thread(target=run, args=("follower",))
        with (
            patch("quasarr.search.search_runtime", runtime),
            patch("quasarr.search.search_cache", cache),
            patch("quasarr.search.search_singleflight", singleflight),
        ):
            leader.start()
            self.assertTrue(source_started.wait(10))
            follower.start()
            self.assertTrue(follower_arrived.wait(10))
            release_source.set()
            leader.join(10)
            follower.join(10)

        self.assertEqual([], failures)
        self.assertFalse(leader.is_alive())
        self.assertFalse(follower.is_alive())
        self.assertEqual(1, calls)
        self.assertEqual([], responses["leader"][0])
        self.assertEqual([], responses["follower"][0])
        self.assertIn("SF", responses["leader"][1])
        self.assertIn("SF", responses["follower"][1])
        self.assertEqual([], cache.writes)
        self.assertEqual(0, cache.stats()["entry_count"])
        snapshot = runtime.snapshot()
        self.assertEqual(1, snapshot["source_errored"])
        self.assertEqual(1, snapshot["coalesced_waiters"])
        self.assertEqual({}, singleflight._flights)

    def test_empty_success_is_shared_and_cached_once(self):
        source_started = Event()
        follower_arrived = Event()
        release_source = Event()
        self.addCleanup(release_source.set)
        runtime = NotifyingRuntime(follower_arrived)
        cache = RecordingCache()
        singleflight = SearchSingleFlight()
        state = object()
        calls = 0
        failures = []
        responses = {}

        def search():
            nonlocal calls
            calls += 1
            source_started.set()
            release_source.wait(10)
            return []

        source = make_source("sf", search)

        def run(name):
            try:
                executor = SearchExecutor(deadline=time.time() + 10)
                executor.add(source, (state, 0.0, 2000), {}, use_cache=True)
                responses[name] = executor.run_all()[0]
            except Exception as exc:
                failures.append(exc)

        leader = Thread(target=run, args=("leader",))
        follower = Thread(target=run, args=("follower",))
        with (
            patch("quasarr.search.search_runtime", runtime),
            patch("quasarr.search.search_cache", cache),
            patch("quasarr.search.search_singleflight", singleflight),
        ):
            leader.start()
            self.assertTrue(source_started.wait(10))
            follower.start()
            self.assertTrue(follower_arrived.wait(10))
            release_source.set()
            leader.join(10)
            follower.join(10)

        self.assertEqual([], failures)
        self.assertFalse(leader.is_alive())
        self.assertFalse(follower.is_alive())
        self.assertEqual(1, calls)
        self.assertEqual([], responses["leader"])
        self.assertEqual([], responses["follower"])
        self.assertEqual(1, len(cache.writes))
        cached, _ = cache.get(cache.writes[0][0])
        self.assertEqual([], cached)
        self.assertEqual(1, runtime.snapshot()["source_completed"])
        self.assertEqual({}, singleflight._flights)

    def test_registry_and_cache_use_the_same_integer_key(self):
        runtime = SearchRuntime(memory_reader=lambda: {})
        cache = RecordingCache()
        singleflight = RecordingSingleFlight()
        release = {"details": {"title": "One.Release"}}
        executor = SearchExecutor(deadline=time.time() + 10)
        executor.add(
            make_source("sf", lambda: [release]),
            (object(), 0.0, 2000),
            {"search_string": "synthetic"},
            use_cache=True,
            cache_category=2010,
        )

        with (
            patch("quasarr.search.search_runtime", runtime),
            patch("quasarr.search.search_cache", cache),
            patch("quasarr.search.search_singleflight", singleflight),
        ):
            results = executor.run_all()[0]

        self.assertEqual([release], results)
        self.assertEqual(1, len(singleflight.keys))
        self.assertIsInstance(singleflight.keys[0], int)
        self.assertEqual(singleflight.keys, cache.reads)
        self.assertEqual(singleflight.keys[0], cache.writes[0][0])


class SearchSingleFlightRegistryTests(unittest.TestCase):
    def test_follower_cleanup_only_decrements_waiters(self):
        registry = SearchSingleFlight()
        executor = ControlledExecutor()
        leader = registry.submit(101, executor, lambda: [], time.time() + 10)
        follower = registry.submit(101, executor, lambda: [], time.time() + 1)

        self.assertTrue(leader.is_leader)
        self.assertFalse(follower.is_leader)
        self.assertIs(leader.future, follower.future)
        self.assertEqual(1, follower._flight.waiters)

        follower.waiter_done()
        follower.waiter_done()

        self.assertEqual(0, follower._flight.waiters)
        self.assertFalse(leader.future.cancelled())
        self.assertIn(101, registry._flights)

        leader.future.set_result([])
        self.assertEqual({}, registry._flights)

    def test_done_callback_cleans_success_exception_and_cancellation(self):
        for terminal_state in ("success", "exception", "cancellation"):
            with self.subTest(terminal_state=terminal_state):
                registry = SearchSingleFlight()
                executor = ControlledExecutor()
                shared_work = registry.submit(
                    101, executor, lambda: [], time.time() + 10
                )
                self.assertIn(101, registry._flights)

                if terminal_state == "success":
                    shared_work.future.set_result([])
                elif terminal_state == "exception":
                    shared_work.future.set_exception(RuntimeError("synthetic failure"))
                else:
                    shared_work.future.cancel()

                self.assertEqual({}, registry._flights)

    def test_joiner_replaces_a_cancelled_future_waiting_for_cleanup(self):
        registry = SearchSingleFlight()
        cancellation_observed = Event()
        cancelled_future = Future()
        cancelled_future.add_done_callback(lambda _: cancellation_observed.set())
        old_executor = ControlledExecutor([cancelled_future])
        old_work = registry.submit(101, old_executor, lambda: [], time.time() + 10)
        new_executor = ControlledExecutor()

        with registry._lock:
            cancel_thread = Thread(target=old_work.future.cancel)
            cancel_thread.start()
            self.assertTrue(cancellation_observed.wait(10))
            replacement = registry.submit(
                101, new_executor, lambda: ["replacement"], time.time() + 10
            )
            self.assertTrue(replacement.is_leader)
            self.assertIsNot(old_work.future, replacement.future)

        cancel_thread.join(10)
        self.assertFalse(cancel_thread.is_alive())
        self.assertIs(replacement.future, registry._flights[101].future)

        replacement.future.set_result(["replacement"])
        self.assertEqual({}, registry._flights)

    def test_independent_integer_keys_never_coalesce(self):
        registry = SearchSingleFlight()
        executor = ControlledExecutor()

        first = registry.submit(101, executor, lambda: ["first"], time.time() + 10)
        second = registry.submit(202, executor, lambda: ["second"], time.time() + 10)

        self.assertTrue(first.is_leader)
        self.assertTrue(second.is_leader)
        self.assertIsNot(first.future, second.future)
        self.assertEqual(2, len(executor.functions))

        first.future.set_result(["first"])
        second.future.set_result(["second"])
        self.assertEqual({}, registry._flights)


if __name__ == "__main__":
    unittest.main()
