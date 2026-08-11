import unittest
from threading import Barrier, Thread
from unittest.mock import patch

from quasarr.search.runtime import (
    SearchRuntime,
    _parse_proc_fields,
    _read_proc_fields,
    read_process_memory,
    search_runtime,
)

SNAPSHOT_KEYS = {
    "active_requests",
    "active_source_tasks",
    "overdue_source_tasks",
    "peak_active_source_tasks",
    "requests_started",
    "requests_completed",
    "categories_planned",
    "families_planned",
    "source_started",
    "source_completed",
    "source_dropped",
    "source_skipped",
    "source_errored",
    "source_budget_exhausted",
    "cache_hits",
    "cache_misses",
    "cache_evictions",
    "coalesced_waiters",
    "rss_kib",
    "pss_kib",
    "threads",
}


def make_runtime(memory=None):
    return SearchRuntime(clock=lambda: 100.0, memory_reader=lambda: memory or {})


class SearchRuntimeTests(unittest.TestCase):
    def test_request_and_source_counts_return_to_zero(self):
        runtime = SearchRuntime(clock=lambda: 100.0, memory_reader=lambda: {})
        with runtime.request(category_count=3, family_count=1):
            with runtime.source_task():
                snapshot = runtime.snapshot()
                self.assertEqual(1, snapshot["active_requests"])
                self.assertEqual(1, snapshot["active_source_tasks"])
        snapshot = runtime.snapshot()
        self.assertEqual(0, snapshot["active_requests"])
        self.assertEqual(0, snapshot["active_source_tasks"])

    def test_fixed_outcome_counters_are_recorded(self):
        runtime = SearchRuntime(clock=lambda: 100.0, memory_reader=lambda: {})
        runtime.record_source_outcome("completed")
        runtime.record_source_outcome("dropped")
        runtime.record_source_outcome("budget_exhausted")
        self.assertEqual(1, runtime.snapshot()["source_completed"])
        self.assertEqual(1, runtime.snapshot()["source_dropped"])
        self.assertEqual(1, runtime.snapshot()["source_budget_exhausted"])

    def test_unknown_outcome_is_rejected(self):
        runtime = SearchRuntime(clock=lambda: 100.0, memory_reader=lambda: {})
        with self.assertRaises(ValueError):
            runtime.record_source_outcome("source-name-must-not-be-a-label")

    def test_skipped_and_errored_outcomes_are_recorded(self):
        runtime = make_runtime()

        runtime.record_source_outcome("skipped")
        runtime.record_source_outcome("errored")
        runtime.record_source_outcome("errored")

        snapshot = runtime.snapshot()
        self.assertEqual(1, snapshot["source_skipped"])
        self.assertEqual(2, snapshot["source_errored"])

    def test_snapshot_exposes_only_fixed_cardinality_keys(self):
        # Every key is a fixed counter name. A source initial, query, URL or
        # category ID must never reach the snapshot, because these counters end
        # up in logs and unbounded label values would leak user configuration.
        runtime = make_runtime()

        with runtime.request(category_count=2, family_count=1):
            with runtime.source_task():
                pass

        self.assertEqual(SNAPSHOT_KEYS, set(runtime.snapshot()))

    def test_request_totals_accumulate_across_requests(self):
        runtime = make_runtime()

        with runtime.request(category_count=3, family_count=2):
            pass
        with runtime.request(category_count=1, family_count=1):
            pass

        snapshot = runtime.snapshot()
        self.assertEqual(2, snapshot["requests_started"])
        self.assertEqual(2, snapshot["requests_completed"])
        self.assertEqual(4, snapshot["categories_planned"])
        self.assertEqual(3, snapshot["families_planned"])
        self.assertEqual(0, snapshot["active_requests"])

    def test_gauges_are_restored_when_the_body_raises(self):
        # The fan-out raises on source errors; a gauge that only decrements on
        # the success path would report phantom in-flight work forever.
        runtime = make_runtime()

        with self.assertRaises(RuntimeError):
            with runtime.request(category_count=1, family_count=1):
                with runtime.source_task():
                    raise RuntimeError("source failed")

        snapshot = runtime.snapshot()
        self.assertEqual(0, snapshot["active_requests"])
        self.assertEqual(0, snapshot["active_source_tasks"])
        self.assertEqual(1, snapshot["requests_completed"])
        self.assertEqual(1, snapshot["source_started"])

    def test_peak_active_source_tasks_keeps_the_high_water_mark(self):
        runtime = make_runtime()

        with runtime.source_task():
            with runtime.source_task():
                with runtime.source_task():
                    pass

        snapshot = runtime.snapshot()
        self.assertEqual(0, snapshot["active_source_tasks"])
        self.assertEqual(3, snapshot["peak_active_source_tasks"])
        self.assertEqual(3, snapshot["source_started"])

    def test_overdue_source_tasks_are_tracked_as_a_gauge(self):
        # A source dropped at the deadline keeps running; the gauge has to fall
        # again once its future finishes, otherwise idle reclamation would
        # never see a quiet process.
        runtime = make_runtime()

        runtime.mark_source_overdue()
        runtime.mark_source_overdue()
        self.assertEqual(2, runtime.snapshot()["overdue_source_tasks"])

        runtime.resolve_source_overdue()
        self.assertEqual(1, runtime.snapshot()["overdue_source_tasks"])

        runtime.resolve_source_overdue()
        runtime.resolve_source_overdue()
        self.assertEqual(0, runtime.snapshot()["overdue_source_tasks"])

    def test_cache_counters_are_recorded(self):
        runtime = make_runtime()

        runtime.record_cache_hit()
        runtime.record_cache_hit()
        runtime.record_cache_miss()
        runtime.record_cache_eviction()
        runtime.record_coalesced_waiter()

        snapshot = runtime.snapshot()
        self.assertEqual(2, snapshot["cache_hits"])
        self.assertEqual(1, snapshot["cache_misses"])
        self.assertEqual(1, snapshot["cache_evictions"])
        self.assertEqual(1, snapshot["coalesced_waiters"])

    def test_injected_memory_readings_are_merged_into_the_snapshot(self):
        runtime = make_runtime({"rss_kib": 4096, "pss_kib": 2048, "threads": 12})

        snapshot = runtime.snapshot()

        self.assertEqual(4096, snapshot["rss_kib"])
        self.assertEqual(2048, snapshot["pss_kib"])
        self.assertEqual(12, snapshot["threads"])

    def test_missing_memory_readings_stay_none(self):
        runtime = make_runtime({"rss_kib": 4096})

        snapshot = runtime.snapshot()

        self.assertEqual(4096, snapshot["rss_kib"])
        self.assertIsNone(snapshot["pss_kib"])
        self.assertIsNone(snapshot["threads"])

    def test_unknown_memory_reader_keys_are_ignored(self):
        runtime = make_runtime({"rss_kib": 4096, "process_command_line": "secret"})

        self.assertEqual(SNAPSHOT_KEYS, set(runtime.snapshot()))

    def test_activity_is_stamped_with_the_injected_clock(self):
        ticks = iter([10.0, 20.0, 30.0, 40.0])
        runtime = SearchRuntime(clock=lambda: next(ticks), memory_reader=lambda: {})

        self.assertEqual(10.0, runtime.last_activity_at)
        with runtime.request(category_count=1, family_count=1):
            self.assertEqual(20.0, runtime.last_activity_at)
        self.assertEqual(30.0, runtime.last_activity_at)

    def test_reset_clears_counters_and_gauges(self):
        runtime = make_runtime()

        with runtime.request(category_count=2, family_count=1):
            with runtime.source_task():
                runtime.record_source_outcome("completed")
        runtime.mark_source_overdue()

        runtime.reset()

        self.assertEqual(make_runtime().snapshot(), runtime.snapshot())

    def test_concurrent_source_tasks_are_counted_exactly(self):
        # The fan-out runs one worker per source, so every counter update has to
        # survive concurrent threads without losing increments.
        runtime = make_runtime()
        workers = 8
        barrier = Barrier(workers)

        def work():
            barrier.wait(5)
            with runtime.source_task():
                runtime.record_source_outcome("completed")

        threads = [Thread(target=work) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)

        snapshot = runtime.snapshot()
        self.assertEqual(workers, snapshot["source_started"])
        self.assertEqual(workers, snapshot["source_completed"])
        self.assertEqual(0, snapshot["active_source_tasks"])

    def test_module_singleton_is_a_search_runtime(self):
        self.assertIsInstance(search_runtime, SearchRuntime)


class ProcessMemoryTests(unittest.TestCase):
    def test_non_linux_platforms_report_no_readings(self):
        with patch("quasarr.search.runtime.sys.platform", "win32"):
            self.assertEqual(
                {"rss_kib": None, "pss_kib": None, "threads": None},
                read_process_memory(),
            )

    def test_proc_fields_are_parsed_by_name(self):
        lines = [
            "Name:\tpython3\n",
            "VmRSS:\t  123456 kB\n",
            "Threads:\t17\n",
        ]

        self.assertEqual(
            {"rss_kib": 123456, "threads": 17},
            _parse_proc_fields(lines, {"VmRSS": "rss_kib", "Threads": "threads"}),
        )

    def test_unparsable_proc_values_are_skipped(self):
        lines = ["VmRSS:\tunknown kB\n", "Threads:\n", "Pss:\t42 kB\n"]

        self.assertEqual(
            {"pss_kib": 42},
            _parse_proc_fields(
                lines,
                {"VmRSS": "rss_kib", "Threads": "threads", "Pss": "pss_kib"},
            ),
        )

    def test_unreadable_proc_file_yields_no_fields(self):
        # smaps_rollup is absent on some kernels and containers; a missing file
        # must degrade to "unknown", never raise into a search request.
        self.assertEqual(
            {}, _read_proc_fields("/proc/self/does-not-exist", {"Pss": "pss_kib"})
        )


if __name__ == "__main__":
    unittest.main()
