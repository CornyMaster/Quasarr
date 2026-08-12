import ctypes
import unittest
from threading import Event, Lock, Thread

from quasarr.search.cache import SearchCache
from quasarr.search.reclaim import (
    IdleMemoryReclaimer,
    _NativeHeapTrimmer,
    trim_native_heap,
)
from quasarr.search.runtime import SearchRuntime

SUMMARY_KEYS = {
    "performed",
    "failed",
    "expired_cache_entries",
    "gc_collected",
    "native_heap_trimmed",
    "pss_before_kib",
    "pss_after_kib",
}


class FakeClock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeTimer:
    def __init__(self, interval, callback):
        self.interval = interval
        self.callback = callback
        self.daemon = False
        self.started = False
        self.cancelled = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.callback()


class FakeTimerFactory:
    def __init__(self):
        self.timers = []

    def __call__(self, interval, callback):
        timer = FakeTimer(interval, callback)
        self.timers.append(timer)
        return timer


class FailingOnceClock(FakeClock):
    def __init__(self, now=100.0):
        super().__init__(now)
        self.fail_next = False

    def __call__(self):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("synthetic clock failure")
        return super().__call__()


class FailingTimerFactory(FakeTimerFactory):
    def __init__(self, fail_on_call):
        super().__init__()
        self.calls = 0
        self.fail_on_call = fail_on_call

    def __call__(self, interval, callback):
        self.calls += 1
        if self.calls == self.fail_on_call:
            raise RuntimeError("synthetic timer factory failure")
        return super().__call__(interval, callback)


class RaisingReclaimer:
    def __init__(self):
        self.calls = []

    def cancel_for_activity(self):
        self.calls.append("cancel")
        raise RuntimeError("synthetic cancel failure")

    def schedule_if_quiet(self):
        self.calls.append("schedule")
        raise RuntimeError("synthetic schedule failure")


class HandoffActivityRuntime(SearchRuntime):
    def __init__(self):
        super().__init__(memory_reader=lambda: {})
        self.activity_on_next_idle_check = False
        self.activity_events = []

    def is_idle(self):
        if self.activity_on_next_idle_check:
            self.activity_on_next_idle_check = False
            with self.request(category_count=1, family_count=1):
                self.activity_events.append("activity")
        return super().is_idle()


class ReservedTimerRaceRuntime(SearchRuntime):
    def __init__(self):
        super().__init__(memory_reader=lambda: {})
        self.first_idle_checked = Event()
        self.activity_cancelled = Event()
        self.busy_recheck_observed = Event()
        self.allow_busy_recheck_return = Event()
        self.activity_schedule_returned = Event()
        self._first_idle_check_pending = True

    def is_idle(self):
        if self._first_idle_check_pending:
            self._first_idle_check_pending = False
            was_idle = super().is_idle()
            self.first_idle_checked.set()
            if not self.activity_cancelled.wait(5):
                raise RuntimeError("activity cancellation did not complete")
            return was_idle

        is_idle = super().is_idle()
        if not is_idle and not self.busy_recheck_observed.is_set():
            self.busy_recheck_observed.set()
            if not self.allow_busy_recheck_return.wait(5):
                raise RuntimeError("busy recheck was not released")
        return is_idle

    def _cancel_idle_reclaim(self, reclaimer):
        super()._cancel_idle_reclaim(reclaimer)
        self.activity_cancelled.set()

    def _schedule_idle_reclaim(self, reclaimer):
        super()._schedule_idle_reclaim(reclaimer)
        self.activity_schedule_returned.set()


class RecordingCache:
    def __init__(self, events=None, expired=0):
        self.events = events if events is not None else []
        self.expired = expired
        self.lock = Lock()

    def sweep(self):
        with self.lock:
            self.events.append("sweep")
            return self.expired


class SequenceMemoryReader:
    def __init__(self, events, pss_values):
        self.events = events
        self.pss_values = iter(pss_values)

    def __call__(self):
        self.events.append("memory")
        return {
            "rss_kib": 4096,
            "pss_kib": next(self.pss_values),
            "threads": 8,
            "source_identifier": "must-not-be-recorded",
        }


def can_acquire_from_another_thread(lock):
    acquired = []

    def try_lock():
        locked = lock.acquire(blocking=False)
        acquired.append(locked)
        if locked:
            lock.release()

    thread = Thread(target=try_lock)
    thread.start()
    thread.join(5)
    return acquired == [True]


class IdleMemoryReclaimerTests(unittest.TestCase):
    def make_reclaimer(
        self,
        *,
        runtime=None,
        cache=None,
        clock=None,
        timers=None,
        collector=None,
        native_trimmer=None,
        memory_reader=None,
        logger=None,
    ):
        runtime = runtime or SearchRuntime(memory_reader=lambda: {})
        cache = cache or RecordingCache()
        clock = clock or FakeClock()
        timers = timers or FakeTimerFactory()
        return IdleMemoryReclaimer(
            cache,
            runtime,
            quiet_seconds=30,
            minimum_interval_seconds=300,
            clock=clock,
            timer_factory=timers,
            collector=collector or (lambda: 0),
            native_trimmer=native_trimmer or (lambda: False),
            memory_reader=memory_reader or (lambda: {}),
            logger=logger or (lambda _message: None),
        )

    def test_only_one_daemon_timer_is_armed_for_the_quiet_period(self):
        timers = FakeTimerFactory()
        reclaimer = self.make_reclaimer(timers=timers)

        reclaimer.schedule_if_quiet()
        reclaimer.schedule_if_quiet()

        self.assertEqual(1, len(timers.timers))
        self.assertEqual(30, timers.timers[0].interval)
        self.assertTrue(timers.timers[0].daemon)
        self.assertTrue(timers.timers[0].started)

    def test_request_activity_cancels_and_restarts_the_quiet_timer(self):
        runtime = SearchRuntime(memory_reader=lambda: {})
        timers = FakeTimerFactory()
        reclaimer = self.make_reclaimer(runtime=runtime, timers=timers)
        reclaimer.schedule_if_quiet()
        first = timers.timers[0]

        with runtime.request(category_count=1, family_count=1):
            self.assertTrue(first.cancelled)
            reclaimer.schedule_if_quiet()
            self.assertEqual(1, len(timers.timers))

        self.assertEqual(2, len(timers.timers))
        self.assertFalse(timers.timers[1].cancelled)
        self.assertEqual(30, timers.timers[1].interval)

    def test_discarded_unstarted_timer_rearms_after_activity_end_race(self):
        runtime = ReservedTimerRaceRuntime()
        timers = FakeTimerFactory()
        reclaimer = self.make_reclaimer(runtime=runtime, timers=timers)
        finish_activity = Event()
        activity_entered = Event()
        thread_errors = []

        def schedule_timer():
            try:
                reclaimer.schedule_if_quiet()
            except Exception as error:
                thread_errors.append(error)

        def run_activity():
            try:
                with runtime.request(category_count=1, family_count=1):
                    activity_entered.set()
                    if not finish_activity.wait(5):
                        raise RuntimeError("activity was not released")
            except Exception as error:
                thread_errors.append(error)

        scheduler = Thread(target=schedule_timer)
        activity = Thread(target=run_activity)
        scheduler.start()
        self.assertTrue(runtime.first_idle_checked.wait(5))
        activity.start()
        self.assertTrue(activity_entered.wait(5))
        self.assertTrue(runtime.busy_recheck_observed.wait(5))

        finish_activity.set()
        self.assertTrue(runtime.activity_schedule_returned.wait(5))
        runtime.allow_busy_recheck_return.set()
        scheduler.join(5)
        activity.join(5)

        self.assertFalse(scheduler.is_alive())
        self.assertFalse(activity.is_alive())
        self.assertEqual([], thread_errors)
        self.assertTrue(runtime.is_idle())
        self.assertEqual(2, len(timers.timers))
        self.assertTrue(timers.timers[0].cancelled)
        self.assertEqual(30, timers.timers[1].interval)
        self.assertTrue(timers.timers[1].started)
        self.assertIs(reclaimer._timer, timers.timers[1])

    def test_arm_generation_restarts_after_hidden_handoff_activity(self):
        runtime = HandoffActivityRuntime()
        timers = FakeTimerFactory()
        reclaim_events = []
        reclaimer = self.make_reclaimer(
            runtime=runtime,
            timers=timers,
            cache=RecordingCache(reclaim_events),
            collector=lambda: reclaim_events.append("gc") or 0,
            native_trimmer=lambda: reclaim_events.append("trim") or True,
        )
        reclaimer.schedule_if_quiet()
        runtime.activity_on_next_idle_check = True

        timers.timers[0].fire()

        self.assertEqual(["activity"], runtime.activity_events)
        self.assertEqual([], reclaim_events)
        self.assertEqual(1, reclaimer._activity_generation)
        self.assertEqual(2, len(timers.timers))
        self.assertEqual(30, timers.timers[1].interval)
        self.assertTrue(timers.timers[1].started)

    def test_timer_callback_during_public_collection_rearms_at_rate_limit(self):
        timers = FakeTimerFactory()
        sweep_started = Event()
        release_sweep = Event()
        summaries = []
        thread_errors = []

        class BlockingCache:
            def sweep(self):
                sweep_started.set()
                if not release_sweep.wait(5):
                    raise RuntimeError("blocked sweep was not released")
                return 0

        reclaimer = self.make_reclaimer(cache=BlockingCache(), timers=timers)
        reclaimer.schedule_if_quiet()

        def collect_publicly():
            try:
                summaries.append(reclaimer.collect_and_trim())
            except Exception as error:
                thread_errors.append(error)

        collector = Thread(target=collect_publicly)
        collector.start()
        self.assertTrue(sweep_started.wait(5))

        timers.timers[0].fire()
        release_sweep.set()
        collector.join(5)

        self.assertFalse(collector.is_alive())
        self.assertEqual([], thread_errors)
        self.assertTrue(summaries[0]["performed"])
        self.assertEqual(2, len(timers.timers))
        self.assertEqual(300, timers.timers[1].interval)
        self.assertTrue(timers.timers[1].started)

    def test_timer_start_runs_outside_runtime_and_reclaimer_locks(self):
        runtime = SearchRuntime(memory_reader=lambda: {})
        lock_checks = []
        timers = []

        class LockCheckingTimer(FakeTimer):
            def start(self):
                lock_checks.append(
                    (
                        can_acquire_from_another_thread(runtime._lock),
                        can_acquire_from_another_thread(reclaimer._lock),
                    )
                )
                super().start()

        def timer_factory(interval, callback):
            timer = LockCheckingTimer(interval, callback)
            timers.append(timer)
            return timer

        reclaimer = self.make_reclaimer(runtime=runtime, timers=timer_factory)

        with runtime.request(category_count=1, family_count=1):
            pass

        self.assertEqual([(True, True)], lock_checks)
        self.assertEqual(1, len(timers))

    def test_raising_reclaimer_callbacks_do_not_fail_request_transitions(self):
        runtime = SearchRuntime(memory_reader=lambda: {})
        reclaimer = RaisingReclaimer()
        runtime.set_idle_reclaimer(reclaimer)

        with runtime.request(category_count=1, family_count=1):
            pass

        self.assertEqual(["cancel", "schedule"], reclaimer.calls)
        self.assertEqual(0, runtime.snapshot()["active_requests"])

    def test_raising_reclaimer_callbacks_do_not_fail_source_transitions(self):
        runtime = SearchRuntime(memory_reader=lambda: {})
        reclaimer = RaisingReclaimer()
        runtime.set_idle_reclaimer(reclaimer)

        with runtime.source_task():
            pass

        self.assertEqual(["cancel", "schedule"], reclaimer.calls)
        self.assertEqual(0, runtime.snapshot()["active_source_tasks"])

    def test_raising_reclaimer_callbacks_do_not_fail_overdue_transitions(self):
        runtime = SearchRuntime(memory_reader=lambda: {})
        reclaimer = RaisingReclaimer()
        runtime.set_idle_reclaimer(reclaimer)

        token = runtime.mark_source_overdue()
        resolved = runtime.resolve_source_overdue(token)

        self.assertTrue(resolved)
        self.assertEqual(["cancel", "schedule"], reclaimer.calls)
        self.assertEqual(0, runtime.snapshot()["overdue_source_tasks"])

    def test_timer_callback_failure_is_contained_and_state_stays_schedulable(self):
        clock = FailingOnceClock()
        timers = FakeTimerFactory()
        reclaimer = self.make_reclaimer(clock=clock, timers=timers)
        reclaimer.schedule_if_quiet()
        clock.fail_next = True

        timers.timers[0].fire()
        reclaimer.schedule_if_quiet()

        self.assertEqual(2, len(timers.timers))
        self.assertTrue(timers.timers[1].started)

    def test_public_collect_rearm_factory_failure_is_contained(self):
        runtime = SearchRuntime(memory_reader=lambda: {})
        timers = FailingTimerFactory(fail_on_call=1)

        class ActivityDuringSweepCache:
            def sweep(self):
                with runtime.request(category_count=1, family_count=1):
                    pass
                return 0

        reclaimer = self.make_reclaimer(
            runtime=runtime,
            timers=timers,
            cache=ActivityDuringSweepCache(),
        )

        summary = reclaimer.collect_and_trim()

        self.assertFalse(summary["performed"])
        self.assertFalse(summary["failed"])
        self.assertEqual(1, timers.calls)
        self.assertEqual([], timers.timers)
        self.assertFalse(reclaimer._collecting)
        self.assertTrue(reclaimer._schedule_pending)

        reclaimer.schedule_if_quiet()

        self.assertEqual(2, timers.calls)
        self.assertEqual(1, len(timers.timers))
        self.assertTrue(timers.timers[0].started)
        self.assertFalse(reclaimer._schedule_pending)

    def test_timer_start_failure_records_one_deferred_schedule(self):
        timers = FakeTimerFactory()

        class StartFailureTimer(FakeTimer):
            def start(self):
                raise RuntimeError("synthetic timer start failure")

        def fail_first_start(interval, callback):
            if not timers.timers:
                timer = StartFailureTimer(interval, callback)
                timers.timers.append(timer)
                return timer
            return timers(interval, callback)

        reclaimer = self.make_reclaimer(timers=fail_first_start)

        reclaimer.schedule_if_quiet()

        self.assertEqual(1, len(timers.timers))
        self.assertIsNone(reclaimer._timer)
        self.assertTrue(reclaimer._schedule_pending)

        reclaimer.schedule_if_quiet()

        self.assertEqual(2, len(timers.timers))
        self.assertTrue(timers.timers[1].started)
        self.assertFalse(reclaimer._schedule_pending)

    def test_source_and_overdue_transitions_each_reset_the_quiet_timer(self):
        runtime = SearchRuntime(memory_reader=lambda: {})
        timers = FakeTimerFactory()
        reclaimer = self.make_reclaimer(runtime=runtime, timers=timers)
        reclaimer.schedule_if_quiet()

        with runtime.source_task():
            self.assertTrue(timers.timers[0].cancelled)
        self.assertEqual(2, len(timers.timers))

        token = runtime.mark_source_overdue()
        self.assertTrue(timers.timers[1].cancelled)
        self.assertEqual(2, len(timers.timers))

        self.assertTrue(runtime.resolve_source_overdue(token))
        self.assertEqual(3, len(timers.timers))
        self.assertFalse(timers.timers[2].cancelled)

    def test_an_active_request_prevents_sweep_gc_and_trim(self):
        runtime = SearchRuntime(memory_reader=lambda: {})
        events = []
        reclaimer = self.make_reclaimer(
            runtime=runtime,
            cache=RecordingCache(events),
            collector=lambda: events.append("gc") or 0,
            native_trimmer=lambda: events.append("trim") or True,
        )

        with runtime.request(category_count=1, family_count=1):
            summary = reclaimer.collect_and_trim()

        self.assertFalse(summary["performed"])
        self.assertEqual([], events)

    def test_an_active_source_prevents_sweep_gc_and_trim(self):
        runtime = SearchRuntime(memory_reader=lambda: {})
        events = []
        reclaimer = self.make_reclaimer(
            runtime=runtime,
            cache=RecordingCache(events),
            collector=lambda: events.append("gc") or 0,
            native_trimmer=lambda: events.append("trim") or True,
        )

        with runtime.source_task():
            summary = reclaimer.collect_and_trim()

        self.assertFalse(summary["performed"])
        self.assertEqual([], events)

    def test_an_overdue_source_prevents_sweep_gc_and_trim(self):
        runtime = SearchRuntime(memory_reader=lambda: {})
        events = []
        reclaimer = self.make_reclaimer(
            runtime=runtime,
            cache=RecordingCache(events),
            collector=lambda: events.append("gc") or 0,
            native_trimmer=lambda: events.append("trim") or True,
        )
        token = runtime.mark_source_overdue()

        summary = reclaimer.collect_and_trim()

        self.assertFalse(summary["performed"])
        self.assertEqual([], events)
        self.assertTrue(runtime.resolve_source_overdue(token))

    def test_stale_callback_cannot_consume_replacement_while_fully_idle(self):
        runtime = SearchRuntime(memory_reader=lambda: {})
        timers = FakeTimerFactory()
        events = []
        reclaimer = self.make_reclaimer(
            runtime=runtime,
            timers=timers,
            cache=RecordingCache(events),
            collector=lambda: events.append("gc") or 0,
            native_trimmer=lambda: events.append("trim") or True,
        )
        reclaimer.schedule_if_quiet()
        stale_callback = timers.timers[0].callback
        token = runtime.mark_source_overdue()
        self.assertTrue(runtime.resolve_source_overdue(token))
        replacement = timers.timers[1]
        replacement_token = reclaimer._timer_token

        stale_callback()

        self.assertTrue(runtime.is_idle())
        self.assertEqual([], events)
        self.assertEqual(2, len(timers.timers))
        self.assertIs(reclaimer._timer, replacement)
        self.assertIs(reclaimer._timer_token, replacement_token)
        self.assertFalse(replacement.cancelled)
        self.assertTrue(replacement.started)

    def test_activity_during_sweep_aborts_and_restarts_the_quiet_period(self):
        runtime = SearchRuntime(memory_reader=lambda: {})
        timers = FakeTimerFactory()
        events = []

        class ActivityDuringSweepCache:
            def sweep(self):
                events.append("sweep")
                with runtime.request(category_count=1, family_count=1):
                    events.append("activity")
                return 0

        reclaimer = self.make_reclaimer(
            runtime=runtime,
            timers=timers,
            cache=ActivityDuringSweepCache(),
            collector=lambda: events.append("gc") or 0,
            native_trimmer=lambda: events.append("trim") or True,
        )

        summary = reclaimer.collect_and_trim()

        self.assertFalse(summary["performed"])
        self.assertEqual(["sweep", "activity"], events)
        self.assertEqual(1, len(timers.timers))
        self.assertEqual(30, timers.timers[0].interval)

    def test_reclaim_runs_at_most_once_per_five_minutes(self):
        clock = FakeClock()
        timers = FakeTimerFactory()
        collections = []
        reclaimer = self.make_reclaimer(
            clock=clock,
            timers=timers,
            collector=lambda: collections.append(clock()) or 0,
        )

        self.assertTrue(reclaimer.collect_and_trim()["performed"])
        clock.advance(1)
        self.assertFalse(reclaimer.collect_and_trim()["performed"])
        reclaimer.schedule_if_quiet()

        self.assertEqual(299, timers.timers[0].interval)
        clock.advance(298)
        self.assertFalse(reclaimer.collect_and_trim()["performed"])
        self.assertEqual([100.0], collections)

        clock.advance(1)
        timers.timers[0].fire()
        self.assertEqual([100.0, 400.0], collections)

    def test_rate_limited_timer_callback_rearms_for_remaining_interval(self):
        clock = FakeClock()
        timers = FakeTimerFactory()
        collections = []
        reclaimer = self.make_reclaimer(
            clock=clock,
            timers=timers,
            collector=lambda: collections.append(clock()) or 0,
        )
        reclaimer.schedule_if_quiet()

        self.assertTrue(reclaimer.collect_and_trim()["performed"])
        clock.advance(30)
        timers.timers[0].fire()

        self.assertEqual([100.0], collections)
        self.assertEqual(2, len(timers.timers))
        self.assertEqual(270, timers.timers[1].interval)
        self.assertTrue(timers.timers[1].started)

    def test_public_collect_does_not_arm_without_a_deferred_schedule(self):
        timers = FakeTimerFactory()
        reclaimer = self.make_reclaimer(timers=timers)

        summary = reclaimer.collect_and_trim()

        self.assertTrue(summary["performed"])
        self.assertEqual([], timers.timers)

    def test_a_failed_reclaim_attempt_obeys_the_five_minute_rate_limit(self):
        clock = FakeClock()
        timers = FakeTimerFactory()

        class FailingCache:
            def __init__(self):
                self.calls = 0

            def sweep(self):
                self.calls += 1
                raise RuntimeError("synthetic sweep failure")

        cache = FailingCache()
        reclaimer = self.make_reclaimer(
            cache=cache,
            clock=clock,
            timers=timers,
        )

        first = reclaimer.collect_and_trim()
        clock.advance(1)
        second = reclaimer.collect_and_trim()
        reclaimer.schedule_if_quiet()

        self.assertTrue(first["failed"])
        self.assertFalse(second["performed"])
        self.assertFalse(second["failed"])
        self.assertEqual(1, cache.calls)
        self.assertEqual(299, timers.timers[0].interval)

    def test_cache_sweep_precedes_gc_trim_and_pss_after_reading(self):
        runtime = SearchRuntime(memory_reader=lambda: {})
        events = []
        logs = []
        cache = RecordingCache(events, expired=3)

        def collect():
            events.append("gc")
            return 7

        def trim():
            events.append("trim")
            return True

        def log(message):
            events.append("log")
            logs.append(message)

        reclaimer = self.make_reclaimer(
            runtime=runtime,
            cache=cache,
            collector=collect,
            native_trimmer=trim,
            memory_reader=SequenceMemoryReader(events, [1000, 700]),
            logger=log,
        )

        summary = reclaimer.collect_and_trim()

        self.assertEqual(["memory", "sweep", "gc", "trim", "memory", "log"], events)
        self.assertEqual(
            {
                "performed": True,
                "failed": False,
                "expired_cache_entries": 3,
                "gc_collected": 7,
                "native_heap_trimmed": True,
                "pss_before_kib": 1000,
                "pss_after_kib": 700,
            },
            summary,
        )
        self.assertEqual(SUMMARY_KEYS, set(summary))
        self.assertEqual(1, len(logs))
        self.assertNotIn("must-not-be-recorded", logs[0])

    def test_gc_and_trim_run_outside_all_reclaimer_related_locks(self):
        runtime = SearchRuntime(memory_reader=lambda: {})
        cache = RecordingCache()
        lock_checks = []

        def record_lock_state():
            lock_checks.append(
                (
                    can_acquire_from_another_thread(cache.lock),
                    can_acquire_from_another_thread(runtime._lock),
                    can_acquire_from_another_thread(reclaimer._lock),
                )
            )

        reclaimer = self.make_reclaimer(
            runtime=runtime,
            cache=cache,
            collector=lambda: record_lock_state() or 0,
            native_trimmer=lambda: record_lock_state() or True,
        )

        self.assertTrue(reclaimer.collect_and_trim()["performed"])
        self.assertEqual(
            [(True, True, True), (True, True, True)],
            lock_checks,
        )

    def test_reclaim_sweeps_expired_entries_without_clearing_live_entries(self):
        cache_clock = FakeClock()
        runtime = SearchRuntime(memory_reader=lambda: {})
        cache = SearchCache(clock=cache_clock)
        cache.set("expired", [{"id": 1}], ttl=10)
        cache.set("live", [{"id": 2}], ttl=60)
        cache_clock.advance(11)
        reclaimer = self.make_reclaimer(runtime=runtime, cache=cache)

        summary = reclaimer.collect_and_trim()

        self.assertEqual(1, summary["expired_cache_entries"])
        self.assertEqual(
            {
                "entry_count": 1,
                "release_count": 1,
                "max_entries": 2048,
                "max_releases": 50000,
            },
            cache.stats(),
        )
        self.assertEqual(([{"id": 2}], 160.0), cache.get("live"))

    def test_other_platforms_still_run_gc_without_native_trim(self):
        collections = []
        reclaimer = self.make_reclaimer(
            collector=lambda: collections.append("gc") or 4,
            native_trimmer=lambda: False,
        )

        summary = reclaimer.collect_and_trim()

        self.assertEqual(["gc"], collections)
        self.assertEqual(4, summary["gc_collected"])
        self.assertFalse(summary["native_heap_trimmed"])

    def test_public_trim_function_delegates_to_the_cached_process_probe(self):
        calls = []

        def trimmer():
            calls.append(0)
            return True

        import quasarr.search.reclaim as reclaim

        original = reclaim._native_heap_trimmer
        reclaim._native_heap_trimmer = trimmer
        self.addCleanup(setattr, reclaim, "_native_heap_trimmer", original)

        self.assertTrue(trim_native_heap())
        self.assertEqual([0], calls)


class FakeMallocTrim:
    def __init__(self, result=1):
        self.result = result
        self.argtypes = None
        self.restype = None
        self.calls = []

    def __call__(self, padding):
        self.calls.append(padding)
        return self.result


class NativeHeapTrimmerTests(unittest.TestCase):
    def test_windows_never_probes_libc(self):
        loads = []
        trimmer = _NativeHeapTrimmer(
            platform="win32", libc_loader=lambda *args, **kwargs: loads.append(args)
        )

        self.assertFalse(trimmer())
        self.assertFalse(trimmer())
        self.assertEqual([], loads)

    def test_a_failed_linux_probe_is_a_permanent_no_op(self):
        loads = []

        def missing_libc(name, *, use_errno):
            loads.append((name, use_errno))
            raise OSError("musl has no libc.so.6")

        trimmer = _NativeHeapTrimmer(platform="linux", libc_loader=missing_libc)

        self.assertFalse(trimmer())
        self.assertFalse(trimmer())
        self.assertEqual([("libc.so.6", True)], loads)

    def test_a_non_oserror_loader_failure_is_a_permanent_no_op(self):
        loads = []

        def broken_loader(name, *, use_errno):
            loads.append((name, use_errno))
            raise RuntimeError("synthetic loader failure")

        trimmer = _NativeHeapTrimmer(platform="linux", libc_loader=broken_loader)

        self.assertFalse(trimmer())
        self.assertFalse(trimmer())
        self.assertEqual([("libc.so.6", True)], loads)

    def test_a_libc_without_malloc_trim_is_a_permanent_no_op(self):
        loads = []

        def libc_without_trim(name, *, use_errno):
            loads.append((name, use_errno))
            return object()

        trimmer = _NativeHeapTrimmer(platform="linux", libc_loader=libc_without_trim)

        self.assertFalse(trimmer())
        self.assertFalse(trimmer())
        self.assertEqual([("libc.so.6", True)], loads)

    def test_a_symbol_setup_failure_is_a_permanent_no_op(self):
        loads = []

        class FailingSignatureMallocTrim:
            restype = None

            @property
            def argtypes(self):
                return None

            @argtypes.setter
            def argtypes(self, _value):
                raise RuntimeError("synthetic signature failure")

            def __call__(self, _padding):
                return 1

        libc = type("FakeLibc", (), {"malloc_trim": FailingSignatureMallocTrim()})()

        def load_libc(name, *, use_errno):
            loads.append((name, use_errno))
            return libc

        trimmer = _NativeHeapTrimmer(platform="linux", libc_loader=load_libc)

        self.assertFalse(trimmer())
        self.assertFalse(trimmer())
        self.assertEqual([("libc.so.6", True)], loads)

    def test_a_trim_call_failure_is_a_permanent_no_op(self):
        loads = []

        class FailingMallocTrim(FakeMallocTrim):
            def __call__(self, padding):
                self.calls.append(padding)
                raise RuntimeError("synthetic trim failure")

        malloc_trim = FailingMallocTrim()
        libc = type("FakeLibc", (), {"malloc_trim": malloc_trim})()

        def load_libc(name, *, use_errno):
            loads.append((name, use_errno))
            return libc

        trimmer = _NativeHeapTrimmer(platform="linux", libc_loader=load_libc)

        self.assertFalse(trimmer())
        self.assertFalse(trimmer())
        self.assertEqual([("libc.so.6", True)], loads)
        self.assertEqual([0], malloc_trim.calls)

    def test_glibc_malloc_trim_is_typed_cached_and_called_with_zero(self):
        loads = []
        malloc_trim = FakeMallocTrim()
        libc = type("FakeLibc", (), {"malloc_trim": malloc_trim})()

        def load_libc(name, *, use_errno):
            loads.append((name, use_errno))
            return libc

        trimmer = _NativeHeapTrimmer(platform="linux", libc_loader=load_libc)

        self.assertTrue(trimmer())
        self.assertTrue(trimmer())
        self.assertEqual([("libc.so.6", True)], loads)
        self.assertEqual([ctypes.c_size_t], malloc_trim.argtypes)
        self.assertIs(ctypes.c_int, malloc_trim.restype)
        self.assertEqual([0, 0], malloc_trim.calls)


if __name__ == "__main__":
    unittest.main()
